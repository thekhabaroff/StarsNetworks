"""Сервис неизменяемого журнала движения средств.

Сервис не меняет ``User.balance`` и не вызывает ``commit``.  В обработчике,
который меняет баланс, нужно выполнить изменение баланса и вызов одной из
функций ``record_*`` в одной транзакции.  Тогда при ошибке не сохранится ни
баланс, ни запись журнала.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from math import ceil
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.ledger import BalanceLedger
from database.models import User


MONEY_QUANT = Decimal("0.01")
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 30

LEDGER_REASON_TOPUP = "topup"
LEDGER_REASON_PURCHASE = "purchase"
LEDGER_REASON_TRANSFER_IN = "transfer_in"
LEDGER_REASON_TRANSFER_OUT = "transfer_out"
LEDGER_REASON_REFERRAL_REWARD = "referral_reward"
LEDGER_REASON_CASHBACK = "cashback"
LEDGER_REASON_MANUAL_ADJUSTMENT = "manual_adjustment"
LEDGER_REASON_SELF_TOPUP = "self_topup"
LEDGER_REASON_REFUND = "refund"
LEDGER_REASON_PROMO = "promo"
LEDGER_REASON_OPENING_BALANCE = "opening_balance"

LEDGER_REASON_LABELS = {
    LEDGER_REASON_TOPUP: "Пополнение",
    LEDGER_REASON_PURCHASE: "Оплата заказа",
    LEDGER_REASON_TRANSFER_IN: "Получен перевод",
    LEDGER_REASON_TRANSFER_OUT: "Перевод пользователю",
    LEDGER_REASON_REFERRAL_REWARD: "Реферальная награда",
    LEDGER_REASON_CASHBACK: "Реферальный кешбэк",
    LEDGER_REASON_MANUAL_ADJUSTMENT: "Корректировка администратором",
    LEDGER_REASON_SELF_TOPUP: "Самостоятельное пополнение",
    LEDGER_REASON_REFUND: "Возврат",
    LEDGER_REASON_PROMO: "Активация промокода",
    LEDGER_REASON_OPENING_BALANCE: "Стартовый остаток после внедрения журнала",
}


class LedgerOperationConflict(ValueError):
    """Один идемпотентный ключ был использован для разных операций."""


@dataclass(frozen=True)
class LedgerWriteResult:
    """Результат записи операции для безопасной идемпотентной интеграции.

    ``created`` показывает, была ли операция добавлена в этой транзакции.
    Баланс меняют только при ``created=True``. Это важно для повторной
    доставки webhook-а одного и того же платежа.
    """

    entry: BalanceLedger
    created: bool


@dataclass(frozen=True)
class LedgerOverview:
    """Сводные показатели для первого экрана журнала."""

    total_balance: Decimal
    users_with_balance: int
    total_users: int
    movements_count: int


@dataclass(frozen=True)
class BalanceUserRow:
    """Пользователь и его текущий внутренний баланс."""

    user_id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    balance: Decimal


@dataclass(frozen=True)
class LedgerHistoryRow:
    """Запись журнала вместе с безопасным минимумом данных пользователя."""

    ledger_id: int
    user_id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    amount: Decimal
    balance_after: Decimal | None
    reason: str
    description: str | None
    reference_type: str | None
    reference_id: str | None
    operation_key: str | None
    created_at: datetime


@dataclass(frozen=True)
class LedgerPage:
    """Одна страница результатов для inline-интерфейса."""

    items: tuple
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, ceil(self.total / self.page_size))

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page + 1 < self.total_pages


def ledger_reason_label(reason: str) -> str:
    """Вернуть понятное название причины, не раскрывая технические ключи."""
    return LEDGER_REASON_LABELS.get(reason, "Движение баланса")


def _money(value: Decimal | float | int | str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Сумма операции должна быть числом") from exc
    if not amount.is_finite() or amount == 0:
        raise ValueError("Сумма операции должна быть ненулевой")
    return amount


def _balance_value(value: Decimal | float | int | str) -> Decimal:
    """Преобразовать сохранённый остаток: ноль здесь допустим."""
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Остаток баланса должен быть числом") from exc
    if not amount.is_finite():
        raise ValueError("Остаток баланса должен быть конечным числом")
    return amount


def _optional_text(value: str | int | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    if len(result) > max_length:
        raise ValueError(f"Поле {field} не может быть длиннее {max_length} символов")
    return result


def _operation_key(namespace: str, *parts: str) -> str:
    """Получить короткий ключ, даже если внешний идентификатор очень длинный."""
    source = "\x1f".join((namespace, *parts)).encode("utf-8")
    return f"{namespace}:{sha256(source).hexdigest()}"


def _normalize_page(page: int, page_size: int) -> tuple[int, int]:
    try:
        page = int(page)
        page_size = int(page_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректная страница") from exc
    if page < 0:
        raise ValueError("Некорректная страница")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"Размер страницы должен быть от 1 до {MAX_PAGE_SIZE}")
    return page, page_size


def _same_operation(
    entry: BalanceLedger,
    *,
    user_id: int,
    amount: Decimal,
    reason: str,
) -> bool:
    return (
        entry.user_id == user_id
        and Decimal(str(entry.amount)) == amount
        and entry.reason == reason
    )


async def _get_by_operation_key(session: AsyncSession, operation_key: str) -> BalanceLedger | None:
    result = await session.execute(
        select(BalanceLedger).where(BalanceLedger.operation_key == operation_key)
    )
    return result.scalar_one_or_none()


async def record_balance_movement(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    reason: str,
    description: str | None = None,
    reference_type: str | None = None,
    reference_id: str | int | None = None,
    operation_key: str | None = None,
    balance_after: Decimal | float | int | str | None = None,
) -> LedgerWriteResult:
    """Добавить операцию в журнал без самостоятельного ``commit``.

    При повторном вызове с тем же ``operation_key`` возвращается результат с
    ``created=False``, если пользователь, сумма и причина совпадают.  Баланс
    нужно менять только когда ``created=True``. Иначе выбрасывается
    ``LedgerOperationConflict``: это защищает от случайного повторного
    использования одного ключа для другой операции.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id должен быть положительным целым числом")

    amount_decimal = _money(amount)
    reason_value = _optional_text(reason, "reason", 100)
    if not reason_value:
        raise ValueError("Не указана причина операции")
    description_value = _optional_text(description, "description", 10_000)
    reference_type_value = _optional_text(reference_type, "reference_type", 50)
    reference_id_value = _optional_text(reference_id, "reference_id", 255)
    operation_key_value = _optional_text(operation_key, "operation_key", 255)
    balance_after_decimal = (
        None if balance_after is None else _balance_value(balance_after)
    )

    if operation_key_value:
        existing = await _get_by_operation_key(session, operation_key_value)
        if existing:
            if _same_operation(
                existing,
                user_id=user_id,
                amount=amount_decimal,
                reason=reason_value,
            ):
                return LedgerWriteResult(entry=existing, created=False)
            raise LedgerOperationConflict(
                "Идемпотентный ключ уже использован для другой операции"
            )

    entry = BalanceLedger(
        user_id=user_id,
        amount=amount_decimal,
        balance_after=balance_after_decimal,
        reason=reason_value,
        description=description_value,
        reference_type=reference_type_value,
        reference_id=reference_id_value,
        operation_key=operation_key_value,
    )

    # Savepoint сохраняет рабочую сессию после гонки двух одинаковых webhook-ов.
    # Внешняя транзакция и изменение User.balance при этом остаются под контролем
    # вызывающего кода.
    try:
        async with session.begin_nested():
            session.add(entry)
            await session.flush()
    except IntegrityError:
        if not operation_key_value:
            raise
        existing = await _get_by_operation_key(session, operation_key_value)
        if existing:
            if _same_operation(
                existing,
                user_id=user_id,
                amount=amount_decimal,
                reason=reason_value,
            ):
                return LedgerWriteResult(entry=existing, created=False)
            raise LedgerOperationConflict(
                "Идемпотентный ключ уже использован для другой операции"
            )
        raise

    return LedgerWriteResult(entry=entry, created=True)


