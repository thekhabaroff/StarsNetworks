"""Create immutable multi-recipient checkout batches from a persistent cart."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import settings
from database.models import Cart, CartItem, Coupon, CouponActivation, Order, OrderBatch, Product, User
from utils.cart import MAX_CART_ITEMS
from utils.global_discount import (
    calculate_order_price,
    get_global_discount_config,
    get_personal_discount_config,
    product_category_lineage,
)
from utils.money import ZERO, money
from utils.promotions import reserve_active_coupon_for_order
from utils.stars_catalog import is_virtual_product


BATCH_PENDING = "PENDING_PAYMENT"
BATCH_PAID = "PAID"
BATCH_PARTIAL = "PARTIAL"
BATCH_COMPLETED = "COMPLETED"
BATCH_CANCELLED = "CANCELLED"


class BatchCheckoutError(ValueError):
    """Expected checkout error safe to show to the buyer."""


@dataclass(frozen=True)
class BatchPreview:
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    items_count: int


async def _locked_buyer(session: AsyncSession, telegram_user_id: int) -> User:
    user = (await session.execute(
        select(User).where(User.telegram_id == telegram_user_id).with_for_update()
    )).scalar_one_or_none()
    if user is None:
        raise BatchCheckoutError("Пользователь не найден. Нажмите /start")
    if user.is_blocked:
        raise BatchCheckoutError("Покупки для этого аккаунта недоступны")
    return user


async def _cart_rows(
    session: AsyncSession, user_id: int, *, lock: bool = False
) -> tuple[Cart, list[tuple[CartItem, Product]]]:
    cart_stmt = select(Cart).where(Cart.user_id == user_id)
    if lock:
        cart_stmt = cart_stmt.with_for_update()
    cart = (await session.execute(cart_stmt)).scalar_one_or_none()
    if cart is None:
        raise BatchCheckoutError("Корзина пуста")
    stmt = (
        select(CartItem, Product)
        .join(Product, Product.id == CartItem.product_id)
        .where(CartItem.cart_id == cart.id)
        .order_by(CartItem.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    rows = list((await session.execute(stmt)).all())
    if not rows:
        raise BatchCheckoutError("Корзина пуста")
    if len(rows) > MAX_CART_ITEMS:
        raise BatchCheckoutError("В корзине слишком много позиций")
    return cart, rows


async def preview_cart(
    session: AsyncSession, telegram_user_id: int
) -> BatchPreview:
    user = (await session.execute(
        select(User).where(User.telegram_id == telegram_user_id)
    )).scalar_one_or_none()
    if user is None:
        raise BatchCheckoutError("Пользователь не найден")
    _cart, rows = await _cart_rows(session, user.id)
    global_discount = await get_global_discount_config(session)
    personal_discount = await get_personal_discount_config(session, user)
    coupon_row = (await session.execute(
        select(CouponActivation, Coupon)
        .join(Coupon, Coupon.id == CouponActivation.coupon_id)
        .where(
            CouponActivation.user_id == user.id,
            CouponActivation.is_active.is_(True),
            Coupon.is_active.is_(True),
            Coupon.discount_type == "PERCENT",
        )
        .order_by(CouponActivation.updated_at.desc(), CouponActivation.id.desc())
    )).first()
    coupon = coupon_row[1] if coupon_row else None
    subtotal = ZERO
    discount = ZERO
    total = ZERO
    for item, product in rows:
        if not product.is_active or not is_virtual_product(product):
            raise BatchCheckoutError(f"Товар «{product.name}» больше недоступен")
        subtotal += money(product.price) * item.quantity
        lineage = await product_category_lineage(session, product.category_id)
        pricing = calculate_order_price(
            price_per_unit=money(product.price),
            quantity=item.quantity,
            product=product,
            config=global_discount,
            category_lineage=lineage,
            coupon_percent=(Decimal(str(coupon.discount_value)) if coupon else ZERO),
            personal_config=personal_discount,
        )
        discount += pricing.discount_amount
        total += pricing.total_amount
    if total <= ZERO:
        raise BatchCheckoutError("Скидки не могут сделать заказ бесплатным")
    return BatchPreview(money(subtotal), money(discount), money(total), len(rows))


async def create_order_batch(
    session: AsyncSession, telegram_user_id: int
) -> tuple[OrderBatch, list[Order]]:
    """Materialize one cart into an immutable batch and child orders.

    The caller commits exactly once. No provider calls happen while database
    locks are held. Prices are read from the already refreshed product cache.
    """
    user = await _locked_buyer(session, telegram_user_id)
    result = await session.execute(
        select(func.count(OrderBatch.id)).where(
            OrderBatch.user_id == user.id,
            OrderBatch.status == BATCH_PENDING,
        )
    )
    if int(result.scalar_one() or 0) >= 3:
        raise BatchCheckoutError(
            "У вас уже есть три неоплаченных оформления. Оплатите или отмените их сначала."
        )

    cart, rows = await _cart_rows(session, user.id, lock=True)
    global_discount = await get_global_discount_config(session)
    personal_discount = await get_personal_discount_config(session, user)
    coupon, activation = await reserve_active_coupon_for_order(session, user_id=user.id)

    subtotal = ZERO
    discount_total = ZERO
    line_data: list[tuple[CartItem, Product, object]] = []
    for item, product in rows:
        if not product.is_active or not is_virtual_product(product):
            raise BatchCheckoutError(f"Товар «{product.name}» больше недоступен")
        if item.quantity <= 0:
            raise BatchCheckoutError("В корзине указано некорректное количество")
        item.unit_price = money(product.price)
        lineage = await product_category_lineage(session, product.category_id)
        pricing = calculate_order_price(
            price_per_unit=item.unit_price,
            quantity=item.quantity,
            product=product,
            config=global_discount,
            category_lineage=lineage,
            coupon_percent=(Decimal(str(coupon.discount_value)) if coupon else ZERO),
            personal_config=personal_discount,
        )
        if pricing.total_amount <= ZERO:
            raise BatchCheckoutError("Скидки не могут сделать заказ бесплатным")
        subtotal += item.unit_price * item.quantity
        discount_total += pricing.discount_amount
        line_data.append((item, product, pricing))

    total = money(sum((pricing.total_amount for _, _, pricing in line_data), ZERO))
    if total <= ZERO:
        raise BatchCheckoutError("Корзина не может быть бесплатной")

    now = datetime.now()
    batch = OrderBatch(
        user_id=user.id,
        status=BATCH_PENDING,
        subtotal_amount=money(subtotal),
        discount_amount=money(discount_total),
        total_amount=total,
        coupon_activation_id=activation.id if activation else None,
        reserved_until=now + timedelta(minutes=settings.ORDER_RESERVATION_MINUTES),
    )
    session.add(batch)
    await session.flush()

    orders: list[Order] = []
    for item, product, pricing in line_data:
        order = Order(
            user_id=user.id,
            product_id=product.id,
            batch_id=batch.id,
            quantity=item.quantity,
            price_per_unit=item.unit_price,
            discount=pricing.discount_percent,
            discount_amount=pricing.discount_amount,
            total_amount=pricing.total_amount,
            status="ОЖИДАЕТ ОПЛАТЫ",
            target_username=item.target_username,
            reserved_until=batch.reserved_until,
        )
        session.add(order)
        orders.append(order)

    cart.status = "CHECKED_OUT"
    await session.delete(cart)
    await session.flush()
    return batch, orders
