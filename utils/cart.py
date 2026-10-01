"""Persistent cart operations for multi-recipient virtual products."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Cart, CartItem, Product
from utils.money import ZERO, money
from utils.stars_catalog import is_premium_product, is_stars_product, is_virtual_product


MAX_CART_ITEMS = 20
MAX_CART_STARS = 1_000_000


class CartError(ValueError):
    """Expected cart error safe to show to the user."""


@dataclass(frozen=True)
class CartSummary:
    items_count: int
    total: Decimal


def normalize_username(value: str) -> str:
    username = (value or "").strip().removeprefix("@").strip()
    return username


async def get_or_create_cart(
    session: AsyncSession, user_id: int, *, lock: bool = False
) -> Cart:
    stmt = select(Cart).where(Cart.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    cart = (await session.execute(stmt)).scalar_one_or_none()
    if cart is None:
        cart = Cart(user_id=user_id, status="DRAFT")
        session.add(cart)
        await session.flush()
    elif cart.status != "DRAFT":
        cart.status = "DRAFT"
        await session.flush()
    return cart


async def get_cart_items(
    session: AsyncSession, user_id: int, *, lock: bool = False
) -> list[tuple[CartItem, Product]]:
    cart = await get_or_create_cart(session, user_id, lock=lock)
    stmt = (
        select(CartItem, Product)
        .join(Product, Product.id == CartItem.product_id)
        .where(CartItem.cart_id == cart.id)
        .order_by(CartItem.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).all())


async def get_cart_summary(session: AsyncSession, user_id: int) -> CartSummary:
    cart_id = await session.scalar(select(Cart.id).where(Cart.user_id == user_id, Cart.status == "DRAFT"))
    if cart_id is None:
        return CartSummary(items_count=0, total=ZERO)
    rows = list((await session.execute(
        select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.id)
    )).scalars())
    total = sum((money(item.unit_price) * item.quantity for item in rows), ZERO)
    return CartSummary(items_count=len(rows), total=money(total))


def _validate_target(target_username: str) -> str:
    import re
    username = normalize_username(target_username)
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise CartError("Некорректный username получателя")
    return username


async def add_cart_item(
    session: AsyncSession,
    *,
    user_id: int,
    product: Product,
    target_username: str,
    quantity: int,
) -> CartItem:
    if not is_virtual_product(product) or not product.is_active:
        raise CartError("Товар недоступен для добавления в корзину")
    if not isinstance(quantity, int) or quantity <= 0:
        raise CartError("Количество должно быть больше нуля")
    target = _validate_target(target_username)
    if is_premium_product(product) and quantity != 1:
        raise CartError("Одна подписка Premium добавляется на одного получателя")
    if is_stars_product(product) and quantity > MAX_CART_STARS:
        raise CartError(f"За один получатель можно добавить не более {MAX_CART_STARS:,} Stars")

    cart = await get_or_create_cart(session, user_id, lock=True)
    result = await session.execute(
        select(CartItem)
        .where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == product.id,
            CartItem.target_username.ilike(target),
        )
        .with_for_update()
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if is_premium_product(product):
            raise CartError("Этот тариф Premium для данного получателя уже есть в корзине")
        if existing.quantity + quantity > MAX_CART_STARS:
            raise CartError(f"Общее количество для получателя не может превышать {MAX_CART_STARS:,} Stars")
        existing.quantity += quantity
        existing.unit_price = money(product.price)
        return existing

    # Count rows without loading all cart items into memory.
    from sqlalchemy import func
    item_count = await session.scalar(
        select(func.count(CartItem.id)).where(CartItem.cart_id == cart.id)
    )
    if int(item_count or 0) >= MAX_CART_ITEMS:
        raise CartError(f"В корзине может быть не более {MAX_CART_ITEMS} позиций")
    item = CartItem(
        cart_id=cart.id,
        product_id=product.id,
        target_username=target,
        quantity=quantity,
        unit_price=money(product.price),
    )
    session.add(item)
    await session.flush()
    return item


async def remove_cart_item(session: AsyncSession, *, user_id: int, item_id: int) -> bool:
    cart = await get_or_create_cart(session, user_id, lock=True)
    result = await session.execute(
        select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart.id).with_for_update()
    )
    item = result.scalar_one_or_none()
    if item is None:
        return False
    await session.delete(item)
    await session.flush()
    return True


async def clear_cart(session: AsyncSession, *, user_id: int) -> None:
    cart = await get_or_create_cart(session, user_id, lock=True)
    await session.execute(delete(CartItem).where(CartItem.cart_id == cart.id))
    await session.flush()


async def update_cart_target(
    session: AsyncSession, *, user_id: int, item_id: int, target_username: str
) -> CartItem:
    target = _validate_target(target_username)
    cart = await get_or_create_cart(session, user_id, lock=True)
    item = (await session.execute(
        select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart.id).with_for_update()
    )).scalar_one_or_none()
    if item is None:
        raise CartError("Позиция корзины не найдена")
    duplicate = await session.scalar(
        select(CartItem.id).where(
            CartItem.cart_id == cart.id, CartItem.id != item.id,
            CartItem.product_id == item.product_id, CartItem.target_username.ilike(target),
        )
    )
    if duplicate is not None:
        raise CartError("Такая позиция уже есть в корзине")
    item.target_username = target
    return item


async def update_cart_quantity(
    session: AsyncSession, *, user_id: int, item_id: int, quantity: int
) -> CartItem:
    if quantity <= 0 or quantity > MAX_CART_STARS:
        raise CartError(f"Количество должно быть от 1 до {MAX_CART_STARS:,}")
    cart = await get_or_create_cart(session, user_id, lock=True)
    item = (await session.execute(
        select(CartItem).join(Product, Product.id == CartItem.product_id).where(
            CartItem.id == item_id, CartItem.cart_id == cart.id,
            Product.delivery_type == "telegram_stars",
        ).with_for_update()
    )).scalar_one_or_none()
    if item is None:
        raise CartError("Изменение количества доступно только для Stars")
    item.quantity = quantity
    return item