async def record_topup(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    provider: str,
    payment_id: str | int | None = None,
    balance_after: Decimal | float | int | str | None = None,
) -> LedgerWriteResult:
    """Подготовленная точка интеграции для успешного пополнения."""
    provider_value = _optional_text(provider, "provider", 50)
    if not provider_value:
        raise ValueError("Не указан платёжный провайдер")
    payment_id_value = _optional_text(payment_id, "payment_id", 255)
    return await record_balance_movement(
        session,
        user_id=user_id,
        amount=abs(_money(amount)),
        reason=LEDGER_REASON_TOPUP,
        description=f"Пополнение через {provider_value}",
        reference_type="payment",
        reference_id=payment_id_value,
        operation_key=(
            _operation_key("topup", provider_value, payment_id_value)
            if payment_id_value
            else None
        ),
        balance_after=balance_after,
    )


async def record_purchase(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    order_id: int | str,
    balance_after: Decimal | float | int | str | None = None,
) -> LedgerWriteResult:
    """Подготовленная точка интеграции для оплаты заказа с баланса."""
    order_id_value = _optional_text(order_id, "order_id", 255)
    if not order_id_value:
        raise ValueError("Не указан заказ")
    return await record_balance_movement(
        session,
        user_id=user_id,
        amount=-abs(_money(amount)),
        reason=LEDGER_REASON_PURCHASE,
        description=f"Оплата заказа №{order_id_value}",
        reference_type="order",
        reference_id=order_id_value,
        operation_key=_operation_key("purchase", order_id_value),
        balance_after=balance_after,
    )


