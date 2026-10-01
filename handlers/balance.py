"""Баланс и безопасное создание счетов на пополнение."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from datetime import datetime, timedelta
from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BalanceTransfer, Payment, User
from utils.checkout import (
    CheckoutError,
    PENDING_PAYMENT_STATUS,
    STARS_AUTHORIZED_PAYMENT_STATUS,
    SUCCESS_PAYMENT_STATUS,
    get_or_create_pending_external_payment,
    resolve_pending_external_invoice,
)
from utils.payments import (
    PAYMENT_METHOD_TITLES,
    PaymentService,
    ProviderInvoice,
    get_enabled_category_methods,
    get_payment_category_title,
    stars_amount_for_rubles,
)
from utils.keyboards import (
    get_balance_actions_keyboard,
    get_balance_topup_category_methods_keyboard,
    get_balance_topup_keyboard,
)
from utils.ledger import record_topup, record_transfer
from utils.money import ZERO, format_rubles, money, to_money
from utils.promotions import CouponError, redeem_coupon
from utils.private_chat import apply_private_chat_guard
from utils.text import get_balance_text
from utils.single_message import apply_single_message_workflow, edit_input_screen

logger = logging.getLogger(__name__)
router = Router()
apply_private_chat_guard(router)
apply_single_message_workflow(router)


class TopupStates(StatesGroup):
    waiting_amount = State()


class TransferStates(StatesGroup):
    waiting_amount = State()
    waiting_recipient_id = State()
    waiting_message = State()


class CouponStates(StatesGroup):
    waiting_code = State()


@dataclass(frozen=True)
class TopupPaymentResult:
    provider: str
    amount: Decimal
    invoice: ProviderInvoice | None = None
    stars_payment: Payment | None = None
    stars: int | None = None


def _stars_topup_payload(payment: Payment) -> str:
    return f"stars_topup:{payment.id}:{payment.idempotency_key}"


def _parse_stars_topup_payload(payload: str | None) -> tuple[int, str] | None:
    parts = (payload or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "stars_topup" or not parts[1].isdigit():
        return None
    payment_id, idempotency_key = int(parts[1]), parts[2]
    if payment_id <= 0 or len(idempotency_key) != 32:
        return None
    return payment_id, idempotency_key


def _stars_amount(rubles: Decimal | float | int) -> int:
    """Посчитать Stars для нового счёта по текущему курсу."""
    return stars_amount_for_rubles(rubles)


def _stored_stars_amount(payment: Payment) -> int:
    """Взять число Stars из выпущенного счёта, не пересчитывая его позже."""
    if isinstance(payment.provider_amount, int) and payment.provider_amount > 0:
        return payment.provider_amount
    # До миграции курс был фиксированным 2.3 ₽ за Star.
    return max(
        1,
        int((Decimal(str(payment.amount)) / Decimal("2.3")).to_integral_value(rounding=ROUND_UP)),
    )


async def _get_user(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


def _has_enabled_topup_method() -> bool:
    return any(
        PaymentService.provider_enabled(method)
        for method in ("yookassa", "yoomoney", "lava", "heleket", "cryptobot", "stars")
    )


def _topup_amount_keyboard() -> InlineKeyboardMarkup:
    """Компактный выбор суммы пополнения с возможностью ручного ввода."""
    amounts = (100, 250, 500, 1000, 2500, 5000)
    rows = [
        [
            InlineKeyboardButton(text=f"{amount} ₽", callback_data=f"topup_amount_{amount}")
            for amount in amounts[index:index + 3]
        ]
        for index in range(0, len(amounts), 3)
    ]
    rows.extend([
        [InlineKeyboardButton(text="✏️ Своя сумма", callback_data="topup_manual_amount")],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="balance_topup_menu"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _topup_manual_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⛔ Отмена", callback_data="balance_topup_menu"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
    ]])


@router.callback_query(F.data == "menu_balance")
async def show_balance_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await _get_user(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден. Используйте /start", show_alert=True)
        return
    methods_available = _has_enabled_topup_method()
    await callback.message.edit_text(
        get_balance_text(user.balance, methods_available),
        reply_markup=get_balance_actions_keyboard(methods_available),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance_topup_menu")
async def show_topup_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Показать только подключённые способы пополнения."""
    await state.clear()
    user = await _get_user(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    methods_available = _has_enabled_topup_method()
    await callback.message.edit_text(
        get_balance_text(
            user.balance,
            methods_available,
            choosing_topup_category=True,
        ),
        reply_markup=get_balance_topup_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance_topup_unavailable")
async def balance_topup_unavailable(callback: CallbackQuery):
    """Explain the disabled action instead of opening an empty payment screen."""
    await callback.answer(
        "Пополнение сейчас недоступно: администратор не подключил ни одного способа оплаты.",
        show_alert=True,
    )


def _coupon_input_keyboard(back_callback: str = "menu_balance") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Отмена", callback_data=back_callback)],
    ])


