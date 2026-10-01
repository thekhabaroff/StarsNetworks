"""Catalog policy for Stars Networks.

The catalog engine supports many product types.  Stars
Networks exposes two virtual products: Telegram Stars and Telegram Premium.
Historical account products remain untouched in the database and are kept
inactive in the public catalogue.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Category, Product


STARS_DELIVERY_TYPE = "telegram_stars"
PREMIUM_DELIVERY_TYPE = "telegram_premium"
VIRTUAL_DELIVERY_TYPES = frozenset({STARS_DELIVERY_TYPE, PREMIUM_DELIVERY_TYPE})
STARS_CATEGORY_NAME = "⭐ Telegram Stars"
STARS_PRODUCT_NAME = "Telegram Stars"
DEFAULT_STARS_PRODUCT_PRICE = Decimal("1.50")
PREMIUM_CATEGORY_NAME = "💎 Telegram Premium"
PREMIUM_MONTHS = (3, 6, 12)
DEFAULT_PREMIUM_PRICES = {
    3: Decimal("1200.00"),
    6: Decimal("1600.00"),
    12: Decimal("2900.00"),
}


def is_stars_product(product: Product | None) -> bool:
    return product is not None and getattr(product, "delivery_type", "account") == STARS_DELIVERY_TYPE


def is_premium_product(product: Product | None) -> bool:
    return product is not None and getattr(product, "delivery_type", "account") == PREMIUM_DELIVERY_TYPE


def is_virtual_product(product: Product | None) -> bool:
    return product is not None and getattr(product, "delivery_type", "account") in VIRTUAL_DELIVERY_TYPES


def is_supported_delivery_type(delivery_type: str | None) -> bool:
    return delivery_type in VIRTUAL_DELIVERY_TYPES


async def get_stars_product(session: AsyncSession, *, active_only: bool = True) -> Product | None:
    stmt = select(Product).where(Product.delivery_type == STARS_DELIVERY_TYPE)
    if active_only:
        stmt = stmt.where(Product.is_active.is_(True))
    return (await session.execute(stmt.order_by(Product.id))).scalars().first()


async def ensure_stars_catalog(session: AsyncSession) -> Product:
    """Ensure the active Stars product exists after migrations.

    The operation is idempotent and deliberately deactivates only legacy
    non-virtual products. Their rows and historical orders are preserved for
    audit and refunds, but they can no longer leak into the customer catalog.
    """
    category = (await session.execute(
        select(Category).where(Category.name == STARS_CATEGORY_NAME)
    )).scalars().first()
    if category is None:
        category = Category(
            name=STARS_CATEGORY_NAME,
            description="Пополнение Telegram Stars с автоматической отправкой через Fragment.",
            is_active=True,
        )
        session.add(category)
        await session.flush()
    elif not category.is_active:
        category.is_active = True

    product = await get_stars_product(session, active_only=False)
    if product is None:
        product = Product(
            name=STARS_PRODUCT_NAME,
            description="Telegram Stars с автоматической отправкой на указанный @username.",
            price=DEFAULT_STARS_PRODUCT_PRICE,
            category=category,
            stock_count=0,
            is_active=True,
            format_info="Звёзды Telegram (XTR)",
            recommendations="Проверьте username получателя перед оплатой. После оплаты отправка выполняется автоматически.",
            delivery_type=STARS_DELIVERY_TYPE,
            pricing_mode="COST_PLUS",
            markup_percent=Decimal("0"),
        )
        session.add(product)
    else:
        product.category_id = category.id
        product.is_active = True

    # Deactivate only legacy products; do not delete data or rewrite orders.
    await session.execute(
        update(Product)
        .where(~Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES))
        .values(is_active=False)
    )
    await session.commit()
    await session.refresh(product)
    return product


async def ensure_premium_catalog(session: AsyncSession) -> list[Product]:
    """Ensure one active Premium product exists for each supported duration."""
    category = (await session.execute(
        select(Category).where(Category.name == PREMIUM_CATEGORY_NAME)
    )).scalars().first()
    if category is None:
        category = Category(
            name=PREMIUM_CATEGORY_NAME,
            description="Подарочные подписки Telegram Premium с автоматической отправкой через Fragment.",
            is_active=True,
        )
        session.add(category)
        await session.flush()
    elif not category.is_active:
        category.is_active = True

    products: list[Product] = []
    for months in PREMIUM_MONTHS:
        product = (await session.execute(
            select(Product).where(
                Product.delivery_type == PREMIUM_DELIVERY_TYPE,
                Product.premium_months == months,
            ).order_by(Product.id)
        )).scalars().first()
        if product is None:
            product = Product(
                name=f"💎 Telegram Premium — {months} мес.",
                description=f"Подарочная подписка Telegram Premium на {months} месяца.",
                price=DEFAULT_PREMIUM_PRICES[months],
                category=category,
                stock_count=0,
                is_active=True,
                format_info=f"Telegram Premium ({months} мес.)",
                recommendations="Проверьте username получателя. После оплаты подписка отправляется автоматически.",
                delivery_type=PREMIUM_DELIVERY_TYPE,
                premium_months=months,
                pricing_mode="COST_PLUS",
                markup_percent=Decimal("0"),
            )
            session.add(product)
        else:
            product.category_id = category.id
            product.is_active = True
            product.premium_months = months
        products.append(product)

    await session.execute(
        update(Product)
        .where(~Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES))
        .values(is_active=False)
    )
    await session.commit()
    return products