async def record_transfer(
    session: AsyncSession,
    *,
    sender_id: int,
    recipient_id: int,
    amount: Decimal | float | int | str,
    transfer_id: int | str,
    sender_balance_after: Decimal | float | int | str | None = None,
    recipient_balance_after: Decimal | float | int | str | None = None,
    message: str | None = None,
) -> tuple[LedgerWriteResult, LedgerWriteResult]:
    """Подготовленная точка интеграции для перевода между балансами."""
    if sender_id == recipient_id:
        raise ValueError("Нельзя перевести средства самому себе")
    transfer_id_value = _optional_text(transfer_id, "transfer_id", 255)
    if not transfer_id_value:
        raise ValueError("Не указан идентификатор перевода")
    transfer_amount = abs(_money(amount))
    message_value = _optional_text(message, "message", 10_000)
    suffix = f". {message_value}" if message_value else ""

    outgoing = await record_balance_movement(
        session,
        user_id=sender_id,
        amount=-transfer_amount,
        reason=LEDGER_REASON_TRANSFER_OUT,
        description=f"Перевод #{transfer_id_value}{suffix}",
        reference_type="balance_transfer",
        reference_id=transfer_id_value,
        operation_key=_operation_key("transfer_out", transfer_id_value),
        balance_after=sender_balance_after,
    )
    incoming = await record_balance_movement(
        session,
        user_id=recipient_id,
        amount=transfer_amount,
        reason=LEDGER_REASON_TRANSFER_IN,
        description=f"Перевод #{transfer_id_value}{suffix}",
        reference_type="balance_transfer",
        reference_id=transfer_id_value,
        operation_key=_operation_key("transfer_in", transfer_id_value),
        balance_after=recipient_balance_after,
    )
    return outgoing, incoming


async def record_referral_reward(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    order_id: int | str,
    balance_after: Decimal | float | int | str | None = None,
) -> LedgerWriteResult:
    """Подготовленная точка интеграции для реферальной награды."""
    order_id_value = _optional_text(order_id, "order_id", 255)
    if not order_id_value:
        raise ValueError("Не указан заказ")
    return await record_balance_movement(
        session,
        user_id=user_id,
        amount=abs(_money(amount)),
        reason=LEDGER_REASON_REFERRAL_REWARD,
        description=f"Награда за заказ №{order_id_value}",
        reference_type="order",
        reference_id=order_id_value,
        operation_key=_operation_key("referral_reward", str(user_id), order_id_value),
        balance_after=balance_after,
    )


async def record_cashback(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    order_id: int | str,
    balance_after: Decimal | float | int | str | None = None,
) -> LedgerWriteResult:
    """Подготовленная точка интеграции для реферального кешбэка."""
    order_id_value = _optional_text(order_id, "order_id", 255)
    if not order_id_value:
        raise ValueError("Не указан заказ")
    return await record_balance_movement(
        session,
        user_id=user_id,
        amount=abs(_money(amount)),
        reason=LEDGER_REASON_CASHBACK,
        description=f"Кешбэк за заказ №{order_id_value}",
        reference_type="order",
        reference_id=order_id_value,
        operation_key=_operation_key("cashback", str(user_id), order_id_value),
        balance_after=balance_after,
    )


async def record_manual_adjustment(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    adjustment_id: int | str,
    description: str | None = None,
    balance_after: Decimal | float | int | str | None = None,
    is_self_topup: bool = False,
) -> LedgerWriteResult:
    """Подготовленная точка интеграции для ручной корректировки администратором."""
    adjustment_id_value = _optional_text(adjustment_id, "adjustment_id", 255)
    if not adjustment_id_value:
        raise ValueError("Не указан идентификатор корректировки")
    return await record_balance_movement(
        session,
        user_id=user_id,
        amount=_money(amount),
        reason=(
            LEDGER_REASON_SELF_TOPUP
            if is_self_topup
            else LEDGER_REASON_MANUAL_ADJUSTMENT
        ),
        description=description or "Ручная корректировка баланса",
        reference_type="balance_adjustment",
        reference_id=adjustment_id_value,
        operation_key=_operation_key("adjustment", adjustment_id_value),
        balance_after=balance_after,
    )


