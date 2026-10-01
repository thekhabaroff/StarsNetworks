"""Глобальная скидка магазина и её безопасный расчёт при создании заказа."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Category,
    GlobalDiscountTarget,
    PersonalDiscountTarget,
    Product,
    Setting,
    User,
)
from utils.discount import calculate_discount
from utils.money import money


ENABLED_KEY = "global_discount.enabled"
SCOPE_KEY = "global_discount.scope"
MODE_KEY = "global_discount.mode"
TYPE_KEY = "global_discount.type"
VALUE_KEY = "global_discount.value"
GLOBAL_DISCOUNT_SETTING_KEYS = (ENABLED_KEY, SCOPE_KEY, MODE_KEY, TYPE_KEY, VALUE_KEY)

SCOPES = {"ALL", "CATEGORY", "SUBCATEGORY", "GROUP", "TYPE", "PRODUCT"}
MODES = {"STACK", "MAXIMUM"}
TYPES = {"PERCENT", "FIXED"}
# A 100% discount would turn a paid order into a free one.  The store has no
# free-checkout flow, so keep percentage discounts below that threshold even
# when a stale value is stored in the settings table.
MAX_PERCENT_DISCOUNT = Decimal("99.99")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _number(value: str | None, *, minimum: Decimal, maximum: Decimal | None = None) -> Decimal:
    try:
        number = Decimal(str(value or "0"))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")
    if not number.is_finite():
        return Decimal("0")
    if number < minimum or (maximum is not None and number > maximum):
        return Decimal("0")
    return number


@dataclass(frozen=True)
class GlobalDiscountConfig:
    enabled: bool = False
    scope: str = "ALL"
    mode: str = "MAXIMUM"
    discount_type: str = "PERCENT"
    value: Decimal = Decimal("0")
    target_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class PersonalDiscountConfig:
    enabled: bool = False
    scope: str = "ALL"
    mode: str = "MAXIMUM"
    discount_type: str = "PERCENT"
    value: Decimal = Decimal("0")
    target_ids: frozenset[int] = frozenset()
    valid_until: datetime | None = None


@dataclass(frozen=True)
class PriceBreakdown:
    total_amount: Decimal
    discount_amount: Decimal
    discount_percent: Decimal
    quantity_discount_percent: Decimal
    global_discount_amount: Decimal
    coupon_discount_amount: Decimal
    personal_discount_amount: Decimal


async def get_global_discount_config(session: AsyncSession) -> GlobalDiscountConfig:
    result = await session.execute(
        select(Setting).where(Setting.key.in_(GLOBAL_DISCOUNT_SETTING_KEYS))
    )
    values = {item.key: item.value for item in result.scalars()}
    scope = values.get(SCOPE_KEY, "ALL")
    mode = values.get(MODE_KEY, "MAXIMUM")
    discount_type = values.get(TYPE_KEY, "PERCENT")
    if scope not in SCOPES:
        scope = "ALL"
    if mode not in MODES:
        mode = "MAXIMUM"
    if discount_type not in TYPES:
        discount_type = "PERCENT"
    maximum = MAX_PERCENT_DISCOUNT if discount_type == "PERCENT" else None
    value = _number(values.get(VALUE_KEY), minimum=Decimal("0"), maximum=maximum)
    target_result = await session.execute(
        select(GlobalDiscountTarget.target_id).where(GlobalDiscountTarget.scope == scope)
    )
    return GlobalDiscountConfig(
        enabled=values.get(ENABLED_KEY) == "true",
        scope=scope,
        mode=mode,
        discount_type=discount_type,
        value=value,
        target_ids=frozenset(target_result.scalars().all()),
    )


async def save_global_discount_config(
    session: AsyncSession, config: GlobalDiscountConfig
) -> None:
    """Сохранить единый набор настроек и заменить выбранные цели."""
    values = {
        ENABLED_KEY: "true" if config.enabled else "false",
        SCOPE_KEY: config.scope,
        MODE_KEY: config.mode,
        TYPE_KEY: config.discount_type,
        VALUE_KEY: format(config.value, "f"),
    }
    result = await session.execute(
        select(Setting).where(Setting.key.in_(GLOBAL_DISCOUNT_SETTING_KEYS))
    )
    existing = {item.key: item for item in result.scalars()}
    for key, value in values.items():
        item = existing.get(key)
        if item is None:
            session.add(Setting(key=key, value=value))
        else:
            item.value = value

    await session.execute(delete(GlobalDiscountTarget))
    if config.scope != "ALL":
        for target_id in sorted(config.target_ids):
            session.add(GlobalDiscountTarget(scope=config.scope, target_id=target_id))


async def get_personal_discount_config(
    session: AsyncSession, user: User
) -> PersonalDiscountConfig:
    """Загрузить и нормализовать скидку конкретного пользователя."""
    scope = user.personal_discount_scope if user.personal_discount_scope in SCOPES else "ALL"
    mode = user.personal_discount_mode if user.personal_discount_mode in MODES else "MAXIMUM"
    discount_type = (
        user.personal_discount_type if user.personal_discount_type in TYPES else "PERCENT"
    )
    maximum = MAX_PERCENT_DISCOUNT if discount_type == "PERCENT" else None
    value = _number(str(user.personal_discount), minimum=Decimal("0"), maximum=maximum)
    targets = await session.scalars(
        select(PersonalDiscountTarget.target_id).where(
            PersonalDiscountTarget.user_id == user.id,
            PersonalDiscountTarget.scope == scope,
        )
    )
    not_expired = user.personal_discount_until is None or user.personal_discount_until > datetime.now()
    return PersonalDiscountConfig(
        enabled=bool(user.personal_discount_enabled) and not_expired,
        scope=scope,
        mode=mode,
        discount_type=discount_type,
        value=value,
        target_ids=frozenset(targets.all()),
        valid_until=user.personal_discount_until,
    )


async def save_personal_discount_config(
    session: AsyncSession,
    user: User,
    config: PersonalDiscountConfig,
) -> None:
    """Сохранить персональную скидку и атомарно заменить её цели."""
    user.personal_discount_enabled = bool(config.enabled)
    user.personal_discount_scope = config.scope
    user.personal_discount_mode = config.mode
    user.personal_discount_type = config.discount_type
    user.personal_discount = config.value
    user.personal_discount_until = config.valid_until
    await session.execute(
        delete(PersonalDiscountTarget).where(PersonalDiscountTarget.user_id == user.id)
    )
    if config.scope != "ALL":
        for target_id in sorted(config.target_ids):
            session.add(PersonalDiscountTarget(
                user_id=user.id,
                scope=config.scope,
                target_id=target_id,
            ))


async def product_category_lineage(session: AsyncSession, category_id: int) -> frozenset[int]:
    """Return the category and all its parents, with loop protection."""
    lineage: set[int] = set()
    current_id: int | None = category_id
    while current_id is not None and current_id not in lineage:
        lineage.add(current_id)
        result = await session.execute(
            select(Category.parent_id).where(Category.id == current_id)
        )
        current_id = result.scalar_one_or_none()
    return frozenset(lineage)


def discount_applies_to_product(
    config: GlobalDiscountConfig | PersonalDiscountConfig,
    product: Product,
    *,
    category_lineage: frozenset[int] | None = None,
) -> bool:
    if not config.enabled or config.value <= 0:
        return False
    if config.scope == "ALL":
        return True
    if config.scope == "PRODUCT":
        return product.id in config.target_ids
    # Любой выбранный уровень дерева применяется ко всем его потомкам;
    # различается только набор узлов, который показывает админ-панель.
    return bool((category_lineage or frozenset({product.category_id})) & config.target_ids)


def calculate_order_price(
    *,
    price_per_unit: Decimal,
    quantity: int,
    product: Product,
    config: GlobalDiscountConfig,
    category_lineage: frozenset[int] | None = None,
    coupon_percent: Decimal = Decimal("0"),
    personal_discount_percent: Decimal = Decimal("0"),
    personal_config: PersonalDiscountConfig | None = None,
) -> PriceBreakdown:
    """Рассчитать скидку без изменения данных БД.

    Режим ``STACK`` складывает глобальную и уже действующую скидку за объём.
    ``MAXIMUM`` оставляет только большую из них. Сумма никогда не становится
    отрицательной; фиксированная глобальная скидка применяется к заказу целиком.
    """
    base = _money(money(price_per_unit) * Decimal(quantity))
    quantity_percent = Decimal(str(calculate_discount(quantity)))
    quantity_discount = _money(base * quantity_percent / Decimal("100"))

    global_discount = Decimal("0")
    if discount_applies_to_product(config, product, category_lineage=category_lineage):
        if config.discount_type == "PERCENT":
            global_discount = _money(base * Decimal(str(config.value)) / Decimal("100"))
        else:
            global_discount = _money(Decimal(str(config.value)))

    coupon_discount = _money(base * Decimal(str(coupon_percent)) / Decimal("100"))
    personal_discount = Decimal("0")
    personal_mode = "STACK"
    if personal_config is not None:
        personal_mode = personal_config.mode
        if discount_applies_to_product(
            personal_config, product, category_lineage=category_lineage
        ):
            if personal_config.discount_type == "PERCENT":
                personal_discount = _money(
                    base * Decimal(str(personal_config.value)) / Decimal("100")
                )
            else:
                personal_discount = _money(Decimal(str(personal_config.value)))
    else:
        # Совместимость со старыми вызовами до миграции полного редактора.
        safe_personal_percent = _number(
            str(personal_discount_percent), minimum=Decimal("0"), maximum=MAX_PERCENT_DISCOUNT
        )
        personal_discount = _money(base * safe_personal_percent / Decimal("100"))

    base_discounts = quantity_discount + coupon_discount
    other_discount = (
        base_discounts + personal_discount
        if personal_mode == "STACK"
        else max(base_discounts, personal_discount)
    )
    # "STACK" means the global discount is added to other discounts (volume
    # and coupon).  "MAXIMUM" keeps only the larger side, exactly as the
    # setting promises in the admin panel.
    discount = (
        other_discount + global_discount
        if config.mode == "STACK" else max(other_discount, global_discount)
    )
    discount = min(base, _money(discount))
    total = _money(base - discount)
    effective_percent = _money(discount * Decimal("100") / base) if base else Decimal("0")
    return PriceBreakdown(
        total_amount=total,
        discount_amount=discount,
        discount_percent=effective_percent,
        quantity_discount_percent=quantity_percent,
        global_discount_amount=_money(global_discount),
        coupon_discount_amount=_money(coupon_discount),
        personal_discount_amount=_money(personal_discount),
    )