def _coupon_back_callback(data: dict) -> str:
    """Return only a known navigation target stored for the coupon flow."""
    callback_data = data.get("coupon_back_callback")
    return callback_data if callback_data in {"back_to_menu", "menu_balance"} else "menu_balance"


async def _edit_coupon_screen(
    bot,
    data: dict,
    fallback_chat_id: int,
    fallback_message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await bot.edit_message_text(
        chat_id=data.get("coupon_chat_id", fallback_chat_id),
        message_id=data.get("coupon_message_id", fallback_message_id),
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.in_({"balance_coupon", "menu_coupon"}))
async def balance_coupon_start(callback: CallbackQuery, state: FSMContext):
    """Open a single-message prompt for a user-facing promo-code redemption."""
    await state.clear()
    back_callback = "back_to_menu" if callback.data == "menu_coupon" else "menu_balance"
    await state.update_data(
        coupon_chat_id=callback.message.chat.id,
        coupon_message_id=callback.message.message_id,
        coupon_attempt_key=uuid4().hex,
        coupon_back_callback=back_callback,
    )
    await state.set_state(CouponStates.waiting_code)
    await callback.message.edit_text(
        "🎟 <b>Активация промокода</b>\n\n"
        "Отправьте код промокода одним сообщением.",
        reply_markup=_coupon_input_keyboard(back_callback),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CouponStates.waiting_code)
async def balance_coupon_redeem(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    """Activate a fixed credit or a percentage coupon in one DB transaction."""
    data = await state.get_data()
    try:
        redemption = await redeem_coupon(
            session,
            telegram_user_id=message.from_user.id,
            coupon_code=message.text or "",
            idempotency_key=data.get("coupon_attempt_key"),
        )
        await session.commit()
        user = await _get_user(session, message.from_user.id)
        if user is None:
            raise CouponError("Пользователь не найден")
    except CouponError as exc:
        await session.rollback()
        await _edit_coupon_screen(
            message.bot,
            data,
            message.chat.id,
            message.message_id,
            f"❌ <b>{escape(str(exc))}</b>\n\nОтправьте код ещё раз.",
            _coupon_input_keyboard(_coupon_back_callback(data)),
        )
        await _delete_transfer_input(message)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not redeem coupon")
        await _edit_coupon_screen(
            message.bot,
            data,
            message.chat.id,
            message.message_id,
            "❌ Не удалось активировать промокод. Попробуйте ещё раз.",
            _coupon_input_keyboard(_coupon_back_callback(data)),
        )
        await _delete_transfer_input(message)
        return

    back_callback = _coupon_back_callback(data)
    await state.clear()
    if redemption.credited_amount > ZERO:
        result_text = (
            "✅ <b>Промокод активирован.</b>\n\n"
            f"На баланс зачислено: <b>{format_rubles(redemption.credited_amount)} ₽</b>\n"
            f"Текущий баланс: <b>{format_rubles(user.balance)} ₽</b>"
        )
    elif redemption.already_redeemed:
        result_text = (
            "ℹ️ <b>Этот промокод уже обработан.</b>\n\n"
            "Баланс повторно не изменён."
        )
    elif redemption.already_active:
        result_text = (
            "ℹ️ <b>Этот промокод уже активирован.</b>\n\n"
            "Его скидка будет применяться к новым заказам."
        )
    else:
        result_text = (
            "✅ <b>Промокод активирован.</b>\n\n"
            f"Скидка: <b>{format_rubles(redemption.coupon.discount_value)}%</b>\n"
            "Она будет учтена при создании следующего заказа."
        )
    await _edit_coupon_screen(
        message.bot,
        data,
        message.chat.id,
        message.message_id,
        result_text,
        (
            get_balance_actions_keyboard(_has_enabled_topup_method())
            if back_callback == "menu_balance"
            else _coupon_input_keyboard("back_to_menu")
        ),
    )
    await _delete_transfer_input(message)


def _parse_transfer_amount(value: str | None) -> Decimal:
    try:
        return to_money(value or "", minimum=Decimal("0.01"))
    except ValueError as exc:
        raise ValueError("Введите корректную сумму, например 100 или 100.50") from exc


async def _edit_transfer_screen(
    bot, data: dict, fallback_chat_id: int, fallback_message_id: int, text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await bot.edit_message_text(
        chat_id=data.get("transfer_chat_id", fallback_chat_id),
        message_id=data.get("transfer_message_id", fallback_message_id),
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def _delete_transfer_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete balance transfer input", exc_info=True)


def _transfer_amount_keyboard(selected_amount: Decimal | None = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора суммы перевода без необходимости писать её вручную."""
    quick_amounts = (100, 250, 500, 1000, 2000, 5000)
    rows = []
    for start in range(0, len(quick_amounts), 3):
        row = []
        for amount in quick_amounts[start:start + 3]:
            marker = "✓ " if selected_amount == amount else ""
            row.append(InlineKeyboardButton(
                text=f"{marker}{amount} ₽",
                callback_data=f"balance_transfer_amount_{amount}",
            ))
        rows.append(row)
    rows.extend([
        [InlineKeyboardButton(text="✏️ Ручной ввод", callback_data="balance_transfer_manual_amount")],
        [
            InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance"),
            InlineKeyboardButton(text="✅ Принять", callback_data="balance_transfer_confirm_amount"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_transfer_amount_screen(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
) -> None:
    data = await state.get_data()
    selected_amount = data.get("transfer_amount")
    selected = selected_amount if isinstance(selected_amount, Decimal) else None
    selected_text = (
        f"\n\nВыбрано: <b>{format_rubles(selected)} ₽</b>"
        if selected is not None else ""
    )
    await callback.message.edit_text(
        "🔁 <b>Перевод с баланса</b>\n\n"
        f"Доступно: <b>{format_rubles(user.balance)} ₽</b>\n\n"
        f"Выберите сумму перевода или введите свою:{selected_text}",
        reply_markup=_transfer_amount_keyboard(selected),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "balance_transfer")
async def balance_transfer_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать безопасный перевод между балансами пользователей."""
    user = await _get_user(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        transfer_chat_id=callback.message.chat.id,
        transfer_message_id=callback.message.message_id,
        transfer_idempotency_key=uuid4().hex,
    )
    await state.set_state(TransferStates.waiting_amount)
    await _show_transfer_amount_screen(callback, state, user)
    await callback.answer()


@router.message(TransferStates.waiting_amount)
async def balance_transfer_amount(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    try:
        amount = _parse_transfer_amount(message.text)
        sender = await _get_user(session, message.from_user.id)
        if not sender:
            raise ValueError("Пользователь не найден")
        if amount > money(sender.balance):
            raise ValueError("Недостаточно средств на балансе")
    except ValueError as exc:
        await _edit_transfer_screen(
            message.bot, data, message.chat.id, message.message_id,
            f"❌ {escape(str(exc))}\n\nВведите сумму ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")]
            ]),
        )
        await _delete_transfer_input(message)
        return
    await state.update_data(transfer_amount=amount)
    user = await _get_user(session, message.from_user.id)
    if not user:
        await _delete_transfer_input(message)
        return
    await state.set_state(TransferStates.waiting_amount)
    await _edit_transfer_screen(
        message.bot, data, message.chat.id, message.message_id,
        "🔁 <b>Перевод с баланса</b>\n\n"
        f"Доступно: <b>{format_rubles(user.balance)} ₽</b>\n\n"
        f"Выберите сумму перевода или введите свою:\n\nВыбрано: <b>{format_rubles(amount)} ₽</b>",
        _transfer_amount_keyboard(amount),
    )
    await _delete_transfer_input(message)


@router.callback_query(F.data.startswith("balance_transfer_amount_"))
async def balance_transfer_select_amount(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        amount = _parse_transfer_amount(callback.data.rsplit("_", 1)[1])
        user = await _get_user(session, callback.from_user.id)
        if not user:
            raise ValueError("Пользователь не найден")
        if amount > money(user.balance):
            raise ValueError("Недостаточно средств на балансе")
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(transfer_amount=amount)
    await state.set_state(TransferStates.waiting_amount)
    await _show_transfer_amount_screen(callback, state, user)
    await callback.answer()


@router.callback_query(F.data == "balance_transfer_manual_amount")
async def balance_transfer_manual_amount(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TransferStates.waiting_amount)
    await callback.message.edit_text(
        "✏️ <b>Ручной ввод суммы</b>\n\n"
        "Отправьте сумму перевода, например <code>100</code> или <code>100.50</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance_transfer_confirm_amount")
async def balance_transfer_confirm_amount(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    amount = data.get("transfer_amount")
    if not isinstance(amount, Decimal):
        await callback.answer("Сначала выберите сумму", show_alert=True)
        return
    user = await _get_user(session, callback.from_user.id)
    if not user or amount > money(user.balance):
        await callback.answer("Недостаточно средств на балансе", show_alert=True)
        return
    await state.set_state(TransferStates.waiting_recipient_id)
    await callback.message.edit_text(
        "🔁 <b>Перевод с баланса</b>\n\n"
        f"Сумма: <b>{format_rubles(amount)} ₽</b>\n\n"
        "Отправьте Telegram ID получателя.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TransferStates.waiting_recipient_id)
async def balance_transfer_recipient(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    try:
        recipient_telegram_id = int((message.text or "").strip())
        if recipient_telegram_id <= 0:
            raise ValueError
    except ValueError:
        await _edit_transfer_screen(
            message.bot, data, message.chat.id, message.message_id,
            "❌ Укажите корректный числовой Telegram ID получателя.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")]
            ]),
        )
        await _delete_transfer_input(message)
        return
    if recipient_telegram_id == message.from_user.id:
        error = "Нельзя перевести средства самому себе."
    else:
        recipient = await _get_user(session, recipient_telegram_id)
        error = "Получатель не найден. Он должен сначала запустить бота." if not recipient else None
        if recipient and recipient.is_blocked:
            error = "Получатель заблокирован."
    if error:
        await _edit_transfer_screen(
            message.bot, data, message.chat.id, message.message_id,
            f"❌ {error}\n\nОтправьте Telegram ID ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")]
            ]),
        )
        await _delete_transfer_input(message)
        return
    await state.update_data(transfer_recipient_telegram_id=recipient_telegram_id)
    await state.set_state(TransferStates.waiting_message)
    await _edit_transfer_screen(
        message.bot, data, message.chat.id, message.message_id,
        "🔁 <b>Сообщение к переводу</b>\n\n"
        "Отправьте сообщение для получателя (до 1000 символов) или пропустите этот шаг.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Без сообщения", callback_data="balance_transfer_skip_message")],
            [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")],
        ]),
    )
    await _delete_transfer_input(message)


