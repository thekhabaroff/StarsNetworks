"""Retail price calculation for products.

``Product.price`` remains a cached retail price so existing checkout and
historical orders keep working unchanged. In automatic mode the cost is
refreshed from Fragment's public quote pages and the configured provider API
fee; the administrator only supplies the markup percentage.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Product
from utils.fragment_pricing import FragmentPricingError, quote_cost_rubles
from utils.money import ZERO, money, to_money

logger = logging.getLogger(__name__)


PRICING_MODE_FIXED = "FIXED"
PRICING_MODE_COST_PLUS = "COST_PLUS"
MAX_MARKUP_PERCENT = Decimal("99999.9999")


def calculate_cost_plus_price(cost_price: object, markup_percent: object) -> Decimal:
    """Return retail price = full cost + the configured percentage markup."""
    cost = to_money(cost_price, minimum=Decimal("0.01"))
    try:
        markup = Decimal(str(markup_percent).strip().replace(",", "."))
    except (AttributeError, InvalidOperation, ValueError):
        raise ValueError("Введите корректную наценку в процентах") from None
    if not markup.is_finite() or markup < ZERO or markup > MAX_MARKUP_PERCENT:
        raise ValueError("Наценка должна быть от 0 до 99999.9999%")
    return to_money(cost * (Decimal("1") + markup / Decimal("100")), minimum=Decimal("0.01"))


def _parse_markup(markup_percent: object) -> Decimal:
    try:
        markup = Decimal(str(markup_percent).strip().replace(",", "."))
    except (AttributeError, InvalidOperation, ValueError):
        raise ValueError("Введите корректную наценку в процентах") from None
    if not markup.is_finite() or markup < ZERO or markup > MAX_MARKUP_PERCENT:
        raise ValueError("Наценка должна быть от 0 до 99999.9999%")
    return markup.quantize(Decimal("0.0001"))


def set_cost_plus_pricing(product: Product, markup_percent: object) -> Decimal:
    """Enable automatic cost pricing using only the administrator's markup."""
    normalized_markup = _parse_markup(markup_percent)
    product.pricing_mode = PRICING_MODE_COST_PLUS
    product.markup_percent = normalized_markup
    # The live quote is applied by ``refresh_product_cost_price``. Keeping a
    # previous quote until the refresh succeeds avoids a transient zero price.
    return normalized_markup


def apply_cost_quote(product: Product, cost_price: object) -> Decimal:
    """Store a freshly fetched cost and update the cached retail price."""
    cost = to_money(cost_price, minimum=Decimal("0.01"))
    markup = _parse_markup(product.markup_percent)
    product.pricing_mode = PRICING_MODE_COST_PLUS
    product.cost_price = cost
    product.markup_percent = markup
    product.price = calculate_cost_plus_price(cost, markup)
    return product.price


def set_fixed_pricing(product: Product, price: object) -> Decimal:
    """Switch a product back to a manually entered retail price."""
    retail = to_money(price, minimum=Decimal("0.01"))
    product.pricing_mode = PRICING_MODE_FIXED
    product.price = retail
    return retail


async def sync_cost_plus_prices(session: AsyncSession) -> int:
    """Refresh all automatic prices, preserving the last quote on failures."""
    products = (await session.scalars(
        select(Product).where(Product.pricing_mode == PRICING_MODE_COST_PLUS)
    )).all()
    changed = 0
    for product in products:
        previous_price = money(product.price)
        previous_cost = money(product.cost_price) if product.cost_price is not None else None
        try:
            await refresh_product_cost_price(session, product)
        except FragmentPricingError as exc:
            logger.warning(
                "Could not refresh automatic price for product %s: %s",
                product.id,
                exc,
            )
            continue
        if money(product.price) != previous_price or money(product.cost_price) != previous_cost:
            changed += 1
    if changed:
        await session.commit()
    return changed


async def refresh_product_cost_price(
    session: AsyncSession,
    product: Product,
) -> Decimal:
    """Fetch and apply one current Fragment quote without committing it.

    The ``session`` argument is kept explicit for call-site clarity and future
    quote auditing; this function intentionally does not commit. Checkout can
    then commit the quote together with its immutable order price.
    """
    del session
    if getattr(product, "pricing_mode", PRICING_MODE_FIXED) != PRICING_MODE_COST_PLUS:
        return money(product.price)
    cost, _quote = await quote_cost_rubles(product)
    return apply_cost_quote(product, cost)