async def record_refund(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal | float | int | str,
    order_id: int | str,
    balance_after: Decimal | float | int | str | None = None,
) -> LedgerWriteResult:
    """Подготовленная точка интеграции для возврата на внутренний баланс."""
    order_id_value = _optional_text(order_id, "order_id", 255)
    if not order_id_value:
        raise ValueError("Не указан заказ")
    return await record_balance_movement(
        session,
        user_id=user_id,
        amount=abs(_money(amount)),
        reason=LEDGER_REASON_REFUND,
        description=f"Возврат по заказу №{order_id_value}",
        reference_type="order",
        reference_id=order_id_value,
        operation_key=_operation_key("refund", str(user_id), order_id_value),
        balance_after=balance_after,
    )


async def get_ledger_overview(session: AsyncSession) -> LedgerOverview:
    """Получить общую сумму текущих балансов и число записей журнала."""
    total_balance, users_with_balance, total_users, movements_count = (
        await session.execute(
            select(
                func.coalesce(func.sum(User.balance), 0),
                func.count(User.id).filter(User.balance > 0),
                func.count(User.id),
                select(func.count(BalanceLedger.id)).scalar_subquery(),
            )
        )
    ).one()
    return LedgerOverview(
        total_balance=Decimal(str(total_balance or 0)),
        users_with_balance=int(users_with_balance or 0),
        total_users=int(total_users or 0),
        movements_count=int(movements_count or 0),
    )


async def list_user_balances(
    session: AsyncSession,
    *,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    nonzero_only: bool = False,
) -> LedgerPage:
    """Получить разбивку балансов, при необходимости исключив нулевые."""
    page, page_size = _normalize_page(page, page_size)
    filters = (User.balance != 0,) if nonzero_only else ()
    total = int(
        (await session.scalar(select(func.count(User.id)).where(*filters))) or 0
    )
    page = min(page, max(0, ceil(total / page_size) - 1))
    rows = (
        await session.execute(
            select(User)
            .where(*filters)
            .order_by(User.balance.desc(), User.id.asc())
            .offset(page * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    items = tuple(
        BalanceUserRow(
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            balance=Decimal(str(user.balance or 0)),
        )
        for user in rows
    )
    return LedgerPage(items=items, page=page, page_size=page_size, total=total)


async def get_user_balance(session: AsyncSession, user_id: int) -> BalanceUserRow | None:
    """Получить пользователя для подробного просмотра его баланса."""
    user = await session.get(User, user_id)
    if not user:
        return None
    return BalanceUserRow(
        user_id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        balance=Decimal(str(user.balance or 0)),
    )


async def list_ledger_history(
    session: AsyncSession,
    *,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    user_id: int | None = None,
) -> LedgerPage:
    """Получить историю движений, при необходимости только одного пользователя."""
    page, page_size = _normalize_page(page, page_size)
    filters: Iterable = ()
    if user_id is not None:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id должен быть положительным целым числом")
        filters = (BalanceLedger.user_id == user_id,)

    total = int(
        (await session.scalar(select(func.count(BalanceLedger.id)).where(*filters))) or 0
    )
    page = min(page, max(0, ceil(total / page_size) - 1))
    rows = await session.execute(
        select(BalanceLedger, User)
        .join(User, User.id == BalanceLedger.user_id)
        .where(*filters)
        .order_by(BalanceLedger.created_at.desc(), BalanceLedger.id.desc())
        .offset(page * page_size)
        .limit(page_size)
    )
    items = tuple(
        LedgerHistoryRow(
            ledger_id=entry.id,
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            amount=Decimal(str(entry.amount)),
            balance_after=(
                Decimal(str(entry.balance_after))
                if entry.balance_after is not None
                else None
            ),
            reason=entry.reason,
            description=entry.description,
            reference_type=entry.reference_type,
            reference_id=entry.reference_id,
            operation_key=entry.operation_key,
            created_at=entry.created_at,
        )
        for entry, user in rows.all()
    )
    return LedgerPage(items=items, page=page, page_size=page_size, total=total)
