"""Atomic user-facing promotion and coupon operations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Coupon, CouponActivation, CouponUsage, Order, Promotion, User
from utils.ledger import record_balance_movement
from utils.money import ZERO, money


class CouponError(ValueError):
    """Expected coupon error that is safe to show to a customer."""


@dataclass(frozen=True)
class CouponRedemption:
    coupon: Coupon
    credited_amount: Decimal = ZERO
    activation: CouponActivation | None = None
    already_active: bool = False
    already_redeemed: bool = False


async def get_active_promotion(
    session: AsyncSession,
    product_id: int,
    quantity: int,
) -> Optional[Promotion]:
    """Return the best active legacy promotion for a product."""
    now = datetime.now()
    result = await session.execute(
        select(Promotion)
        .where(
            and_(
                Promotion.is_active.is_(True),
                Promotion.start_date <= now,
                Promotion.end_date >= now,
                Promotion.min_quantity <= quantity,
                (Promotion.product_id == product_id) | (Promotion.product_id.is_(None)),
            )
        )
        .order_by(Promotion.discount_value.desc())
    )
    return result.scalars().first()


def apply_promotion(
    base_price: Decimal | float | int,
    quantity: int,
    promotion: Promotion,
) -> tuple[Decimal, Decimal]:
    """Calculate a legacy promotion exactly, without writing to the database."""
    base = money(base_price) * quantity
    if promotion.discount_type == "PERCENT":
        discount = money(base * Decimal(str(promotion.discount_value)) / Decimal("100"))
    else:
        discount = money(Decimal(str(promotion.discount_value)) * quantity)
    discount = min(base, discount)
    return discount, money(base - discount)


async def _locked_coupon(session: AsyncSession, code: str) -> Coupon:
    result = await session.execute(
        select(Coupon)
        .where(Coupon.code == code.upper())
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    coupon = result.scalar_one_or_none()
    if coupon is None:
        raise CouponError("Промокод не найден")
    now = datetime.now()
    if not coupon.is_active:
        raise CouponError("Промокод неактивен")
    if now < coupon.valid_from or (coupon.valid_until is not None and now > coupon.valid_until):
        raise CouponError("Срок действия промокода истёк")
    return coupon


async def _locked_user(session: AsyncSession, telegram_user_id: int) -> User:
    result = await session.execute(
        select(User)
        .where(User.telegram_id == telegram_user_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise CouponError("Пользователь не найден. Нажмите /start")
    if user.is_blocked:
        raise CouponError("Промокод недоступен")
    return user


async def _locked_usage(session: AsyncSession, coupon_id: int, user_id: int) -> CouponUsage | None:
    result = await session.execute(
        select(CouponUsage)
        .where(CouponUsage.coupon_id == coupon_id, CouponUsage.user_id == user_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return result.scalar_one_or_none()


def _check_limits(coupon: Coupon, usage: CouponUsage | None) -> None:
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        raise CouponError("Промокод исчерпан")
    if (
        coupon.max_uses_per_user is not None
        and usage is not None
        and usage.used_count >= coupon.max_uses_per_user
    ):
        raise CouponError("Ваш личный лимит использований промокода исчерпан")


async def _increment_usage(
    session: AsyncSession,
    coupon: Coupon,
    user_id: int,
    usage: CouponUsage | None,
) -> CouponUsage:
    """Increment limits while the coupon row is locked; no internal commit."""
    _check_limits(coupon, usage)
    coupon.used_count += 1
    if usage is None:
        usage = CouponUsage(coupon_id=coupon.id, user_id=user_id, used_count=1)
        session.add(usage)
    else:
        usage.used_count += 1
    await session.flush()
    return usage


async def validate_coupon(
    session: AsyncSession,
    coupon_code: str,
    user_id: Optional[int] = None,
) -> tuple[Coupon | None, str | None]:
    """Read-only validation for a UI preview.

    The final activation must still call :func:`redeem_coupon`, which locks the
    coupon and its per-user counter in the same transaction.
    """
    result = await session.execute(select(Coupon).where(Coupon.code == coupon_code.upper()))
    coupon = result.scalar_one_or_none()
    if coupon is None:
        return None, "Промокод не найден"
    now = datetime.now()
    if not coupon.is_active:
        return None, "Промокод неактивен"
    if now < coupon.valid_from or (coupon.valid_until and now > coupon.valid_until):
        return None, "Срок действия промокода истёк"
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        return None, "Промокод исчерпан"
    if user_id is not None and coupon.max_uses_per_user is not None:
        usage_result = await session.execute(
            select(CouponUsage.used_count).where(
                CouponUsage.coupon_id == coupon.id,
                CouponUsage.user_id == user_id,
            )
        )
        if (usage_result.scalar_one_or_none() or 0) >= coupon.max_uses_per_user:
            return None, "Ваш личный лимит использований промокода исчерпан"
    return coupon, None


async def redeem_coupon(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    coupon_code: str,
    idempotency_key: str | None = None,
) -> CouponRedemption:
    """Redeem a code atomically and without committing the caller's session.

    A fixed code credits the internal balance immediately.  A percentage code
    creates/updates an activation: ``ONE_TIME`` discounts one reserved order;
    ``PERMANENT`` applies to future orders until disabled or expired.
    """
    normalized_code = (coupon_code or "").strip().upper()
    if not normalized_code:
        raise CouponError("Введите промокод")
    user = await _locked_user(session, telegram_user_id)
    coupon = await _locked_coupon(session, normalized_code)
    usage = await _locked_usage(session, coupon.id, user.id)

    if coupon.discount_type == "FIXED":
        credited = money(coupon.discount_value)
        balance_after = money(user.balance) + credited
        operation_key = (
            f"coupon:fixed_attempt:{coupon.id}:{user.id}:{idempotency_key}"
            if idempotency_key
            else None
        )
        ledger_result = await record_balance_movement(
            session,
            user_id=user.id,
            amount=credited,
            reason="promo",
            description=f"Промокод {coupon.code}",
            reference_type="coupon",
            reference_id=coupon.id,
            operation_key=operation_key,
            balance_after=balance_after,
        )
        if not ledger_result.created:
            return CouponRedemption(coupon=coupon, already_redeemed=True)
        usage = await _increment_usage(session, coupon, user.id, usage)
        user.balance = balance_after
        return CouponRedemption(coupon=coupon, credited_amount=credited)

    if coupon.discount_type != "PERCENT":
        raise CouponError("У промокода указан неизвестный тип награды")

    result = await session.execute(
        select(CouponActivation)
        .where(CouponActivation.coupon_id == coupon.id, CouponActivation.user_id == user.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    activation = result.scalar_one_or_none()
    permanent = coupon.percent_mode == "PERMANENT"
    if permanent and activation and activation.is_active and activation.uses_remaining is None:
        return CouponRedemption(coupon=coupon, activation=activation, already_active=True)
    if (
        not permanent
        and activation
        and activation.is_active
        and (activation.uses_remaining or 0) > 0
    ):
        # A second delivery of the same Telegram update (or a repeated click)
        # must not consume another per-user/global use while a one-time
        # discount is already waiting for the next order.
        return CouponRedemption(coupon=coupon, activation=activation, already_active=True)

    usage = await _increment_usage(session, coupon, user.id, usage)
    if activation is None:
        activation = CouponActivation(
            coupon_id=coupon.id,
            user_id=user.id,
            uses_remaining=None if permanent else 1,
            is_active=True,
        )
        session.add(activation)
    else:
        activation.uses_remaining = None if permanent else 1
        activation.is_active = True
    await session.flush()
    return CouponRedemption(coupon=coupon, activation=activation)


async def reserve_active_coupon_for_order(
    session: AsyncSession,
    *,
    user_id: int,
) -> tuple[Coupon | None, CouponActivation | None]:
    """Lock a usable active percentage coupon and reserve a one-time use."""
    now = datetime.now()
    result = await session.execute(
        select(CouponActivation, Coupon)
        .join(Coupon, Coupon.id == CouponActivation.coupon_id)
        .where(
            CouponActivation.user_id == user_id,
            CouponActivation.is_active.is_(True),
            Coupon.is_active.is_(True),
            Coupon.discount_type == "PERCENT",
            Coupon.valid_from <= now,
            (Coupon.valid_until.is_(None)) | (Coupon.valid_until >= now),
        )
        .order_by(CouponActivation.updated_at.desc(), CouponActivation.id.desc())
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    for activation, coupon in result.all():
        if coupon.percent_mode == "PERMANENT":
            return coupon, activation
        if activation.uses_remaining and activation.uses_remaining > 0:
            activation.uses_remaining -= 1
            if activation.uses_remaining == 0:
                activation.is_active = False
            return coupon, activation
    return None, None


async def restore_coupon_for_cancelled_order(session: AsyncSession, order: Order) -> None:
    """Return a reserved one-time coupon if its order is cancelled unpaid."""
    if not order.coupon_activation_id:
        return
    result = await session.execute(
        select(CouponActivation, Coupon)
        .join(Coupon, Coupon.id == CouponActivation.coupon_id)
        .where(CouponActivation.id == order.coupon_activation_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    row = result.one_or_none()
    if not row:
        return
    activation, coupon = row
    if coupon.percent_mode == "ONE_TIME":
        activation.uses_remaining = max(1, activation.uses_remaining or 0)
        activation.is_active = True


def apply_coupon(
    total_amount: Decimal | float | int,
    coupon: Coupon | None,
) -> tuple[Decimal, Decimal]:
    """Apply a percentage coupon to an already calculated order total."""
    total = money(total_amount)
    if coupon is None or coupon.discount_type != "PERCENT":
        return ZERO, total
    percent = Decimal(str(coupon.discount_value))
    discount = money(total * percent / Decimal("100"))
    discount = min(total, discount)
    return discount, money(total - discount)


async def use_coupon(session: AsyncSession, coupon_id: int, user_id: Optional[int] = None) -> None:
    """Compatibility shim kept for legacy callers.

    New code must call :func:`redeem_coupon`; this function intentionally does
    not commit, so it can never split a checkout transaction.
    """
    if user_id is None:
        raise CouponError("Для применения промокода нужен пользователь")
    result = await session.execute(select(Coupon).where(Coupon.id == coupon_id))
    coupon = result.scalar_one_or_none()
    if coupon is None:
        raise CouponError("Промокод не найден")
    await redeem_coupon(session, telegram_user_id=user_id, coupon_code=coupon.code)