async def _complete_balance_transfer(
    bot, state: FSMContext, session: AsyncSession, sender_telegram_id: int,
    fallback_chat_id: int, fallback_message_id: int, note: str | None,
) -> None:
    data = await state.get_data()
    amount = data.get("transfer_amount")
    recipient_telegram_id = data.get("transfer_recipient_telegram_id")
    transfer_idempotency_key = data.get("transfer_idempotency_key")
    if (
        not isinstance(amount, Decimal)
        or not isinstance(recipient_telegram_id, int)
        or not isinstance(transfer_idempotency_key, str)
        or len(transfer_idempotency_key) != 32
    ):
        raise ValueError("Данные перевода не найдены. Начните заново.")

    result = await session.execute(
        select(User)
        .where(User.telegram_id.in_([sender_telegram_id, recipient_telegram_id]))
        .order_by(User.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    users = {user.telegram_id: user for user in result.scalars()}
    sender = users.get(sender_telegram_id)
    recipient = users.get(recipient_telegram_id)
    if not sender or not recipient:
        raise ValueError("Отправитель или получатель не найден")
    if sender.is_blocked or recipient.is_blocked:
        raise ValueError("Перевод недоступен для заблокированного пользователя")
    if sender.id == recipient.id:
        raise ValueError("Нельзя перевести средства самому себе")
    if amount > money(sender.balance):
        raise ValueError("Недостаточно средств на балансе")

    # Both users are already locked. Checking the durable idempotency key
    # under the same lock makes a second Telegram callback a harmless no-op.
    existing_transfer = (
        await session.execute(
            select(BalanceTransfer)
            .where(BalanceTransfer.idempotency_key == transfer_idempotency_key)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing_transfer is not None:
        await session.rollback()
        await state.clear()
        await _edit_transfer_screen(
            bot, data, fallback_chat_id, fallback_message_id,
            "ℹ️ <b>Этот перевод уже выполнен.</b> Баланс повторно не изменён.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К балансу", callback_data="menu_balance")]
            ]),
        )
        return

    transfer = BalanceTransfer(
        sender_id=sender.id,
        recipient_id=recipient.id,
        amount=amount,
        message=note,
        idempotency_key=transfer_idempotency_key,
    )
    session.add(transfer)
    await session.flush()

    sender_balance_after = money(sender.balance) - amount
    recipient_balance_after = money(recipient.balance) + amount
    outgoing, incoming = await record_transfer(
        session,
        sender_id=sender.id,
        recipient_id=recipient.id,
        amount=amount,
        transfer_id=transfer.id,
        message=note,
        sender_balance_after=sender_balance_after,
        recipient_balance_after=recipient_balance_after,
    )
    if not outgoing.created or not incoming.created:
        raise CheckoutError("Перевод уже был обработан")

    sender.balance = sender_balance_after
    recipient.balance = recipient_balance_after
    await session.commit()
    await state.clear()

    await _edit_transfer_screen(
        bot, data, fallback_chat_id, fallback_message_id,
        "✅ <b>Перевод выполнен.</b>\n\n"
        f"Сумма: <b>{format_rubles(amount)} ₽</b>\n"
        f"Ваш баланс: <b>{format_rubles(sender.balance)} ₽</b>",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 К балансу", callback_data="menu_balance")]
        ]),
    )

    sender_name = f"@{sender.username}" if sender.username else (sender.first_name or "Пользователь")
    notification = (
        "💸 <b>Вам поступил перевод</b>\n\n"
        f"Сумма: <b>{format_rubles(amount)} ₽</b>\n"
        f"Отправитель: {escape(sender_name)}"
    )
    if note:
        notification += f"\n\n💬 Сообщение:\n{escape(note)}"
    try:
        await bot.send_message(recipient.telegram_id, notification, parse_mode="HTML")
    except Exception:
        logger.warning("Balance transfer %s completed but recipient notification was not delivered", recipient.telegram_id)


