"""Единая транзакционная логика оплаты и выдачи заказов.

Все функции этого модуля изменяют только текущую SQLAlchemy-сессию. Коммит
выполняет вызывающий код ровно один раз, поэтому несколько заказов в корзине
оплачиваются атомарно.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
import logging
from typing import Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Account, Order, OrderBatch, Payment, Product, ReferralTransaction, User
from utils.ledger import (
    record_cashback,
    record_purchase,
    record_referral_reward,
    record_topup,
)
from utils.money import ZERO, money
from utils.payments import (
    PaymentService,
    ProviderCreationError,
    ProviderInvoice,
    ProviderInvoiceStatus,
)
from utils.referral_settings import get_referral_program_config
from utils.stars_catalog import VIRTUAL_DELIVERY_TYPES


logger = logging.getLogger(__name__)


PENDING_ORDER_STATUS = "ОЖИДАЕТ ОПЛАТЫ"
COMPLETED_ORDER_STATUS = "ВЫПОЛНЕНО"
CANCELLED_ORDER_STATUS = "ОТМЕНЕНО"
PENDING_PAYMENT_STATUS = "PENDING"
STARS_AUTHORIZED_PAYMENT_STATUS = "STARS_AUTHORIZED"
SUCCESS_PAYMENT_STATUS = "SUCCESS"


def _legacy_stars_amount(rubles: Decimal | float | int | str) -> int:
    """Курс старых счетов, созданных до появления provider_amount."""
    return max(
        1,
        int((Decimal(str(rubles)) / Decimal("2.3")).to_integral_value(rounding=ROUND_UP)),
    )


def _stored_stars_amount(payment: Payment) -> int:
    """Получить число Stars, зафиксированное при выпуске счёта."""
    value = getattr(payment, "provider_amount", None)
    if isinstance(value, int) and value > 0:
        return value
    return _legacy_stars_amount(payment.amount)


FAILED_PAYMENT_STATUS = "FAILED"
# ``payment_url`` is the only persisted field available without a migration to
# record that a remote create request may already have reached the provider.
# It is never presented to a customer: a usable invoice always requires a real
# provider payment_id as well. This marker prevents a retry after a timeout
# from creating a second CryptoBot invoice before reconciliation/TTL.
EXTERNAL_INVOICE_RECONCILIATION_MARKER = "internal:awaiting-provider-reconciliation"

# Stars must remain reserved once Telegram has accepted pre-checkout: a
# successful_payment update can be in flight even if the user closes the UI.
# External invoices are also kept reserved until the provider reports a final
# failure.  Otherwise an old link could be paid after the goods were returned
# to the warehouse or bought from the balance.
ACTIVE_PAYMENT_STATUSES = (
    PENDING_PAYMENT_STATUS,
    STARS_AUTHORIZED_PAYMENT_STATUS,
)
EXTERNAL_PAYMENT_METHODS = frozenset({"yookassa", "yoomoney", "heleket", "lava", "cryptobot"})


class CheckoutError(Exception):
    """Ожидаемая ошибка оплаты, безопасная для показа пользователю."""


@dataclass
class CompletedOrder:
    order: Order
    accounts: list[Account]
    already_completed: bool = False


@dataclass(frozen=True)
class ExternalPaymentReconciliation:
    """One payment settled by the periodic provider reconciliation task."""

    payment_id: int
    completed_order: CompletedOrder | None = None
    completed_orders: list[CompletedOrder] | None = None
    topup_user: User | None = None
    topup_amount: Decimal | None = None


def _same_money(left: Decimal | float | int | str, right: Decimal | float | int | str) -> bool:
    """Сравнить суммы без обычной ошибки двоичных float."""
    try:
        return Decimal(str(left)).quantize(Decimal("0.01")) == Decimal(
            str(right)
        ).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False


async def _locked_user(session: AsyncSession, user_id: int) -> User:
    result = await session.execute(
        select(User)
        .where(User.id == user_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        raise CheckoutError("Пользователь не найден")
    return user


async def _locked_order_user_chain(
    session: AsyncSession, buyer_id: int
) -> tuple[User, dict[int, User]]:
    """Lock a buyer and its possible two-level referrer chain by user ID.

    A balance transfer locks both participants in ascending ``users.id``
    order.  Order completion must do exactly the same *before* locking an
    order, payment or account; otherwise a purchase by A that pays a referral
    reward to B can deadlock with a transfer A -> B.  ``referred_by`` is
    assigned only when a user is created and is not editable in the
    application, so the short read used to build the chain is stable in normal
    operation.  The rows are still re-read with ``FOR UPDATE`` immediately
    afterwards and supplied to the referral credit routine.
    """
    result = await session.execute(
        select(User.id, User.referred_by).where(User.id == buyer_id)
    )
    buyer_row = result.one_or_none()
    if buyer_row is None:
        raise CheckoutError("Пользователь не найден")

    chain_ids = {buyer_id}
    direct_referrer_id = buyer_row.referred_by
    second_referrer_id: int | None = None
    if direct_referrer_id and direct_referrer_id != buyer_id:
        chain_ids.add(direct_referrer_id)
        result = await session.execute(
            select(User.referred_by).where(User.id == direct_referrer_id)
        )
        second_referrer_id = result.scalar_one_or_none()
        if (
            second_referrer_id
            and second_referrer_id not in chain_ids
        ):
            chain_ids.add(second_referrer_id)

    result = await session.execute(
        select(User)
        .where(User.id.in_(chain_ids))
        .order_by(User.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    users = {user.id: user for user in result.scalars()}
    buyer = users.get(buyer_id)
    if buyer is None:
        raise CheckoutError("Пользователь не найден")
    # A direct referral is immutable in the public application.  If a manual
    # database edit changed it concurrently, fail safely rather than acquire
    # an additional row out of order halfway through a financial transaction.
    if buyer.referred_by != direct_referrer_id:
        raise CheckoutError(
            "Структура рефералов изменилась. Повторите оплату."
        )
    if direct_referrer_id:
        direct_referrer = users.get(direct_referrer_id)
        if (
            direct_referrer is None
            or direct_referrer.referred_by != second_referrer_id
        ):
            raise CheckoutError(
                "Структура рефералов изменилась. Повторите оплату."
            )
    return buyer, users


async def _locked_order(session: AsyncSession, order_id: int) -> Order:
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if not order:
        raise CheckoutError("Заказ не найден")
    return order


async def _locked_batch(session: AsyncSession, batch_id: int) -> OrderBatch:
    result = await session.execute(
        select(OrderBatch)
        .where(OrderBatch.id == batch_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    batch = result.scalar_one_or_none()
    if not batch:
        raise CheckoutError("Оформление не найдено")
    return batch


async def _locked_batch_orders(session: AsyncSession, batch_id: int) -> list[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.batch_id == batch_id)
        .order_by(Order.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    orders = list(result.scalars().all())
    if not orders:
        raise CheckoutError("В оформлении нет заказов")
    return orders


async def _complete_batch_locked(
    session: AsyncSession,
    batch: OrderBatch,
    payment: Payment,
    buyer: User,
    locked_users: dict[int, User],
    *,
    provider_payment_id: str | None = None,
) -> list[CompletedOrder]:
    """Complete all child orders after one validated payment."""
    orders = await _locked_batch_orders(session, batch.id)
    payment_id = provider_payment_id or payment.payment_id
    if batch.status in {"PAID", "PARTIAL", "COMPLETED"}:
        if payment.status != SUCCESS_PAYMENT_STATUS or batch.payment_id != payment_id:
            raise CheckoutError("Оформление уже оплачено другим платежом")
        return [
            CompletedOrder(order=order, accounts=await _locked_order_accounts(session, order), already_completed=True)
            for order in orders
        ]
    if batch.status != "PENDING_PAYMENT" or payment.status not in ACTIVE_PAYMENT_STATUSES:
        raise CheckoutError("Оформление уже отменено или недоступно для оплаты")

    accounts_by_order: dict[int, list[Account]] = {}
    for order in orders:
        if order.status != PENDING_ORDER_STATUS:
            raise CheckoutError("В оформлении есть недоступный заказ")
        accounts_by_order[order.id] = await _locked_order_accounts(session, order)

    completed: list[CompletedOrder] = []
    now = datetime.now()
    for order in orders:
        await _credit_referral(session, order, buyer, locked_users)
        order.status = COMPLETED_ORDER_STATUS
        order.payment_method = payment.payment_method
        order.payment_id = payment_id
        order.paid_at = now
        order.completed_at = now
        order.reserved_until = None
        completed.append(CompletedOrder(order=order, accounts=accounts_by_order[order.id]))

    payment.status = SUCCESS_PAYMENT_STATUS
    payment.payment_id = payment_id
    payment.completed_at = now
    batch.status = "PAID"
    batch.payment_method = payment.payment_method
    batch.payment_id = payment_id
    batch.paid_at = now
    batch.reserved_until = None
    return completed


async def _locked_order_accounts(session: AsyncSession, order: Order) -> list[Account]:
    # Telegram Stars are a virtual, unlimited product.  There is deliberately
    # no Account row to reserve or release; fulfillment is performed by the
    # Fragment client after the payment transaction commits.
    delivery_type = await session.scalar(
        select(Product.delivery_type).where(Product.id == order.product_id)
    )
    if delivery_type in VIRTUAL_DELIVERY_TYPES:
        return []

    result = await session.execute(
        select(Account)
        .where(Account.order_id == order.id)
        .order_by(Account.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    accounts = result.scalars().all()

    # После оплаты нельзя подменять товар другими свободными аккаунтами: если
    # бронь уже снята, нужен ручной возврат/разбор, а не новая выдача.
    if len(accounts) != order.quantity:
        raise CheckoutError(
            "Резерв товара для заказа не найден. Обратитесь в поддержку."
        )
    if any(not account.is_sold or account.is_blocked for account in accounts):
        raise CheckoutError("Товар для заказа недоступен. Обратитесь в поддержку.")
    return accounts


async def _locked_active_order_payments(
    session: AsyncSession, order_ids: Iterable[int]
) -> list[Payment]:
    """Lock active invoices after User and Order rows have been locked."""
    unique_ids = sorted(set(order_ids))
    if not unique_ids:
        return []
    result = await session.execute(
        select(Payment)
        .where(
            Payment.order_id.in_(unique_ids),
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        )
        .order_by(Payment.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return result.scalars().all()


async def _credit_referral(
    session: AsyncSession,
    order: Order,
    buyer: User,
    locked_users: dict[int, User],
) -> None:
    """Начислить реферальную комиссию один раз в рамках транзакции заказа."""
    if not buyer.referred_by:
        return

    config = await get_referral_program_config(session)
    if config.reward_percent <= 0:
        return

    # При настройке «первый платёж» награда начисляется только за первый
    # успешно оплаченный заказ приглашённого пользователя.
    if config.condition == "first":
        previous = await session.execute(
            select(ReferralTransaction.id)
            .where(ReferralTransaction.referred_id == buyer.id)
            .limit(1)
        )
        if previous.scalar_one_or_none() is not None:
            return

    exists = await session.execute(
        select(ReferralTransaction.id).where(
            ReferralTransaction.order_id == order.id,
            ReferralTransaction.referrer_id == buyer.referred_by,
        )
    )
    if exists.scalar_one_or_none() is not None:
        return

    # Первый уровень — пользователь, по чьей ссылке зарегистрировался
    # покупатель. Второй — тот, кто пригласил этого пользователя. Так у
    # каждого участника формируется его личная двухуровневая структура:
    # прямые друзья и друзья его друзей. Циклы исключены.
    referrer_ids = [buyer.referred_by]
    direct_referrer = locked_users.get(buyer.referred_by)
    if direct_referrer is None:
        raise CheckoutError("Реферер не найден")
    if (
        config.levels == 2
        and direct_referrer.referred_by
        and direct_referrer.referred_by != buyer.id
    ):
        referrer_ids.append(direct_referrer.referred_by)

    commission = money(
        money(order.total_amount)
        * Decimal(str(config.reward_percent))
        / Decimal("100")
    )
    # A sub-kopeck configured percentage must not create an empty financial
    # row or make the first-payment condition look fulfilled.
    if commission <= ZERO:
        return
    cashback = (
        money(
            commission
            * Decimal(str(config.cashback_percent))
            / Decimal("100")
        )
        if config.cashback_enabled
        else ZERO
    )
    for referrer_id in dict.fromkeys(referrer_ids):
        referrer = locked_users.get(referrer_id)
        if referrer is None:
            # Do not lock a newly discovered user row here: that would break
            # the global ascending lock order and can deadlock a transfer.
            raise CheckoutError(
                "Структура рефералов изменилась. Повторите оплату."
            )
        balance_after = money(referrer.balance) + commission
        ledger_result = await record_referral_reward(
            session,
            user_id=referrer.id,
            amount=commission,
            order_id=order.id,
            balance_after=balance_after,
        )
        if not ledger_result.created:
            raise CheckoutError("Реферальная награда уже была учтена")
        referrer.balance = balance_after
        session.add(
            ReferralTransaction(
                referrer_id=referrer.id,
                referred_id=buyer.id,
                order_id=order.id,
                amount=money(order.total_amount),
                commission=commission,
                cashback=cashback if referrer.id == direct_referrer.id else ZERO,
            )
        )

    if cashback > ZERO:
        balance_after = money(buyer.balance) + cashback
        ledger_result = await record_cashback(
            session,
            user_id=buyer.id,
            amount=cashback,
            order_id=order.id,
            balance_after=balance_after,
        )
        if not ledger_result.created:
            raise CheckoutError("Реферальный кешбэк уже был учтён")
        buyer.balance = balance_after


async def _complete_order_locked(
    session: AsyncSession,
    order: Order,
    payment: Payment,
    buyer: User,
    locked_users: dict[int, User],
) -> CompletedOrder:
    """Перевести заблокированный ожидающий заказ в выполненный."""
    if order.status == COMPLETED_ORDER_STATUS:
        if (
            payment.status != SUCCESS_PAYMENT_STATUS
            or order.payment_method != payment.payment_method
            or order.payment_id != payment.payment_id
        ):
            raise CheckoutError("Заказ уже оплачен другим платежом")
        accounts = await _locked_order_accounts(session, order)
        return CompletedOrder(order=order, accounts=accounts, already_completed=True)
    if order.status != PENDING_ORDER_STATUS:
        raise CheckoutError("Заказ уже отменен или недоступен для оплаты")

    accounts = await _locked_order_accounts(session, order)
    await _credit_referral(session, order, buyer, locked_users)

    order.status = COMPLETED_ORDER_STATUS
    order.payment_method = payment.payment_method
    order.payment_id = payment.payment_id
    order.paid_at = datetime.now()
    order.completed_at = datetime.now()
    order.reserved_until = None

    payment.status = SUCCESS_PAYMENT_STATUS
    payment.completed_at = datetime.now()
    return CompletedOrder(order=order, accounts=accounts)


async def complete_external_order_payment(
    session: AsyncSession,
    *,
    payment_id: int,
    provider_payment_id: str,
    payment_method: str,
    external_order_id: str,
    provider_amount: Decimal | float | int | str,
) -> CompletedOrder:
    """Идемпотентно обработать подтвержденную внешним провайдером оплату."""
    preview_result = await session.execute(
        select(Payment.user_id, Payment.order_id).where(Payment.id == payment_id)
    )
    payment_preview = preview_result.one_or_none()
    if not payment_preview or not payment_preview.order_id:
        raise CheckoutError("Платеж не найден")
    # Во всех потоках заказа сначала блокируются User и Order, а только затем
    # Payment. Единый порядок не дает webhook и повторному клику взаимно ждать
    # друг друга в PostgreSQL.
    buyer, locked_users = await _locked_order_user_chain(
        session, payment_preview.user_id
    )
    order = await _locked_order(session, payment_preview.order_id)
    result = await session.execute(
        select(Payment)
        .where(Payment.id == payment_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    payment = result.scalar_one_or_none()
    if (
        not payment
        or payment.order_id != order.id
        or payment.user_id != buyer.id
        or payment.payment_method != payment_method
        or payment.external_order_id != external_order_id
    ):
        raise CheckoutError("Платеж не найден")
    if not provider_payment_id or payment.payment_id not in {None, provider_payment_id}:
        raise CheckoutError("Идентификатор платежа не совпадает")
    if not _same_money(payment.amount, provider_amount):
        raise CheckoutError("Сумма платежа не совпадает")
    if order.user_id != buyer.id:
        raise CheckoutError("Платеж привязан к неверному пользователю")
    # Webhook can arrive between invoice creation at the provider and storing
    # its ID locally.  Claiming an empty ID under the row lock makes that race
    # safe; the unique (payment_method, payment_id) index rejects reuse.
    if payment.payment_id is None:
        payment.payment_id = provider_payment_id
    if payment.status == SUCCESS_PAYMENT_STATUS:
        accounts = await _locked_order_accounts(session, order)
        if (
            order.status != COMPLETED_ORDER_STATUS
            or order.payment_method != payment.payment_method
            or order.payment_id != payment.payment_id
        ):
            raise CheckoutError("Состояние платежа и заказа не совпадает")
        return CompletedOrder(order=order, accounts=accounts, already_completed=True)
    if payment.status != PENDING_PAYMENT_STATUS:
        raise CheckoutError("Платеж недоступен для подтверждения")
    return await _complete_order_locked(
        session, order, payment, buyer, locked_users
    )


async def complete_balance_topup(
    session: AsyncSession,
    *,
    payment_id: int,
    provider_payment_id: str,
    payment_method: str,
    external_order_id: str,
    provider_amount: Decimal | float | int | str,
) -> User:
    """Идемпотентно зачислить уже подтвержденное внешнее пополнение.

    The provider invoice ID is checked again after the row lock.  The webhook
    did the same comparison before entering this transaction, but repeating it
    here prevents a stale pre-check from ever crediting another invoice.
    """
    preview_result = await session.execute(
        select(Payment.user_id, Payment.order_id).where(Payment.id == payment_id)
    )
    payment_preview = preview_result.one_or_none()
    if not payment_preview or payment_preview.order_id:
        raise CheckoutError("Платеж пополнения не найден")
    user = await _locked_user(session, payment_preview.user_id)
    result = await session.execute(
        select(Payment)
        .where(Payment.id == payment_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    payment = result.scalar_one_or_none()
    if (
        not payment
        or payment.order_id
        or payment.user_id != user.id
        or payment.payment_method != payment_method
        or payment.external_order_id != external_order_id
    ):
        raise CheckoutError("Платеж пополнения не найден")
    if not provider_payment_id or payment.payment_id not in {None, provider_payment_id}:
        raise CheckoutError("Идентификатор платежа не совпадает")
    if not _same_money(payment.amount, provider_amount):
        raise CheckoutError("Сумма платежа не совпадает")

    if payment.payment_id is None:
        payment.payment_id = provider_payment_id

    if payment.status == SUCCESS_PAYMENT_STATUS:
        return user
    if payment.status != PENDING_PAYMENT_STATUS:
        raise CheckoutError("Платеж недоступен для подтверждения")

    balance_after = money(user.balance) + money(payment.amount)
    ledger_result = await record_topup(
        session,
        user_id=user.id,
        amount=payment.amount,
        provider=payment.payment_method,
        payment_id=provider_payment_id,
        balance_after=balance_after,
    )
    if not ledger_result.created:
        raise CheckoutError("Пополнение уже было учтено")
    user.balance = balance_after
    payment.status = SUCCESS_PAYMENT_STATUS
    payment.completed_at = datetime.now()
    return user


async def get_or_create_pending_external_payment(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal,
    payment_method: str,
    order_id: int | None = None,
    batch_id: int | None = None,
    allow_new_invoice: bool = True,
) -> Payment:
    """Создать один ожидающий внешний счет или вернуть ранее созданный.

    Счет сначала фиксируется в БД, а затем создается у провайдера. Это
    связывает webhook с конкретным пользователем/заказом еще до внешнего HTTP
    вызова и исключает дубли при повторном нажатии кнопки.
    """
    try:
        amount = money(amount)
    except ValueError as exc:
        raise CheckoutError("Некорректная сумма платежа") from exc
    if amount <= ZERO:
        raise CheckoutError("Бесплатная оплата недоступна")

    # Сначала сериализуем создание счета. Иначе два быстрых нажатия могут оба
    # увидеть пустую выборку и создать по одному счету у внешнего провайдера.
    if order_id is not None and batch_id is not None:
        raise CheckoutError("Платёж не может быть привязан к заказу и оформлению одновременно")
    if order_id is None and batch_id is None:
        await _locked_user(session, user_id)
        stmt = select(Payment).where(
            Payment.user_id == user_id,
            Payment.payment_method == payment_method,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
            Payment.order_id.is_(None),
            Payment.batch_id.is_(None),
        )
    elif batch_id is not None:
        batch_preview = await session.get(OrderBatch, batch_id)
        if not batch_preview:
            raise CheckoutError("Оформление не найдено")
        buyer = await _locked_user(session, batch_preview.user_id)
        if buyer.id != user_id:
            raise CheckoutError("Оформление не найдено")
        batch = await _locked_batch(session, batch_id)
        if batch.user_id != buyer.id or batch.status != "PENDING_PAYMENT":
            raise CheckoutError("Оформление уже отменено или недоступно для оплаты")
        if not _same_money(batch.total_amount, amount):
            raise CheckoutError("Сумма оформления изменилась")
        stmt = select(Payment).where(
            Payment.batch_id == batch_id,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        )
    else:
        # В пользовательских потоках блокировка User берется раньше Order. Это
        # совпадает с оплатой корзины и не дает взаимных блокировок.
        order_preview = await session.get(Order, order_id)
        if not order_preview:
            raise CheckoutError("Заказ не найден")
        buyer = await _locked_user(session, order_preview.user_id)
        if buyer.id != user_id:
            raise CheckoutError("Заказ не найден")
        order = await _locked_order(session, order_id)
        if order.user_id != buyer.id:
            raise CheckoutError("Заказ не найден")
        if order.status != PENDING_ORDER_STATUS:
            raise CheckoutError("Заказ уже отменен или недоступен для оплаты")
        stmt = select(Payment).where(
            Payment.order_id == order_id,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        )

    result = await session.execute(
        stmt.order_by(Payment.id.desc())
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    existing_payments = result.scalars().all()
    if existing_payments:
        existing = existing_payments[0]
        if existing.payment_method != payment_method:
            raise CheckoutError(
                "Для заказа уже создан счет другим способом оплаты. "
                "Оплатите его или отмените заказ и создайте новый."
            )
        if not _same_money(existing.amount, amount):
            # A top-up for which the provider never returned an ID cannot be
            # reconciled further. After its local TTL, the remote invoice (if
            # it was accepted at all) has expired too, so it must not block a
            # customer from selecting a different amount forever.
            if (
                order_id is None
                and batch_id is None
                and existing.payment_id is None
                and _external_payment_creation_expired(existing)
            ):
                existing.status = FAILED_PAYMENT_STATUS
                existing.completed_at = datetime.now()
            elif order_id is None and batch_id is None:
                # Let the caller reconcile the still-active top-up first. It
                # may have expired at the provider even though its local row
                # was never cleaned up; returning it avoids an endless amount
                # mismatch that would prevent that safe cleanup.
                return existing
            else:
                raise CheckoutError("Сумма существующего платежа не совпадает")
        else:
            return existing

    if not allow_new_invoice:
        raise CheckoutError(
            "Этот способ оплаты отключен. Ранее выставленные счета продолжат "
            "обрабатываться автоматически."
        )

    # Только буквы, цифры, _ и -: это проходит ограничения Heleket и Lava.
    external_order_id = f"{payment_method}-{uuid4().hex}"
    payment = Payment(
        user_id=user_id,
        amount=amount,
        payment_method=payment_method,
        status=PENDING_PAYMENT_STATUS,
        order_id=order_id,
        batch_id=batch_id,
        external_order_id=external_order_id,
        idempotency_key=uuid4().hex,
        expires_at=(
            datetime.now()
            + timedelta(
                minutes=PaymentService.provider_invoice_lifetime_minutes(
                    payment_method
                )
            )
            if order_id is None
            else None
        ),
    )
    session.add(payment)
    await session.flush()
    return payment


def _external_payment_creation_expired(payment: Payment) -> bool:
    """True only when an invoice without a known provider ID is stale.

    A timeout during remote creation is ambiguous, so it is never failed at
    once.  Once its local reservation/TTL has elapsed, the provider invoice
    would also have expired (all create calls use the same setting) and a new
    attempt is safe. ``expires_at`` is used for balance top-ups; the fallback
    keeps old rows compatible while migrations are rolled out.
    """
    now = datetime.now()
    expires_at = getattr(payment, "expires_at", None)
    if expires_at is not None:
        return expires_at <= now
    created_at = getattr(payment, "created_at", None)
    if created_at is None:
        return False
    return created_at + timedelta(
        minutes=PaymentService.provider_invoice_lifetime_minutes(
            payment.payment_method
        )
    ) <= now


async def _bind_external_provider_invoice(
    session: AsyncSession,
    *,
    payment_record_id: int,
    payment_method: str,
    external_order_id: str,
    provider_payment_id: str | None,
    payment_url: str | None,
) -> Payment:
    """Persist a provider invoice only while its local PENDING row matches."""
    result = await session.execute(
        select(Payment)
        .where(Payment.id == payment_record_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    payment = result.scalar_one_or_none()
    if (
        not payment
        or payment.payment_method != payment_method
        or payment.external_order_id != external_order_id
        or payment.status != PENDING_PAYMENT_STATUS
    ):
        raise CheckoutError("Состояние платежа изменилось. Откройте заказ заново.")
    if provider_payment_id and payment.payment_id not in {
        None,
        provider_payment_id,
    }:
        raise CheckoutError("Идентификатор счёта платежного провайдера не совпадает")
    if provider_payment_id:
        payment.payment_id = provider_payment_id
    if payment_url:
        payment.payment_url = payment_url
    return payment


async def _mark_external_invoice_creation_attempted(
    session: AsyncSession,
    *,
    payment_record_id: int,
    payment_method: str,
    external_order_id: str,
) -> None:
    """Durably mark an ambiguous remote create request before sending it."""
    result = await session.execute(
        select(Payment)
        .where(Payment.id == payment_record_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    payment = result.scalar_one_or_none()
    if (
        not payment
        or payment.payment_method != payment_method
        or payment.external_order_id != external_order_id
        or payment.status != PENDING_PAYMENT_STATUS
        or payment.payment_id is not None
        or payment.payment_url is not None
    ):
        raise CheckoutError("Состояние платежа изменилось. Откройте заказ заново.")
    payment.payment_url = EXTERNAL_INVOICE_RECONCILIATION_MARKER


async def fail_pending_external_payment(
    session: AsyncSession,
    *,
    payment_record_id: int,
    payment_method: str,
    external_order_id: str,
    provider_payment_id: str | None = None,
) -> Order | None:
    """Закрыть подтверждённо неуспешный внешний платёж в одной транзакции.

    Для заказа это также снимает резерв. Вызов допустим лишь после явного
    отказа/истечения у провайдера или после безопасного локального TTL счёта,
    для которого мы так и не получили provider ID.
    """
    preview_result = await session.execute(
        select(Payment.user_id, Payment.order_id).where(Payment.id == payment_record_id)
    )
    preview = preview_result.one_or_none()
    if not preview:
        return None
    buyer = await _locked_user(session, preview.user_id)
    order = await _locked_order(session, preview.order_id) if preview.order_id else None
    result = await session.execute(
        select(Payment)
        .where(Payment.id == payment_record_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    payment = result.scalar_one_or_none()
    if (
        not payment
        or payment.user_id != buyer.id
        or payment.payment_method != payment_method
        or payment.external_order_id != external_order_id
        or (order is not None and payment.order_id != order.id)
    ):
        return None
    if provider_payment_id and payment.payment_id not in {
        None,
        provider_payment_id,
    }:
        return None
    if payment.status != PENDING_PAYMENT_STATUS:
        return order

    if provider_payment_id and payment.payment_id is None:
        payment.payment_id = provider_payment_id
    payment.status = FAILED_PAYMENT_STATUS
    payment.completed_at = datetime.now()
    if order is None or order.status != PENDING_ORDER_STATUS:
        return order
    # The current row is no longer active, therefore cancellation cannot
    # accidentally invalidate a still payable external link.
    return await cancel_pending_order(
        session, order.id, reconcile_provider=False
    )


async def _apply_provider_reconciliation(
    session: AsyncSession,
    *,
    payment_record_id: int,
    payment_method: str,
    external_order_id: str,
    provider_status: ProviderInvoiceStatus,
) -> ProviderInvoice | None:
    """Persist an observed provider state, returning a usable pending invoice."""
    state = provider_status.state
    provider_payment_id = provider_status.payment_id
    payment_url = provider_status.payment_url
    if state == "failed":
        await fail_pending_external_payment(
            session,
            payment_record_id=payment_record_id,
            payment_method=payment_method,
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
        )
        await session.commit()
        raise CheckoutError(
            "Срок действия счёта истёк или провайдер его отменил. "
            "Создайте оплату заново."
        )

    if provider_payment_id:
        await _bind_external_provider_invoice(
            session,
            payment_record_id=payment_record_id,
            payment_method=payment_method,
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
            payment_url=payment_url,
        )
        await session.commit()

    if state == "succeeded":
        # Never cancel/re-create a paid external invoice if its webhook is
        # delayed. Its immutable provider ID has already been stored above.
        raise CheckoutError(
            "Провайдер уже подтвердил оплату. Дождитесь автоматической выдачи товара."
        )
    if state == "pending":
        if provider_payment_id and payment_url:
            return ProviderInvoice(provider_payment_id, payment_url)
        raise CheckoutError(
            "Счёт уже создан и ожидает сверки. Повторите попытку через несколько минут; "
            "новый счёт не будет создан."
        )
    return None


async def resolve_pending_external_invoice(
    session: AsyncSession,
    *,
    payment_record_id: int,
    payment_method: str,
    description: str,
    allow_new_invoice: bool,
) -> ProviderInvoice:
    """Reconcile/create one external invoice without a DB transaction over HTTP.

    The local payment row is committed before this function is called. A
    process-local lock serializes rapid duplicate callbacks; remote creation
    is always preceded by status reconciliation and uses the same stable
    external_order_id. A timeout therefore leaves a safe PENDING row rather
    than either charging twice or returning reserved goods prematurely.
    """
    async with PaymentService.invoice_creation_lock(str(payment_record_id)):
        result = await session.execute(
            select(Payment)
            .where(Payment.id == payment_record_id)
            .execution_options(populate_existing=True)
        )
        payment = result.scalar_one_or_none()
        if (
            not payment
            or payment.payment_method != payment_method
            or payment.status != PENDING_PAYMENT_STATUS
            or not payment.external_order_id
        ):
            await session.rollback()
            raise CheckoutError("Платёж недоступен для создания счёта")
        if (
            payment.payment_id
            and payment.payment_url
            and payment.payment_url != EXTERNAL_INVOICE_RECONCILIATION_MARKER
        ):
            invoice = ProviderInvoice(payment.payment_id, payment.payment_url)
            await session.rollback()
            return invoice

        external_order_id = payment.external_order_id
        amount = payment.amount
        known_provider_payment_id = payment.payment_id
        creation_was_attempted = (
            payment.payment_url == EXTERNAL_INVOICE_RECONCILIATION_MARKER
        )
        expired_without_provider_id = (
            not known_provider_payment_id
            and payment_method != "yookassa"
            and _external_payment_creation_expired(payment)
        )
        # Do not leave an open database transaction while contacting a payment
        # API. All values needed for the request are copied above.
        await session.rollback()

        provider_status = await PaymentService.reconcile_provider_invoice(
            payment_method,
            external_order_id=external_order_id,
            provider_payment_id=known_provider_payment_id,
        )
        reconciled_invoice = await _apply_provider_reconciliation(
            session,
            payment_record_id=payment_record_id,
            payment_method=payment_method,
            external_order_id=external_order_id,
            provider_status=provider_status,
        )
        if reconciled_invoice:
            return reconciled_invoice

        if expired_without_provider_id:
            await fail_pending_external_payment(
                session,
                payment_record_id=payment_record_id,
                payment_method=payment_method,
                external_order_id=external_order_id,
            )
            await session.commit()
            raise CheckoutError(
                "Не удалось подтвердить создание старого счёта, его срок истёк. "
                "Создайте оплату заново."
            )
        if creation_was_attempted and payment_method != "yookassa":
            # A prior timeout may have reached the provider. Do not create a
            # second invoice (especially at CryptoBot, which lacks a request
            # idempotency header) until the existing attempt can be reconciled
            # or its TTL has elapsed.
            raise CheckoutError(
                "Создание счёта ещё проверяется. Повторите попытку через несколько "
                "минут: дубликат счёта не будет создан."
            )
        if not allow_new_invoice:
            raise CheckoutError(
                "Этот способ оплаты отключен. Ранее созданный счёт продолжает "
                "безопасно сверяться автоматически."
            )

        if not creation_was_attempted:
            # Persist this marker before remote HTTP. A process restart or a
            # second worker then follows the same reconciliation path instead
            # of issuing another potentially chargeable invoice.
            await _mark_external_invoice_creation_attempted(
                session,
                payment_record_id=payment_record_id,
                payment_method=payment_method,
                external_order_id=external_order_id,
            )
            await session.commit()

        try:
            invoice = await PaymentService.create_provider_invoice(
                payment_method,
                amount=money(amount),
                payment_record_id=payment_record_id,
                external_order_id=external_order_id,
                description=description,
            )
        except ProviderCreationError as exc:
            # A timeout may still have created the invoice. Reconcile one more
            # time before deciding whether it is safe to fail the local row.
            provider_status = await PaymentService.reconcile_provider_invoice(
                payment_method,
                external_order_id=external_order_id,
                provider_payment_id=known_provider_payment_id,
            )
            reconciled_invoice = await _apply_provider_reconciliation(
                session,
                payment_record_id=payment_record_id,
                payment_method=payment_method,
                external_order_id=external_order_id,
                provider_status=provider_status,
            )
            if reconciled_invoice:
                return reconciled_invoice
            if exc.definitive:
                await fail_pending_external_payment(
                    session,
                    payment_record_id=payment_record_id,
                    payment_method=payment_method,
                    external_order_id=external_order_id,
                )
                await session.commit()
                raise CheckoutError(
                    "Платёжная система отклонила создание счёта. Попробуйте другой способ оплаты."
                ) from exc
            raise CheckoutError(
                "Не удалось подтвердить создание счёта. Он будет повторно сверён "
                "при следующем открытии оплаты; дубликат не создаётся."
            ) from exc

        await _bind_external_provider_invoice(
            session,
            payment_record_id=payment_record_id,
            payment_method=payment_method,
            external_order_id=external_order_id,
            provider_payment_id=invoice.payment_id,
            payment_url=invoice.payment_url,
        )
        await session.commit()
        return invoice


async def reconcile_pending_external_payments(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[ExternalPaymentReconciliation]:
    """Finish externally verified invoices even when their webhook was lost.

    Provider HTTP requests deliberately happen before a row lock/transaction.
    The usual completion functions then re-read and lock User → Order →
    Payment, verify amount and currency again, and remain idempotent. A
    provider response that is merely unknown/pending never changes money or
    stock. Final failures are released with the same protected cancellation
    path as a signed webhook.
    """
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")

    snapshots_result = await session.execute(
        select(
            Payment.id,
            Payment.payment_method,
            Payment.external_order_id,
            Payment.payment_id,
        )
        .where(
            Payment.payment_method.in_(EXTERNAL_PAYMENT_METHODS),
            Payment.status == PENDING_PAYMENT_STATUS,
            Payment.external_order_id.is_not(None),
        )
        .order_by(Payment.created_at.asc(), Payment.id.asc())
        .limit(limit)
    )
    snapshots = snapshots_result.all()
    # Do not leave a transaction/connection open while external APIs respond.
    await session.rollback()

    settled: list[ExternalPaymentReconciliation] = []
    for payment_id, payment_method, external_order_id, provider_payment_id in snapshots:
        provider_status = await PaymentService.reconcile_provider_invoice(
            payment_method,
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
        )

        try:
            if provider_status.state == "pending":
                if provider_status.payment_id:
                    await _bind_external_provider_invoice(
                        session,
                        payment_record_id=payment_id,
                        payment_method=payment_method,
                        external_order_id=external_order_id,
                        provider_payment_id=provider_status.payment_id,
                        payment_url=provider_status.payment_url,
                    )
                    await session.commit()
                continue

            if provider_status.state == "failed":
                await fail_pending_external_payment(
                    session,
                    payment_record_id=payment_id,
                    payment_method=payment_method,
                    external_order_id=external_order_id,
                    provider_payment_id=provider_status.payment_id,
                )
                await session.commit()
                continue

            if provider_status.state != "succeeded":
                continue
            if (
                not provider_status.payment_id
                or provider_status.amount is None
                or provider_status.currency != "RUB"
            ):
                logger.error(
                    "Refusing to settle external payment %s: provider %s returned "
                    "incomplete or non-RUB final data",
                    payment_id,
                    payment_method,
                )
                continue

            preview = await session.execute(
                select(Payment.user_id, Payment.order_id, Payment.batch_id)
                .where(Payment.id == payment_id)
            )
            payment_preview = preview.one_or_none()
            if not payment_preview:
                await session.rollback()
                continue

            if payment_preview.order_id:
                completed_order = await complete_external_order_payment(
                    session,
                    payment_id=payment_id,
                    provider_payment_id=provider_status.payment_id,
                    payment_method=payment_method,
                    external_order_id=external_order_id,
                    provider_amount=provider_status.amount,
                )
                await session.commit()
                if not completed_order.already_completed:
                    settled.append(
                        ExternalPaymentReconciliation(
                            payment_id=payment_id,
                            completed_order=completed_order,
                        )
                    )
            elif payment_preview.batch_id:
                completed_orders = await complete_external_batch_payment(
                    session,
                    payment_id=payment_id,
                    provider_payment_id=provider_status.payment_id,
                    payment_method=payment_method,
                    external_order_id=external_order_id,
                    provider_amount=provider_status.amount,
                )
                await session.commit()
                if any(not item.already_completed for item in completed_orders):
                    settled.append(
                        ExternalPaymentReconciliation(
                            payment_id=payment_id,
                            completed_orders=completed_orders,
                        )
                    )
            else:
                topup_user = await complete_balance_topup(
                    session,
                    payment_id=payment_id,
                    provider_payment_id=provider_status.payment_id,
                    payment_method=payment_method,
                    external_order_id=external_order_id,
                    provider_amount=provider_status.amount,
                )
                await session.commit()
                settled.append(
                    ExternalPaymentReconciliation(
                        payment_id=payment_id,
                        topup_user=topup_user,
                        topup_amount=money(provider_status.amount),
                    )
                )
        except CheckoutError as exc:
            # A concurrent webhook may have finished the same row. It is
            # intentionally harmless; the next loop sees its final status.
            await session.rollback()
            logger.warning(
                "Could not settle reconciled external payment %s: %s",
                payment_id,
                exc,
            )
        except Exception:
            await session.rollback()
            logger.exception("External payment reconciliation failed for %s", payment_id)

    return settled


async def expire_pending_stars_topups(session: AsyncSession, *, limit: int = 100) -> int:
    """Expire only never-authorized Stars balance invoices after their TTL.

    Once Telegram accepts pre-checkout the invoice stays reserved: a
    ``successful_payment`` update may still arrive. This function therefore
    touches ``PENDING`` rows only, never ``STARS_AUTHORIZED``.
    """
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    now = datetime.now()
    ids = (
        await session.scalars(
            select(Payment.id)
            .where(
                Payment.payment_method == "stars_topup",
                Payment.status == PENDING_PAYMENT_STATUS,
                Payment.expires_at.is_not(None),
                Payment.expires_at <= now,
            )
            .order_by(Payment.expires_at.asc(), Payment.id.asc())
            .limit(limit)
        )
    ).all()
    expired = 0
    for payment_id in ids:
        payment = (
            await session.execute(
                select(Payment)
                .where(Payment.id == payment_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            payment
            and payment.payment_method == "stars_topup"
            and payment.status == PENDING_PAYMENT_STATUS
            and payment.expires_at is not None
            and payment.expires_at <= now
        ):
            payment.status = FAILED_PAYMENT_STATUS
            payment.completed_at = now
            expired += 1
    if expired:
        await session.commit()
    return expired


async def get_or_create_pending_stars_payment(
    session: AsyncSession,
    *,
    order_id: int,
    telegram_user_id: int,
    stars_amount: int,
) -> tuple[Payment, bool]:
    """Зафиксировать один счёт Telegram Stars для старого заказа."""
    if not isinstance(stars_amount, int) or stars_amount <= 0:
        raise CheckoutError("Некорректная сумма Telegram Stars")
    order_preview = await session.get(Order, order_id)
    if not order_preview:
        raise CheckoutError("Заказ не найден")
    buyer = await _locked_user(session, order_preview.user_id)
    if buyer.telegram_id != telegram_user_id:
        raise CheckoutError("Заказ не найден")
    order = await _locked_order(session, order_id)
    if order.user_id != buyer.id or order.status != PENDING_ORDER_STATUS:
        raise CheckoutError("Заказ уже отменен или недоступен для оплаты")
    if money(order.total_amount) <= ZERO:
        raise CheckoutError("Бесплатная оплата недоступна")
    existing = (await session.execute(
        select(Payment).where(
            Payment.order_id == order.id,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        ).order_by(Payment.id.desc()).execution_options(populate_existing=True).with_for_update()
    )).scalars().first()
    if existing:
        if existing.payment_method != "stars":
            raise CheckoutError("Для заказа уже создан счёт другим способом оплаты")
        return existing, False
    payment = Payment(
        user_id=buyer.id,
        amount=order.total_amount,
        provider_amount=stars_amount,
        payment_method="stars",
        status=PENDING_PAYMENT_STATUS,
        order_id=order.id,
        external_order_id=f"stars-{uuid4().hex}",
        idempotency_key=uuid4().hex,
    )
    session.add(payment)
    await session.flush()
    return payment, True


async def get_or_create_pending_stars_batch_payment(
    session: AsyncSession,
    *,
    batch_id: int,
    telegram_user_id: int,
    stars_amount: int,
) -> tuple[Payment, bool]:
    """Create one Telegram Stars invoice record for a whole checkout batch."""
    if not isinstance(stars_amount, int) or stars_amount <= 0:
        raise CheckoutError("Некорректная сумма Telegram Stars")
    preview = await session.get(OrderBatch, batch_id)
    if not preview:
        raise CheckoutError("Оформление не найдено")
    buyer = await _locked_user(session, preview.user_id)
    if buyer.telegram_id != telegram_user_id:
        raise CheckoutError("Оформление не найдено")
    batch = await _locked_batch(session, batch_id)
    if batch.user_id != buyer.id or batch.status != "PENDING_PAYMENT":
        raise CheckoutError("Оформление уже отменено или недоступно для оплаты")
    existing = (await session.execute(
        select(Payment).where(
            Payment.batch_id == batch.id,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        ).order_by(Payment.id.desc()).execution_options(populate_existing=True).with_for_update()
    )).scalars().first()
    if existing:
        if existing.payment_method != "stars":
            raise CheckoutError("Для оформления уже создан счёт другим способом оплаты")
        return existing, False
    payment = Payment(
        user_id=buyer.id,
        amount=batch.total_amount,
        provider_amount=stars_amount,
        payment_method="stars",
        status=PENDING_PAYMENT_STATUS,
        batch_id=batch.id,
        external_order_id=f"stars-batch-{uuid4().hex}",
        idempotency_key=uuid4().hex,
    )
    session.add(payment)
    await session.flush()
    return payment, True


async def _locked_stars_invoice(
    session: AsyncSession,
    *,
    payment_record_id: int,
    idempotency_key: str,
    telegram_user_id: int,
) -> tuple[Payment, Order | None, OrderBatch | None, User] | None:
    """Lock and validate either a legacy order or a grouped checkout invoice."""
    preview_result = await session.execute(
        select(Payment.user_id, Payment.order_id, Payment.batch_id).where(Payment.id == payment_record_id)
    )
    payment_preview = preview_result.one_or_none()
    if not payment_preview or (not payment_preview.order_id and not payment_preview.batch_id):
        return None
    buyer = await _locked_user(session, payment_preview.user_id)
    order = await _locked_order(session, payment_preview.order_id) if payment_preview.order_id else None
    batch = await _locked_batch(session, payment_preview.batch_id) if payment_preview.batch_id else None
    result = await session.execute(
        select(Payment).where(Payment.id == payment_record_id)
        .execution_options(populate_existing=True).with_for_update()
    )
    payment = result.scalar_one_or_none()
    if (
        not payment
        or payment.payment_method != "stars"
        or payment.idempotency_key != idempotency_key
        or payment.user_id != buyer.id
        or (order is not None and payment.order_id != order.id)
        or (batch is not None and payment.batch_id != batch.id)
    ):
        return None
    if buyer.telegram_id != telegram_user_id:
        return None
    expected_amount = order.total_amount if order is not None else batch.total_amount
    if not _same_money(payment.amount, expected_amount):
        return None
    if order is not None and order.user_id != buyer.id:
        return None
    if batch is not None and batch.user_id != buyer.id:
        return None
    return payment, order, batch, buyer


async def validate_telegram_stars_invoice(
    session: AsyncSession,
    *,
    payment_record_id: int,
    idempotency_key: str,
    telegram_user_id: int,
    allow_completed: bool = False,
) -> tuple[Decimal, int] | None:
    """Validate a Telegram Stars invoice for either order shape."""
    locked = await _locked_stars_invoice(
        session, payment_record_id=payment_record_id,
        idempotency_key=idempotency_key, telegram_user_id=telegram_user_id,
    )
    if not locked:
        return None
    payment, order, batch, _buyer = locked
    status = order.status if order is not None else batch.status
    if status == PENDING_ORDER_STATUS or status == "PENDING_PAYMENT":
        if payment.status not in ACTIVE_PAYMENT_STATUSES:
            return None
        return money(payment.amount), _stored_stars_amount(payment)
    if allow_completed and payment.status == SUCCESS_PAYMENT_STATUS:
        return money(payment.amount), _stored_stars_amount(payment)
    return None


async def authorize_telegram_stars_invoice(
    session: AsyncSession,
    *,
    payment_record_id: int,
    idempotency_key: str,
    telegram_user_id: int,
) -> Decimal | None:
    """Persist pre-checkout authorization for an order or batch."""
    locked = await _locked_stars_invoice(
        session, payment_record_id=payment_record_id,
        idempotency_key=idempotency_key, telegram_user_id=telegram_user_id,
    )
    if not locked:
        return None
    payment, order, batch, _buyer = locked
    status = order.status if order is not None else batch.status
    if status not in {PENDING_ORDER_STATUS, "PENDING_PAYMENT"} or payment.status not in ACTIVE_PAYMENT_STATUSES:
        return None
    if payment.status == PENDING_PAYMENT_STATUS:
        payment.status = STARS_AUTHORIZED_PAYMENT_STATUS
    return money(payment.amount)


async def complete_telegram_stars_payment(
    session: AsyncSession,
    *,
    payment_record_id: int,
    idempotency_key: str,
    telegram_user_id: int,
    telegram_charge_id: str,
) -> CompletedOrder:
    """Идемпотентно завершить оплату Telegram Stars старого заказа."""
    locked = await _locked_stars_invoice(
        session, payment_record_id=payment_record_id,
        idempotency_key=idempotency_key, telegram_user_id=telegram_user_id,
    )
    if not locked:
        raise CheckoutError("Платёж Telegram Stars не найден")
    payment, order, batch, buyer = locked
    if order is None or batch is not None:
        raise CheckoutError("Платёж не является одиночным заказом")
    duplicate = await session.scalar(select(Payment.id).where(
        Payment.payment_method == "stars", Payment.payment_id == telegram_charge_id,
        Payment.id != payment.id,
    ))
    if duplicate:
        raise CheckoutError("Идентификатор Telegram Stars уже использован")
    if payment.status == SUCCESS_PAYMENT_STATUS:
        if payment.payment_id != telegram_charge_id or order.status != COMPLETED_ORDER_STATUS:
            raise CheckoutError("Состояние платежа и заказа не совпадает")
        return CompletedOrder(
            order=order,
            accounts=await _locked_order_accounts(session, order),
            already_completed=True,
        )
    if payment.status not in ACTIVE_PAYMENT_STATUSES:
        raise CheckoutError("Платёж недоступен для подтверждения")
    _buyer, locked_users = await _locked_order_user_chain(session, buyer.id)
    payment.payment_id = telegram_charge_id
    return await _complete_order_locked(session, order, payment, buyer, locked_users)


async def complete_telegram_stars_batch_payment(
    session: AsyncSession,
    *,
    payment_record_id: int,
    idempotency_key: str,
    telegram_user_id: int,
    telegram_charge_id: str,
) -> list[CompletedOrder]:
    """Complete all child orders after a successful Telegram Stars invoice."""
    locked = await _locked_stars_invoice(
        session, payment_record_id=payment_record_id,
        idempotency_key=idempotency_key, telegram_user_id=telegram_user_id,
    )
    if not locked:
        raise CheckoutError("Платёж Telegram Stars не найден")
    payment, order, batch, buyer = locked
    if batch is None:
        raise CheckoutError("Платёж не является пакетным")
    existing = await session.scalar(select(Payment.id).where(
        Payment.payment_method == "stars", Payment.payment_id == telegram_charge_id,
        Payment.id != payment.id,
    ))
    if existing:
        raise CheckoutError("Идентификатор Telegram Stars уже использован")
    if payment.status == SUCCESS_PAYMENT_STATUS:
        if payment.payment_id != telegram_charge_id:
            raise CheckoutError("Идентификатор Telegram Stars уже использован")
        _buyer, locked_users = await _locked_order_user_chain(session, buyer.id)
        return await _complete_batch_locked(session, batch, payment, buyer, locked_users, provider_payment_id=telegram_charge_id)
    if payment.status not in ACTIVE_PAYMENT_STATUSES:
        raise CheckoutError("Платёж недоступен для подтверждения")
    _buyer, locked_users = await _locked_order_user_chain(session, buyer.id)
    payment.payment_id = telegram_charge_id
    return await _complete_batch_locked(session, batch, payment, buyer, locked_users, provider_payment_id=telegram_charge_id)


async def pay_order_from_balance(
    session: AsyncSession,
    order_id: int,
    telegram_user_id: int,
) -> CompletedOrder:
    """Оплатить один заказ с баланса с проверкой под блокировкой строки User."""
    order_preview = await session.get(Order, order_id)
    if not order_preview:
        raise CheckoutError("Заказ не найден")
    user, locked_users = await _locked_order_user_chain(
        session, order_preview.user_id
    )
    if user.telegram_id != telegram_user_id:
        raise CheckoutError("Заказ не найден")
    order = await _locked_order(session, order_id)
    if order.user_id != user.id:
        raise CheckoutError("Заказ не найден")
    if order.status == COMPLETED_ORDER_STATUS:
        accounts = await _locked_order_accounts(session, order)
        return CompletedOrder(order=order, accounts=accounts, already_completed=True)
    if order.status != PENDING_ORDER_STATUS:
        raise CheckoutError("Заказ уже отменен или недоступен для оплаты")
    if money(order.total_amount) <= ZERO:
        raise CheckoutError("Бесплатная оплата недоступна")
    if await _locked_active_order_payments(session, [order.id]):
        raise CheckoutError(
            "Для заказа уже создан счет. Оплатите его или дождитесь отмены "
            "у платежного провайдера."
        )
    if user.balance < order.total_amount:
        raise CheckoutError(
            f"Недостаточно средств на балансе. Требуется: {order.total_amount:.2f} ₽"
        )

    payment_id = f"balance:order:{order.id}"
    payment = Payment(
        user_id=user.id,
        amount=order.total_amount,
        payment_method="balance",
        payment_id=payment_id,
        status=PENDING_PAYMENT_STATUS,
        order_id=order.id,
    )
    session.add(payment)
    balance_after = money(user.balance) - money(order.total_amount)
    ledger_result = await record_purchase(
        session,
        user_id=user.id,
        amount=order.total_amount,
        order_id=order.id,
        balance_after=balance_after,
    )
    if not ledger_result.created:
        raise CheckoutError("Оплата заказа уже была учтена")
    user.balance = balance_after
    return await _complete_order_locked(
        session, order, payment, user, locked_users
    )


async def pay_order_batch_from_balance(
    session: AsyncSession,
    batch_id: int,
    telegram_user_id: int,
) -> list[CompletedOrder]:
    """Atomically debit the balance once and complete every child order."""
    preview = await session.get(OrderBatch, batch_id)
    if not preview:
        raise CheckoutError("Оформление не найдено")
    user, locked_users = await _locked_order_user_chain(session, preview.user_id)
    if user.telegram_id != telegram_user_id:
        raise CheckoutError("Оформление не найдено")
    batch = await _locked_batch(session, batch_id)
    if batch.user_id != user.id:
        raise CheckoutError("Оформление не найдено")
    orders = await _locked_batch_orders(session, batch.id)
    if batch.status in {"PAID", "PARTIAL", "COMPLETED"}:
        payment = (await session.execute(select(Payment).where(Payment.batch_id == batch.id, Payment.status == SUCCESS_PAYMENT_STATUS).order_by(Payment.id.desc()))).scalars().first()
        if not payment:
            raise CheckoutError("Состояние оформления повреждено")
        return await _complete_batch_locked(session, batch, payment, user, locked_users)
    if batch.status != "PENDING_PAYMENT":
        raise CheckoutError("Оформление уже отменено или недоступно для оплаты")
    if await session.scalar(select(Payment.id).where(Payment.batch_id == batch.id, Payment.status.in_(ACTIVE_PAYMENT_STATUSES))):
        raise CheckoutError("Для оформления уже создан счёт. Оплатите его или отмените.")
    total = money(batch.total_amount)
    if total <= ZERO or money(user.balance) < total:
        raise CheckoutError(f"Недостаточно средств. Требуется: {total:.2f} ₽")
    reserved_accounts: dict[int, list[Account]] = {}
    for order in orders:
        reserved_accounts[order.id] = await _locked_order_accounts(session, order)
    balance_after = money(user.balance) - total
    # Keep one immutable ledger entry per child order while the debit itself is
    # still one database transaction for the whole batch.
    for index, order in enumerate(orders):
        line_after = balance_after + sum((money(item.total_amount) for item in orders[index + 1:]), ZERO)
        result = await record_purchase(session, user_id=user.id, amount=order.total_amount, order_id=order.id, balance_after=line_after)
        if not result.created:
            raise CheckoutError(f"Оплата заказа #{order.id} уже была учтена")
    user.balance = balance_after
    payment = Payment(
        user_id=user.id,
        amount=total,
        payment_method="balance",
        payment_id=f"balance:batch:{batch.id}",
        status=PENDING_PAYMENT_STATUS,
        batch_id=batch.id,
    )
    session.add(payment)
    await session.flush()
    completed = await _complete_batch_locked(session, batch, payment, user, locked_users)
    for item in completed:
        item.accounts = reserved_accounts[item.order.id]
    return completed


async def complete_external_batch_payment(
    session: AsyncSession,
    *,
    payment_id: int,
    provider_payment_id: str,
    payment_method: str,
    external_order_id: str,
    provider_amount: Decimal | float | int | str,
) -> list[CompletedOrder]:
    """Complete every child order behind one externally paid invoice."""
    preview = await session.execute(select(Payment.user_id, Payment.batch_id).where(Payment.id == payment_id))
    row = preview.one_or_none()
    if not row or not row.batch_id:
        raise CheckoutError("Платёж оформления не найден")
    buyer, locked_users = await _locked_order_user_chain(session, row.user_id)
    batch = await _locked_batch(session, row.batch_id)
    payment = (await session.execute(select(Payment).where(Payment.id == payment_id).execution_options(populate_existing=True).with_for_update())).scalar_one_or_none()
    if (
        not payment
        or payment.user_id != buyer.id
        or payment.batch_id != batch.id
        or batch.user_id != buyer.id
        or payment.payment_method != payment_method
        or payment.external_order_id != external_order_id
    ):
        raise CheckoutError("Платёж оформления не найден")
    if not provider_payment_id or payment.payment_id not in {None, provider_payment_id}:
        raise CheckoutError("Идентификатор платежа не совпадает")
    if not _same_money(payment.amount, provider_amount) or not _same_money(payment.amount, batch.total_amount):
        raise CheckoutError("Сумма платежа не совпадает")
    payment.payment_id = provider_payment_id
    return await _complete_batch_locked(
        session, batch, payment, buyer, locked_users,
        provider_payment_id=provider_payment_id,
    )


async def pay_orders_from_balance(
    session: AsyncSession,
    order_ids: Iterable[int],
    telegram_user_id: int,
) -> list[CompletedOrder]:
    """Атомарно оплатить корзину с баланса.

    Не делает commit: при любой ошибке вызывающий код выполняет rollback, и ни
    баланс, ни статус какого-либо заказа не меняются.
    """
    unique_ids = list(dict.fromkeys(order_ids))
    if not unique_ids:
        raise CheckoutError("Заказы не найдены")

    # Сначала блокируем пользователя. Это сериализует параллельные списания.
    result = await session.execute(
        select(User.id).where(User.telegram_id == telegram_user_id)
    )
    buyer_id = result.scalar_one_or_none()
    if buyer_id is None:
        raise CheckoutError("Пользователь не найден")
    user, locked_users = await _locked_order_user_chain(session, buyer_id)

    # Стабильный порядок блокировок не дает двум корзинам взаимно ждать друг
    # друга в PostgreSQL.
    result = await session.execute(
        select(Order)
        .where(Order.id.in_(unique_ids), Order.user_id == user.id)
        .order_by(Order.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    orders = result.scalars().all()
    if len(orders) != len(unique_ids):
        raise CheckoutError("Часть заказов не найдена")
    if any(order.status != PENDING_ORDER_STATUS for order in orders):
        raise CheckoutError("В корзине есть уже оплаченный или отмененный заказ")
    if any(money(order.total_amount) <= ZERO for order in orders):
        raise CheckoutError("В корзине есть заказ с нулевой суммой. Бесплатная оплата недоступна")
    if await _locked_active_order_payments(session, (order.id for order in orders)):
        raise CheckoutError(
            "В корзине есть заказ с уже созданным счетом. Оплатите его или "
            "дождитесь отмены у платежного провайдера."
        )

    total_amount = sum((money(order.total_amount) for order in orders), ZERO)
    if user.balance < total_amount:
        raise CheckoutError(
            f"Недостаточно средств на балансе. Требуется: {total_amount:.2f} ₽"
        )

    # Проверяем все резервы до первого изменения. Если один заказ нельзя
    # выдать, транзакция не спишет деньги ни за один из них.
    reserved_accounts: dict[int, list[Account]] = {}
    for order in orders:
        reserved_accounts[order.id] = await _locked_order_accounts(session, order)

    completed: list[CompletedOrder] = []
    for order in orders:
        balance_after = money(user.balance) - money(order.total_amount)
        ledger_result = await record_purchase(
            session,
            user_id=user.id,
            amount=order.total_amount,
            order_id=order.id,
            balance_after=balance_after,
        )
        if not ledger_result.created:
            raise CheckoutError(f"Оплата заказа #{order.id} уже была учтена")
        user.balance = balance_after
        payment = Payment(
            user_id=user.id,
            amount=order.total_amount,
            payment_method="balance",
            payment_id=f"balance:order:{order.id}",
            status=PENDING_PAYMENT_STATUS,
            order_id=order.id,
        )
        session.add(payment)
        completed_order = await _complete_order_locked(
            session, order, payment, user, locked_users
        )
        # _complete_order_locked locks the same rows again on some dialects;
        # сохраненная ссылка гарантирует, что файл выдачи соответствует
        # предварительно проверенному резерву.
        completed_order.accounts = reserved_accounts[order.id]
        completed.append(completed_order)
    return completed


async def _reconcile_external_order_payments_for_cancellation(
    session: AsyncSession,
    order_id: int,
) -> dict[int, ProviderInvoiceStatus]:
    """Read provider states before attempting an order cancellation.

    A separate short-lived database session is closed before HTTP calls, so
    an auto-expiry sweep never holds row locks while it waits for a provider.
    The result is re-checked under locks by :func:`cancel_pending_order`.
    """
    # Do the preliminary lookup in an independent short-lived session.  A
    # caller such as the reservation sweeper can cancel several orders in one
    # transaction; rolling back its session here would silently discard the
    # cancellations made for earlier orders.  This secondary session is closed
    # before HTTP requests, so it neither retains database locks nor affects
    # the caller's transaction.
    bind = session.bind
    if bind is None:
        raise CheckoutError("Не удалось открыть соединение для сверки платежа")
    async with AsyncSession(bind=bind, expire_on_commit=False) as read_session:
        result = await read_session.execute(
            select(
                Payment.id,
                Payment.payment_method,
                Payment.external_order_id,
                Payment.payment_id,
            ).where(
                Payment.order_id == order_id,
                Payment.payment_method.in_(EXTERNAL_PAYMENT_METHODS),
                Payment.status == PENDING_PAYMENT_STATUS,
            )
        )
        snapshots = result.all()
    states: dict[int, ProviderInvoiceStatus] = {}
    for payment_id, method, external_order_id, provider_payment_id in snapshots:
        if not external_order_id:
            continue
        states[payment_id] = await PaymentService.reconcile_provider_invoice(
            method,
            external_order_id=external_order_id,
            provider_payment_id=provider_payment_id,
        )
    return states


async def cancel_pending_order(
    session: AsyncSession,
    order_id: int,
    *,
    telegram_user_id: int | None = None,
    reconcile_provider: bool = True,
) -> Order:
    """Атомарно отменить неоплаченный заказ и снять резерв со склада.

    Заказ, его аккаунты, товар и ожидающие платежи блокируются в одной
    транзакции. Поэтому webhook не сможет выдать товар ровно в тот момент,
    когда резерв снимается по отмене или истечению срока.
    """
    provider_states: dict[int, ProviderInvoiceStatus] = {}
    if reconcile_provider:
        # User/admin cancellation and reservation expiry use this path. A
        # webhook which has already verified a final failure passes False and
        # retains its in-flight transaction instead.
        provider_states = await _reconcile_external_order_payments_for_cancellation(
            session, order_id
        )

    preview_result = await session.execute(
        select(Order.user_id).where(Order.id == order_id)
    )
    order_preview = preview_result.one_or_none()
    if not order_preview:
        raise CheckoutError("Заказ не найден")
    # Keep the same lock order as all completion paths: User -> Order ->
    # Payment -> Account -> Product.  This prevents a cancellation and a
    # webhook from forming a database deadlock cycle.
    buyer = await _locked_user(session, order_preview.user_id)
    if telegram_user_id is not None:
        if buyer.telegram_id != telegram_user_id:
            raise CheckoutError("Заказ не найден")

    order = await _locked_order(session, order_id)
    if order.user_id != buyer.id:
        raise CheckoutError("Заказ не найден")
    if order.status != PENDING_ORDER_STATUS:
        raise CheckoutError("Можно отменить только неоплаченный заказ")

    active_payments = await _locked_active_order_payments(session, [order.id])
    now = datetime.now()
    for payment in active_payments:
        if (
            payment.payment_method not in EXTERNAL_PAYMENT_METHODS
            or payment.status != PENDING_PAYMENT_STATUS
        ):
            continue
        provider_status = provider_states.get(payment.id, ProviderInvoiceStatus("unknown"))
        state = provider_status.state
        observed_payment_id = provider_status.payment_id
        observed_url = provider_status.payment_url
        if observed_payment_id and payment.payment_id not in {
            None,
            observed_payment_id,
        }:
            raise CheckoutError("Идентификатор внешнего счёта не совпадает")
        if observed_payment_id:
            payment.payment_id = observed_payment_id
        if observed_url:
            payment.payment_url = observed_url
        if state == "failed":
            payment.status = FAILED_PAYMENT_STATUS
            payment.completed_at = now
            continue
        if state == "succeeded":
            # Never return stock after an API-confirmed payment, even if its
            # webhook is delayed or was temporarily unavailable.
            raise CheckoutError(
                "Провайдер уже подтвердил оплату. Заказ нельзя отменить; "
                "дождитесь автоматической выдачи товара."
            )
        if (
            payment.payment_id is None
            and payment.payment_method != "yookassa"
            and _external_payment_creation_expired(payment)
            and state == "unknown"
        ):
            # The remote call may have timed out before returning an ID. There
            # is no customer-facing link to pay, and the provider-specific
            # invoice lifetime has elapsed, so it is now safe to drop. YooKassa
            # is excluded because its idempotence-key retry can still recover
            # the invoice without risking a second charge.
            payment.status = FAILED_PAYMENT_STATUS
            payment.completed_at = now

    if any(
        payment.payment_method in EXTERNAL_PAYMENT_METHODS
        and payment.status == PENDING_PAYMENT_STATUS
        for payment in active_payments
    ):
        raise CheckoutError(
            "Для заказа есть активный внешний счет. Его нельзя безопасно "
            "освободить до отмены или истечения срока у платежного провайдера."
        )
    if any(
        payment.payment_method == "stars"
        and payment.status == STARS_AUTHORIZED_PAYMENT_STATUS
        for payment in active_payments
    ):
        raise CheckoutError(
            "Telegram Stars уже подтвердил оплату. Дождитесь завершения "
            "платежа или обратитесь в поддержку."
        )

    # A Stars invoice which has not passed pre-checkout cannot charge the user,
    # so it can be safely invalidated together with the reservation.
    for payment in active_payments:
        payment.status = FAILED_PAYMENT_STATUS
        payment.completed_at = datetime.now()

    delivery_type = await session.scalar(
        select(Product.delivery_type).where(Product.id == order.product_id)
    )
    if delivery_type in VIRTUAL_DELIVERY_TYPES:
        accounts = []
    else:
        result = await session.execute(
            select(Account)
            .where(Account.order_id == order.id)
            .order_by(Account.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        accounts = result.scalars().all()
        if len(accounts) != order.quantity:
            raise CheckoutError(
                "Резерв товара для заказа поврежден. Обратитесь в поддержку."
            )
    result = await session.execute(
        select(Product)
        .where(Product.id == order.product_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    product = result.scalar_one_or_none()
    if not product:
        raise CheckoutError("Товар заказа не найден")

    released_count = 0
    for account in accounts:
        account.is_sold = False
        account.sold_at = None
        account.order_id = None
        if not account.is_blocked:
            released_count += 1
    if delivery_type not in VIRTUAL_DELIVERY_TYPES:
        product.stock_count += released_count

    order.status = CANCELLED_ORDER_STATUS
    order.reserved_until = None
    # A one-time percentage coupon is reserved by the pending order. Return it
    # before the surrounding transaction commits the cancellation.
    from utils.promotions import restore_coupon_for_cancelled_order

    await restore_coupon_for_cancelled_order(session, order)
    return order


async def cancel_pending_batch(
    session: AsyncSession,
    batch_id: int,
    *,
    telegram_user_id: int | None = None,
) -> OrderBatch:
    """Cancel a not-yet-paid grouped checkout and restore its coupon once."""
    preview = await session.get(OrderBatch, batch_id)
    if not preview:
        raise CheckoutError("Оформление не найдено")
    buyer = await _locked_user(session, preview.user_id)
    if telegram_user_id is not None and buyer.telegram_id != telegram_user_id:
        raise CheckoutError("Оформление не найдено")
    batch = await _locked_batch(session, batch_id)
    if batch.user_id != buyer.id:
        raise CheckoutError("Оформление не найдено")
    if batch.status != "PENDING_PAYMENT":
        raise CheckoutError("Можно отменить только неоплаченное оформление")
    orders = await _locked_batch_orders(session, batch.id)
    active = list((await session.execute(
        select(Payment).where(
            Payment.batch_id == batch.id,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        ).execution_options(populate_existing=True).with_for_update()
    )).scalars().all())
    if any(payment.status == STARS_AUTHORIZED_PAYMENT_STATUS for payment in active):
        raise CheckoutError("Telegram Stars уже подтвердил оплату. Дождитесь завершения платежа.")
    if any(payment.payment_method in EXTERNAL_PAYMENT_METHODS for payment in active):
        raise CheckoutError("Для оформления уже создан внешний счёт. Дождитесь его отмены или истечения.")
    now = datetime.now()
    for payment in active:
        payment.status = FAILED_PAYMENT_STATUS
        payment.completed_at = now
    for order in orders:
        if order.status == PENDING_ORDER_STATUS:
            order.status = CANCELLED_ORDER_STATUS
            order.reserved_until = None
    if batch.coupon_activation_id:
        from utils.promotions import restore_coupon_for_cancelled_order
        # Reuse the existing restoration routine with a lightweight proxy order
        # carrying the parent activation ID; no child order stores this FK.
        class _CouponOwner:
            coupon_activation_id = batch.coupon_activation_id
        await restore_coupon_for_cancelled_order(session, _CouponOwner())
    batch.status = "CANCELLED"
    batch.reserved_until = None
    return batch