@router.message(TransferStates.waiting_message)
async def balance_transfer_message(message: Message, state: FSMContext, session: AsyncSession):
    note = (message.text or "").strip()
    data = await state.get_data()
    if len(note) > 1000:
        await _edit_transfer_screen(
            message.bot, data, message.chat.id, message.message_id,
            "❌ Сообщение не должно превышать 1000 символов. Отправьте его ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Без сообщения", callback_data="balance_transfer_skip_message")],
                [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")],
            ]),
        )
        await _delete_transfer_input(message)
        return
    try:
        await _complete_balance_transfer(
            message.bot, state, session, message.from_user.id,
            message.chat.id, message.message_id, note or None,
        )
    except ValueError as exc:
        await session.rollback()
        await _edit_transfer_screen(
            message.bot, data, message.chat.id, message.message_id,
            f"❌ {escape(str(exc))}",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К балансу", callback_data="menu_balance")]
            ]),
        )
        await state.clear()
    except Exception:
        await session.rollback()
        logger.exception("Could not complete balance transfer")
        await _edit_transfer_screen(
            message.bot, data, message.chat.id, message.message_id,
            "❌ Не удалось выполнить перевод. Попробуйте позже.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К балансу", callback_data="menu_balance")]
            ]),
        )
        await state.clear()
    await _delete_transfer_input(message)


@router.callback_query(F.data == "balance_transfer_back_amount")
async def balance_transfer_back_amount(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Вернуться из ввода ID к предыдущему шагу — сумме перевода."""
    user = await _get_user(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await state.update_data(transfer_amount=None, transfer_recipient_telegram_id=None)
    await state.set_state(TransferStates.waiting_amount)
    await _show_transfer_amount_screen(callback, state, user)
    await callback.answer()


@router.callback_query(F.data == "balance_transfer_back_recipient")
async def balance_transfer_back_recipient(callback: CallbackQuery, state: FSMContext):
    """Вернуться из сообщения к предыдущему шагу — Telegram ID получателя."""
    data = await state.get_data()
    amount = data.get("transfer_amount")
    if not isinstance(amount, Decimal):
        await callback.answer("Данные перевода не найдены", show_alert=True)
        return
    await state.update_data(transfer_recipient_telegram_id=None)
    await state.set_state(TransferStates.waiting_recipient_id)
    await callback.message.edit_text(
        "🔁 <b>Перевод с баланса</b>\n\n"
        f"Сумма: <b>{format_rubles(amount)} ₽</b>\n\n"
        "Отправьте Telegram ID получателя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_balance")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance_transfer_skip_message")
async def balance_transfer_skip_message(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        await _complete_balance_transfer(
            callback.message.bot, state, session, callback.from_user.id,
            callback.message.chat.id, callback.message.message_id, None,
        )
    except ValueError as exc:
        await session.rollback()
        await callback.message.edit_text(
            f"❌ {escape(str(exc))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К балансу", callback_data="menu_balance")]
            ]),
            parse_mode="HTML",
        )
        await state.clear()
    except Exception:
        await session.rollback()
        logger.exception("Could not complete balance transfer without message")
        await callback.answer("Не удалось выполнить перевод", show_alert=True)
        return
    await callback.answer()


async def _start_topup_for_method(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    method: str,
) -> None:
    """Открыть ввод суммы для выбранного фактического провайдера."""
    user = await _get_user(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    if method not in {"yookassa", "yoomoney", "heleket", "lava", "cryptobot", "stars"}:
        await callback.answer("Этот способ оплаты не подключен", show_alert=True)
        return
    if not PaymentService.provider_enabled(method):
        await callback.answer("Этот способ оплаты не подключен", show_alert=True)
        return

    await state.update_data(topup_method=method)
    await state.set_state(TopupStates.waiting_amount)
    await callback.message.edit_text(
        "Выберите сумму пополнения:",
        parse_mode="HTML",
        reply_markup=_topup_amount_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("topup_category_"))
async def choose_topup_category(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    """Открыть единственный метод категории или список её провайдеров."""
    category = (callback.data or "").removeprefix("topup_category_")
    methods = get_enabled_category_methods(category)
    if not methods:
        await callback.answer("В этой категории нет подключённых способов оплаты", show_alert=True)
        return

    await state.clear()
    if len(methods) == 1:
        await _start_topup_for_method(callback, session, state, methods[0])
        return

    user = await _get_user(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"{get_payment_category_title(category)}\n\n"
        "Выберите способ пополнения:",
        reply_markup=get_balance_topup_category_methods_keyboard(category),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.in_({
    "topup_yookassa",
    "topup_yoomoney",
    "topup_heleket",
    "topup_lava",
    "topup_cryptobot",
    "topup_stars",
}))
async def process_topup(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    method = callback.data.removeprefix("topup_")
    await _start_topup_for_method(callback, session, state, method)


def _parse_amount(value: str | None) -> Decimal:
    try:
        return to_money(value or "", minimum=Decimal("1.00"))
    except ValueError as exc:
        raise ValueError("Введите корректную сумму, например 100 или 100.50") from exc


async def _create_topup_invoice(
    session: AsyncSession, user: User, provider: str, amount: Decimal
) -> tuple[ProviderInvoice, Decimal]:
    allow_new_invoice = PaymentService.provider_enabled(provider)
    payment = await get_or_create_pending_external_payment(
        session,
        user_id=user.id,
        amount=amount,
        payment_method=provider,
        allow_new_invoice=allow_new_invoice,
    )
    await session.commit()
    invoice = await resolve_pending_external_invoice(
        session,
        payment_record_id=payment.id,
        payment_method=provider,
        description=f"Пополнение баланса пользователя {user.id}",
        allow_new_invoice=allow_new_invoice,
    )
    # A customer can return with another amount while an older invoice remains
    # active. In that case we deliberately show the actual stored amount, not
    # the newly requested one, so the payment screen can never mislead them.
    return invoice, money(payment.amount)


def _stars_topup_expired(payment: Payment) -> bool:
    """A never-authorized Stars invoice may be safely replaced after TTL."""
    if payment.status != PENDING_PAYMENT_STATUS:
        return False
    expires_at = getattr(payment, "expires_at", None)
    if expires_at is not None:
        return expires_at <= datetime.now()
    created_at = getattr(payment, "created_at", None)
    if created_at is None:
        return False
    return created_at + timedelta(
        minutes=PaymentService.provider_invoice_lifetime_minutes("stars")
    ) <= datetime.now()


async def _create_stars_topup(
    session: AsyncSession, user: User, amount: Decimal, stars_amount: int
) -> Payment:
    """Создать один идемпотентный счёт Stars на пополнение баланса."""
    result = await session.execute(
        select(User)
        .where(User.id == user.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    locked_user = result.scalar_one_or_none()
    if not locked_user:
        raise CheckoutError("Пользователь не найден")
    result = await session.execute(
        select(Payment)
        .where(
            Payment.user_id == locked_user.id,
            Payment.payment_method == "stars_topup",
            Payment.order_id.is_(None),
            Payment.status.in_([PENDING_PAYMENT_STATUS, STARS_AUTHORIZED_PAYMENT_STATUS]),
        )
        .order_by(Payment.id.desc())
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    existing = result.scalars().first()
    if existing:
        if _stars_topup_expired(existing):
            existing.status = "FAILED"
            existing.completed_at = datetime.now()
        elif money(existing.amount) == amount:
            return existing
        else:
            raise CheckoutError(
                "У вас уже есть незавершённый счёт Telegram Stars. Оплатите его или дождитесь отмены."
            )
    payment = Payment(
        user_id=locked_user.id,
        amount=amount,
        provider_amount=stars_amount,
        payment_method="stars_topup",
        external_order_id=f"stars-topup-{uuid4().hex}",
        idempotency_key=uuid4().hex,
        status=PENDING_PAYMENT_STATUS,
        expires_at=datetime.now() + timedelta(
            minutes=PaymentService.provider_invoice_lifetime_minutes("stars")
        ),
    )
    session.add(payment)
    await session.flush()
    return payment


async def _prepare_topup_payment(
    session: AsyncSession,
    state: FSMContext,
    telegram_user_id: int,
    amount: Decimal,
) -> TopupPaymentResult:
    """Создать счёт для суммы из кнопки или ручного ввода."""
    data = await state.get_data()
    provider = data.get("topup_method")
    if provider not in {"yookassa", "yoomoney", "heleket", "lava", "cryptobot", "stars"}:
        raise CheckoutError("Сначала выберите способ оплаты")
    if provider == "stars" and not PaymentService.provider_enabled(provider):
        raise CheckoutError("Этот способ оплаты не подключен")

    user = await _get_user(session, telegram_user_id)
    if not user:
        raise CheckoutError("Пользователь не найден")

    if provider == "stars":
        stars_payment = await _create_stars_topup(
            session,
            user,
            amount,
            _stars_amount(amount),
        )
        await session.commit()
        return TopupPaymentResult(
            provider=provider,
            amount=amount,
            stars_payment=stars_payment,
            stars=_stored_stars_amount(stars_payment),
        )

    invoice, invoice_amount = await _create_topup_invoice(
        session, user, provider, amount
    )
    return TopupPaymentResult(
        provider=provider, amount=invoice_amount, invoice=invoice
    )


async def _send_stars_topup_invoice(message: Message, result: TopupPaymentResult) -> None:
    """Отправить Telegram invoice; Stars всегда выставляются отдельным сообщением."""
    if not result.stars_payment or not result.stars:
        raise CheckoutError("Не удалось создать счёт Telegram Stars")
    await message.answer_invoice(
        title="Пополнение баланса",
        description=f"Пополнение баланса на {result.amount:.2f} ₽",
        payload=_stars_topup_payload(result.stars_payment),
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение баланса", amount=result.stars)],
    )


def _topup_success_keyboard(payment_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if payment_url:
        rows.append([InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)])
    rows.append([
        InlineKeyboardButton(text="⬅️ К балансу", callback_data="menu_balance"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "topup_manual_amount")
async def enter_topup_manual_amount(callback: CallbackQuery, state: FSMContext):
    """Перейти от готовых сумм к вводу собственной."""
    data = await state.get_data()
    if not data.get("topup_method"):
        await callback.answer("Сначала выберите способ оплаты", show_alert=True)
        return
    await state.set_state(TopupStates.waiting_amount)
    await callback.message.edit_text(
        "Введите сумму пополнения в рублях (минимум 1 ₽):",
        reply_markup=_topup_manual_amount_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("topup_amount_"))
async def process_topup_quick_amount(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    """Создать счёт по одной из готовых сумм."""
    try:
        amount = _parse_amount((callback.data or "").removeprefix("topup_amount_"))
        if amount not in {
            Decimal("100"), Decimal("250"), Decimal("500"),
            Decimal("1000"), Decimal("2500"), Decimal("5000"),
        }:
            raise ValueError("Выберите сумму из списка или укажите свою")
        result = await _prepare_topup_payment(
            session,
            state,
            callback.from_user.id,
            amount,
        )
    except (ValueError, CheckoutError) as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not create quick topup invoice")
        await callback.answer("Не удалось создать счет. Попробуйте позже.", show_alert=True)
        return

    await state.clear()
    if result.provider == "stars":
        try:
            await _send_stars_topup_invoice(callback.message, result)
        except Exception:
            logger.exception("Could not send Stars topup invoice")
            await callback.answer("Не удалось отправить счёт Telegram Stars", show_alert=True)
            return
        await callback.message.edit_text(
            f"⭐ Счёт на {result.stars} Stars отправлен отдельным сообщением.",
            reply_markup=_topup_success_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"💳 <b>Пополнение через {PAYMENT_METHOD_TITLES[result.provider]}</b>\n\n"
            f"Сумма: {result.amount:.2f} ₽\n\n"
            "Перейдите по ссылке для оплаты. Баланс пополнится после подтверждения провайдером.",
            parse_mode="HTML",
            reply_markup=_topup_success_keyboard(result.invoice.payment_url if result.invoice else None),
        )
    await callback.answer()


@router.message(TopupStates.waiting_amount)
async def process_topup_amount(message: Message, state: FSMContext, session: AsyncSession):
    state_data = await state.get_data()
    try:
        amount = _parse_amount(message.text)
        result = await _prepare_topup_payment(
            session,
            state,
            message.from_user.id,
            amount,
        )
    except (ValueError, CheckoutError) as exc:
        await session.rollback()
        await edit_input_screen(
            message,
            state,
            f"❌ {exc}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⛔ Отмена", callback_data="balance_topup_menu")
            ]]),
            state_data=state_data,
        )
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not create topup invoice")
        await edit_input_screen(
            message,
            state,
            "❌ Не удалось создать счёт. Попробуйте позже.",
            reply_markup=_topup_manual_amount_keyboard(),
            state_data=state_data,
        )
        return

    if result.provider == "stars":
        try:
            await _send_stars_topup_invoice(message, result)
        except Exception:
            logger.exception("Could not send Stars topup invoice")
            await edit_input_screen(
                message,
                state,
                "❌ Не удалось отправить счёт Telegram Stars.",
                reply_markup=_topup_success_keyboard(),
                state_data=state_data,
            )
            return
        await edit_input_screen(
            message,
            state,
            f"⭐ Счёт на {result.stars} Stars отправлен отдельным сообщением.",
            reply_markup=_topup_success_keyboard(),
            state_data=state_data,
        )
        await state.clear()
        return

    await edit_input_screen(
        message,
        state,
        f"💳 <b>Пополнение баланса через {PAYMENT_METHOD_TITLES[result.provider]}</b>\n\n"
        f"Сумма: {result.amount:.2f} ₽\n\n"
        "Перейдите по ссылке для оплаты. Баланс пополнится после подтверждения провайдером.",
        reply_markup=_topup_success_keyboard(result.invoice.payment_url if result.invoice else None),
        state_data=state_data,
    )
    await state.clear()


@router.pre_checkout_query(F.invoice_payload.startswith("stars_topup:"))
async def authorize_stars_topup(pre_checkout_query: PreCheckoutQuery, session: AsyncSession):
    """Подтвердить счёт Stars, созданный для пополнения баланса."""
    # A toggle must prevent only new invoices. Telegram may ask pre-checkout
    # for an invoice issued seconds earlier, and its durable local record is
    # the authoritative permission to continue.
    parsed = _parse_stars_topup_payload(pre_checkout_query.invoice_payload)
    if not parsed:
        await pre_checkout_query.answer(ok=False, error_message="Некорректный платёж")
        return
    payment_id, idempotency_key = parsed
    try:
        result = await session.execute(
            select(Payment)
            .where(Payment.id == payment_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        payment = result.scalar_one_or_none()
        if (
            not payment
            or payment.payment_method != "stars_topup"
            or payment.idempotency_key != idempotency_key
            or payment.order_id is not None
            or payment.status not in {PENDING_PAYMENT_STATUS, STARS_AUTHORIZED_PAYMENT_STATUS}
            or (
                payment.status == PENDING_PAYMENT_STATUS
                and _stars_topup_expired(payment)
            )
            or payment.user_id is None
        ):
            raise CheckoutError("Счёт недоступен")
        user = await _get_user(session, pre_checkout_query.from_user.id)
        if not user or user.id != payment.user_id:
            raise CheckoutError("Счёт недоступен")
        if (
            pre_checkout_query.currency != "XTR"
            or pre_checkout_query.total_amount != _stored_stars_amount(payment)
        ):
            raise CheckoutError("Некорректная сумма")
        payment.status = STARS_AUTHORIZED_PAYMENT_STATUS
        await session.commit()
    except CheckoutError as exc:
        await session.rollback()
        await pre_checkout_query.answer(ok=False, error_message=str(exc))
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not authorize Stars balance topup")
        await pre_checkout_query.answer(ok=False, error_message="Не удалось проверить платёж")
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment.invoice_payload.startswith("stars_topup:"))
async def complete_stars_topup(message: Message, session: AsyncSession):
    """Идемпотентно начислить баланс по успешной оплате Telegram Stars."""
    successful_payment = message.successful_payment
    parsed = _parse_stars_topup_payload(successful_payment.invoice_payload)
    if not parsed:
        return
    payment_id, idempotency_key = parsed
    try:
        result = await session.execute(
            select(Payment)
            .where(Payment.id == payment_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        payment = result.scalar_one_or_none()
        if (
            not payment
            or payment.payment_method != "stars_topup"
            or payment.idempotency_key != idempotency_key
            or payment.order_id is not None
            or payment.status not in {PENDING_PAYMENT_STATUS, STARS_AUTHORIZED_PAYMENT_STATUS, SUCCESS_PAYMENT_STATUS}
            or successful_payment.currency != "XTR"
            or successful_payment.total_amount != _stored_stars_amount(payment)
        ):
            raise CheckoutError("Некорректный платёж Telegram Stars")
        result = await session.execute(
            select(User)
            .where(User.id == payment.user_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        user = result.scalar_one_or_none()
        if not user or user.telegram_id != message.from_user.id:
            raise CheckoutError("Платёж привязан к другому пользователю")
        result = await session.execute(
            select(Payment)
            .where(
                Payment.payment_method == "stars_topup",
                Payment.payment_id == successful_payment.telegram_payment_charge_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        charge_payment = result.scalar_one_or_none()
        if charge_payment and charge_payment.id != payment.id:
            raise CheckoutError("Идентификатор Telegram Stars уже использован")
        if payment.status == SUCCESS_PAYMENT_STATUS:
            if payment.payment_id != successful_payment.telegram_payment_charge_id:
                raise CheckoutError("Идентификатор Telegram Stars уже использован")
            await session.commit()
            return
        balance_after = money(user.balance) + money(payment.amount)
        ledger_result = await record_topup(
            session,
            user_id=user.id,
            amount=payment.amount,
            provider="Telegram Stars",
            payment_id=successful_payment.telegram_payment_charge_id,
            balance_after=balance_after,
        )
        if not ledger_result.created:
            raise CheckoutError("Пополнение Telegram Stars уже было учтено")
        payment.payment_id = successful_payment.telegram_payment_charge_id
        payment.status = SUCCESS_PAYMENT_STATUS
        payment.completed_at = datetime.now()
        user.balance = balance_after
        await session.commit()
    except CheckoutError as exc:
        await session.rollback()
        logger.warning("Telegram Stars topup rejected: %s", exc)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not complete Telegram Stars balance topup")
        return

    # A Stars invoice issued by an older version may have originated in a
    # group. The payment must still be completed, but its amount and balance
    # navigation must never be published to that group.
    await message.bot.send_message(
        message.from_user.id,
        f"✅ Баланс пополнен на {format_rubles(payment.amount)} ₽.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Открыть баланс", callback_data="menu_balance")]
        ]),
    )
