"""Обработчики безопасной оплаты заказов."""
from __future__ import annotations

import logging
from decimal import Decimal
from html import escape
from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    PreCheckoutQuery,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderBatch, Payment, Product, User
from utils.checkout import (
    CheckoutError,
    FAILED_PAYMENT_STATUS,
    PENDING_PAYMENT_STATUS,
    authorize_telegram_stars_invoice,
    complete_telegram_stars_payment,
    complete_telegram_stars_batch_payment,
    get_or_create_pending_external_payment,
    get_or_create_pending_stars_payment,
    get_or_create_pending_stars_batch_payment,
    complete_external_batch_payment,
    cancel_pending_batch,
    pay_order_batch_from_balance,
    pay_order_from_balance,
    pay_orders_from_balance,
    resolve_pending_external_invoice,
    validate_telegram_stars_invoice,
)
from utils.fulfillment import deliver_completed_order
from utils.keyboards import get_payment_category_methods_keyboard
from utils.money import ZERO, format_rubles, money
from utils.private_chat import apply_private_chat_guard
from utils.payments import (
    PaymentService,
    ProviderInvoice,
    get_enabled_category_methods,
    get_payment_category_title,
    stars_amount_for_rubles,
)

logger = logging.getLogger(__name__)
router = Router()
apply_private_chat_guard(router)

PROVIDER_TITLES = {
    "yookassa": "ЮKassa",
    "yoomoney": "ЮMoney",
    "lava": "Lava",
    "heleket": "Heleket",
    "cryptobot": "CryptoBot",
}


def stars_amount(rubles: Decimal | float | int | str) -> int:
    """Количество Stars по действующему курсу для нового счёта."""
    return stars_amount_for_rubles(rubles)


def _stars_payload(payment: Payment) -> str:
    """Неподделываемая ссылка invoice Stars на заранее созданный Payment."""
    return f"stars:{payment.id}:{payment.idempotency_key}"


def _parse_stars_payload(payload: str | None) -> tuple[int, str] | None:
    """Распарсить компактный payload Telegram Stars без небезопасных split."""
    parts = (payload or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "stars" or not parts[1].isdigit():
        return None
    payment_id, idempotency_key = int(parts[1]), parts[2]
    if payment_id <= 0 or len(idempotency_key) != 32:
        return None
    return payment_id, idempotency_key


async def _owned_pending_order(
    session: AsyncSession, telegram_user_id: int, order_id: int
) -> tuple[User, Order] | None:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        return None
    result = await session.execute(
        select(Order).where(
            Order.id == order_id,
            Order.user_id == user.id,
            Order.status == "ОЖИДАЕТ ОПЛАТЫ",
        )
    )
    order = result.scalar_one_or_none()
    return (user, order) if order else None


async def _create_external_invoice(
    session: AsyncSession,
    *,
    user: User,
    order: Order,
    provider: str,
) -> ProviderInvoice | None:
    """Создать/вернуть один счёт для заказа через безопасный state machine."""
    allow_new_invoice = PaymentService.provider_enabled(provider)
    payment = await get_or_create_pending_external_payment(
        session,
        user_id=user.id,
        amount=order.total_amount,
        payment_method=provider,
        order_id=order.id,
        allow_new_invoice=allow_new_invoice,
    )
    await session.commit()
    return await resolve_pending_external_invoice(
        session,
        payment_record_id=payment.id,
        payment_method=provider,
        description=f"Заказ #{order.id}",
        allow_new_invoice=allow_new_invoice,
    )


async def _show_external_payment(
    callback: CallbackQuery,
    session: AsyncSession,
    provider: str,
    title: str,
    *,
    order_id: int | None = None,
) -> None:
    if order_id is None:
        try:
            order_id = int(callback.data.rsplit("_", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("Некорректный заказ", show_alert=True)
            return
    owned = await _owned_pending_order(session, callback.from_user.id, order_id)
    if not owned:
        await callback.answer("Заказ уже оплачен, отменен или не найден", show_alert=True)
        return
    user, order = owned
    try:
        invoice = await _create_external_invoice(
            session, user=user, order=order, provider=provider
        )
    except CheckoutError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not create %s invoice for order %s", provider, order.id)
        await callback.answer("Не удалось создать счет. Попробуйте позже.", show_alert=True)
        return
    if not invoice:
        await callback.answer("Не удалось создать счет. Проверьте настройки оплаты.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=invoice.payment_url)],
            [InlineKeyboardButton(text="◀️ К заказу", callback_data=f"pay_order_{order.id}")],
        ]
    )
    await callback.message.edit_text(
        f"💳 <b>Оплата через {title}</b>\n\n"
        f"Сумма: {order.total_amount:.2f} ₽\n\n"
        "После подтверждения платежа товар будет выдан автоматически.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_balance_"))
async def pay_from_balance(callback: CallbackQuery, session: AsyncSession):
    """Показать подтверждение перед списанием средств с баланса."""
    try:
        order_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный заказ", show_alert=True)
        return

    owned = await _owned_pending_order(session, callback.from_user.id, order_id)
    if not owned:
        await callback.answer(
            "Заказ уже оплачен, отменен или не найден", show_alert=True
        )
        return
    user, order = owned
    product = await session.get(Product, order.product_id)
    amount = money(order.total_amount)
    balance = money(user.balance)
    if balance < amount:
        await callback.answer(
            f"Недостаточно средств. Требуется: {format_rubles(amount)} ₽",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        "💳 <b>Подтверждение оплаты</b>\n\n"
        f"Заказ: <b>#{order.id}</b>\n"
        f"Товар: <b>{escape(product.name) if product else 'Неизвестно'}</b>\n"
        f"Количество: <b>{order.quantity} шт.</b>\n"
        f"К списанию: <b>{format_rubles(amount)} ₽</b>\n"
        f"Баланс после оплаты: <b>{format_rubles(balance - amount)} ₽</b>\n\n"
        "Подтвердить оплату с баланса?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=f"confirm_balance_{order.id}",
                    ),
                    InlineKeyboardButton(
                        text="⛔ Отмена", callback_data=f"pay_order_{order.id}"
                    ),
                ]
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_balance_"))
async def confirm_balance_payment(callback: CallbackQuery, session: AsyncSession):
    """Списать баланс только после явного подтверждения пользователя."""
    try:
        order_id = int(callback.data.rsplit("_", 1)[1])
        completed = await pay_order_from_balance(session, order_id, callback.from_user.id)
        await session.commit()
    except (ValueError, CheckoutError) as exc:
        await session.rollback()
        await callback.answer(str(exc) if str(exc) else "Не удалось оплатить заказ", show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Balance payment failed")
        await callback.answer("Не удалось оплатить заказ", show_alert=True)
        return

    delivered = await deliver_completed_order(callback.bot, completed)
    delivery_text = (
        "Telegram Stars отправлены получателю (или будут повторно отправлены из карточки заказа)."
        if not completed.accounts
        else "Товар отправлен отдельным сообщением; его также можно скачать из заказов."
    )
    if not delivered and not completed.accounts:
        delivery_text = "Оплата принята. Отправка Stars временно не завершилась — повторите из карточки заказа."
    await callback.message.edit_text(
        f"✅ Заказ #{completed.order.id} успешно оплачен с баланса.\n\n{delivery_text}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "pay_cart_balance")
async def pay_cart_from_balance(callback: CallbackQuery, session: AsyncSession):
    """Показать подтверждение атомарной оплаты всей корзины с баланса."""
    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    orders = list(
        (
            await session.scalars(
                select(Order)
                .where(
                    Order.user_id == user.id,
                    Order.status == "ОЖИДАЕТ ОПЛАТЫ",
                    Order.batch_id.is_(None),
                )
                .order_by(Order.id)
            )
        ).all()
    )
    if len(orders) < 2:
        await callback.answer(
            "В корзине недостаточно ожидающих заказов", show_alert=True
        )
        return
    total = sum((money(order.total_amount) for order in orders), ZERO)
    balance = money(user.balance)
    if balance < total:
        await callback.answer(
            f"Недостаточно средств. Требуется: {format_rubles(total)} ₽",
            show_alert=True,
        )
        return
    await callback.message.edit_text(
        "🛒 <b>Подтверждение оплаты корзины</b>\n\n"
        f"Заказов: <b>{len(orders)}</b>\n"
        f"К списанию: <b>{format_rubles(total)} ₽</b>\n"
        f"Баланс после оплаты: <b>{format_rubles(balance - total)} ₽</b>\n\n"
        "Подтвердить оплату всей корзины с баланса?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить", callback_data="confirm_cart_balance"
                    ),
                    InlineKeyboardButton(text="⛔ Отмена", callback_data="my_orders"),
                ]
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "confirm_cart_balance")
async def confirm_cart_balance_payment(
    callback: CallbackQuery, session: AsyncSession
):
    """Атомарно оплатить корзину после явного подтверждения пользователя."""
    try:
        order_ids = list(
            (
                await session.scalars(
                    select(Order.id)
                    .join(User, User.id == Order.user_id)
                    .where(
                        User.telegram_id == callback.from_user.id,
                        Order.status == "ОЖИДАЕТ ОПЛАТЫ",
                        Order.batch_id.is_(None),
                    )
                    .order_by(Order.id)
                )
            ).all()
        )
        if len(order_ids) < 2:
            raise CheckoutError("В корзине недостаточно ожидающих заказов")
        completed_orders = await pay_orders_from_balance(
            session,
            order_ids,
            callback.from_user.id,
        )
        await session.commit()
    except CheckoutError as exc:
        await session.rollback()
        await callback.answer(str(exc) or "Не удалось оплатить корзину", show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Balance cart payment failed")
        await callback.answer("Не удалось оплатить корзину", show_alert=True)
        return

    delivery_errors = False
    for completed in completed_orders:
        try:
            await deliver_completed_order(callback.bot, completed)
        except Exception:
            # Заказ уже оплачен и доступен через «Мои заказы»; ошибка доставки
            # отдельного файла не должна откатывать единую финансовую операцию.
            delivery_errors = True
            logger.exception("Could not deliver completed cart order %s", completed.order.id)

    total = sum((money(item.order.total_amount) for item in completed_orders), ZERO)
    text = (
        "✅ <b>Корзина оплачена с баланса.</b>\n\n"
        f"Заказов: <b>{len(completed_orders)}</b>\n"
        f"Сумма: <b>{format_rubles(total)} ₽</b>\n\n"
    )
    if delivery_errors:
        text += "Часть файлов не удалось отправить автоматически. Они доступны в разделе «Мои заказы»."
    else:
        text += "Товары отправлены отдельными сообщениями и доступны в разделе «Мои заказы»."
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer()


async def _owned_pending_batch(
    session: AsyncSession, telegram_user_id: int, batch_id: int
) -> tuple[User, OrderBatch] | None:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_user_id))
    if not user:
        return None
    batch = await session.scalar(select(OrderBatch).where(
        OrderBatch.id == batch_id,
        OrderBatch.user_id == user.id,
        OrderBatch.status == "PENDING_PAYMENT",
    ))
    return (user, batch) if batch else None


async def _create_external_batch_invoice(
    session: AsyncSession, *, user: User, batch: OrderBatch, provider: str
) -> ProviderInvoice | None:
    allow_new_invoice = PaymentService.provider_enabled(provider)
    payment = await get_or_create_pending_external_payment(
        session,
        user_id=user.id,
        amount=batch.total_amount,
        payment_method=provider,
        batch_id=batch.id,
        allow_new_invoice=allow_new_invoice,
    )
    await session.commit()
    return await resolve_pending_external_invoice(
        session,
        payment_record_id=payment.id,
        payment_method=provider,
        description=f"Оформление #{batch.id}",
        allow_new_invoice=allow_new_invoice,
    )


async def _start_stars_batch_payment(
    callback: CallbackQuery, session: AsyncSession, batch_id: int
) -> None:
    if not PaymentService.provider_enabled("stars"):
        await callback.answer("Telegram Stars сейчас отключены", show_alert=True)
        return
    owned = await _owned_pending_batch(session, callback.from_user.id, batch_id)
    if not owned:
        await callback.answer("Оформление недоступно", show_alert=True)
        return
    _user, batch = owned
    stars_payment: Payment | None = None
    created = False
    invoice_sent = False
    try:
        stars_payment, created = await get_or_create_pending_stars_batch_payment(
            session,
            batch_id=batch.id,
            telegram_user_id=callback.from_user.id,
            stars_amount=stars_amount(batch.total_amount),
        )
        await session.commit()
        if not created:
            await callback.answer("Счёт Telegram Stars уже отправлен.", show_alert=True)
            return
        amount = stars_payment.provider_amount or stars_amount(stars_payment.amount)
        await callback.message.answer_invoice(
            title=f"Оформление #{batch.id}",
            description=f"Оплата корзины по оформлению #{batch.id}",
            payload=_stars_payload(stars_payment),
            currency="XTR",
            prices=[LabeledPrice(label=f"Оформление #{batch.id}", amount=amount)],
        )
        invoice_sent = True
        await callback.message.edit_text(
            f"⭐ <b>Оплата оформления #{batch.id}</b>\n\n"
            f"Счёт на {amount} Stars отправлен отдельным сообщением.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К оформлению", callback_data=f"batch_pay_{batch.id}")]
            ]), parse_mode="HTML",
        )
    except CheckoutError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        if stars_payment and created and not invoice_sent:
            try:
                pending = await session.get(Payment, stars_payment.id, with_for_update=True)
                if pending and pending.status == PENDING_PAYMENT_STATUS:
                    pending.status = FAILED_PAYMENT_STATUS
                    await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Could not release failed Stars batch invoice %s", stars_payment.id)
        logger.exception("Could not create Stars batch invoice %s", batch_id)
        await callback.answer("Не удалось создать счёт Telegram Stars", show_alert=True)
        return
    await callback.answer()


async def _show_external_batch_payment(
    callback: CallbackQuery, session: AsyncSession, provider: str, title: str,
    batch_id: int,
) -> None:
    owned = await _owned_pending_batch(session, callback.from_user.id, batch_id)
    if not owned:
        await callback.answer("Оформление уже оплачено, отменено или не найдено", show_alert=True)
        return
    user, batch = owned
    try:
        invoice = await _create_external_batch_invoice(
            session, user=user, batch=batch, provider=provider
        )
    except CheckoutError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not create %s batch invoice for %s", provider, batch_id)
        await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)
        return
    if not invoice:
        await callback.answer("Не удалось создать счёт. Проверьте настройки оплаты.", show_alert=True)
        return
    await callback.message.edit_text(
        f"💳 <b>Оплата оформления #{batch.id} через {title}</b>\n\n"
        f"Сумма: <b>{format_rubles(batch.total_amount)} ₽</b>\n\n"
        "После оплаты позиции будут отправлены получателям автоматически.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=invoice.payment_url)],
            [InlineKeyboardButton(text="◀️ К оформлению", callback_data=f"batch_pay_{batch.id}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("batch_cancel_"))
async def batch_cancel(callback: CallbackQuery, session: AsyncSession):
    try:
        batch_id = int(callback.data.rsplit("_", 1)[1])
        await cancel_pending_batch(session, batch_id, telegram_user_id=callback.from_user.id)
        await session.commit()
    except (TypeError, ValueError, CheckoutError) as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not cancel order batch %s", batch_id)
        await callback.answer("Не удалось отменить оформление", show_alert=True)
        return
    await callback.message.edit_text(
        f"❌ Оформление #{batch_id} отменено.\n\nТовары снова доступны для заказа.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart_open")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
        ]),
    )
    await callback.answer("Оформление отменено")


@router.callback_query(F.data.startswith("batch_pay_balance_"))
async def batch_pay_balance(callback: CallbackQuery, session: AsyncSession):
    try:
        batch_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректное оформление", show_alert=True)
        return
    owned = await _owned_pending_batch(session, callback.from_user.id, batch_id)
    if not owned:
        await callback.answer("Оформление недоступно", show_alert=True)
        return
    user, batch = owned
    balance = money(user.balance)
    total = money(batch.total_amount)
    if balance < total:
        await callback.answer(f"Недостаточно средств. Требуется: {format_rubles(total)} ₽", show_alert=True)
        return
    count = len((await session.scalars(select(Order.id).where(Order.batch_id == batch.id))).all())
    await callback.message.edit_text(
        "💳 <b>Подтверждение оплаты корзины</b>\n\n"
        f"Оформление: <b>#{batch.id}</b>\n"
        f"Позиций: <b>{count or 0}</b>\n"
        f"К списанию: <b>{format_rubles(total)} ₽</b>\n"
        f"Баланс после оплаты: <b>{format_rubles(balance - total)} ₽</b>\n\n"
        "Подтвердить оплату?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"batch_confirm_balance_{batch.id}"),
            InlineKeyboardButton(text="⛔ Отмена", callback_data=f"batch_pay_{batch.id}"),
        ]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("batch_confirm_balance_"))
async def batch_confirm_balance(callback: CallbackQuery, session: AsyncSession):
    try:
        batch_id = int(callback.data.rsplit("_", 1)[1])
        completed = await pay_order_batch_from_balance(session, batch_id, callback.from_user.id)
        await session.commit()
    except (TypeError, ValueError, CheckoutError) as exc:
        await session.rollback()
        await callback.answer(str(exc) or "Не удалось оплатить корзину", show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Batch balance payment failed")
        await callback.answer("Не удалось оплатить корзину", show_alert=True)
        return
    delivery_errors = 0
    for item in completed:
        if not await deliver_completed_order(callback.bot, item):
            delivery_errors += 1
    text = (
        f"✅ <b>Оформление #{batch_id} оплачено.</b>\n\n"
        f"Позиций: <b>{len(completed)}</b>\n"
    )
    text += ("⚠️ Часть отправок не завершилась. Повторите их из «Мои заказы»." if delivery_errors else "Все позиции отправлены получателям.")
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
    ]), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^batch_pay_(yookassa|yoomoney|heleket|lava|cryptobot)_\d+$"))
async def batch_pay_external(callback: CallbackQuery, session: AsyncSession):
    method, batch_id_text = callback.data.removeprefix("batch_pay_").rsplit("_", 1)
    await _show_external_batch_payment(callback, session, method, PROVIDER_TITLES[method], int(batch_id_text))


@router.callback_query(F.data.startswith("batch_pay_stars_"))
async def batch_pay_stars(callback: CallbackQuery, session: AsyncSession):
    try:
        batch_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректное оформление", show_alert=True)
        return
    await _start_stars_batch_payment(callback, session, batch_id)


@router.callback_query(F.data.startswith("batch_pay_category_"))
async def batch_choose_payment_category(callback: CallbackQuery, session: AsyncSession):
    try:
        category_part, batch_id_raw = callback.data.rsplit("_", 1)
        category = category_part.removeprefix("batch_pay_category_")
        batch_id = int(batch_id_raw)
    except (TypeError, ValueError):
        await callback.answer("Некорректная категория оплаты", show_alert=True)
        return
    methods = get_enabled_category_methods(category)
    if not methods:
        await callback.answer("В этой категории нет подключённых способов оплаты", show_alert=True)
        return
    if len(methods) == 1:
        method = methods[0]
        if method == "stars":
            await _start_stars_batch_payment(callback, session, batch_id)
        else:
            await _show_external_batch_payment(callback, session, method, PROVIDER_TITLES[method], batch_id)
        return
    owned = await _owned_pending_batch(session, callback.from_user.id, batch_id)
    if not owned:
        await callback.answer("Оформление недоступно", show_alert=True)
        return
    await callback.message.edit_text(
        f"{get_payment_category_title(category)}\n\nВыберите способ оплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=PROVIDER_TITLES[method], callback_data=f"batch_pay_{method}_{batch_id}")]
            for method in methods
        ] + [[InlineKeyboardButton(text="◀️ Назад", callback_data=f"batch_pay_{batch_id}")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^batch_pay_\d+$"))
async def batch_payment_screen(callback: CallbackQuery, session: AsyncSession):
    try:
        batch_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректное оформление", show_alert=True)
        return
    owned = await _owned_pending_batch(session, callback.from_user.id, batch_id)
    if not owned:
        await callback.answer("Оформление недоступно", show_alert=True)
        return
    _user, batch = owned
    from handlers.cart import _batch_payment_keyboard
    await callback.message.edit_text(
        f"📦 <b>Оформление #{batch.id}</b>\n\nСумма: <b>{format_rubles(batch.total_amount)} ₽</b>\n\nВыберите способ оплаты:",
        reply_markup=_batch_payment_keyboard(batch.id), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_category_"))
async def choose_payment_category(callback: CallbackQuery, session: AsyncSession):
    """Открыть единственный метод сразу или список внутри категории."""
    try:
        category_part, order_id_raw = (callback.data or "").rsplit("_", 1)
        category = category_part.removeprefix("pay_category_")
        order_id = int(order_id_raw)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная категория оплаты", show_alert=True)
        return

    methods = get_enabled_category_methods(category)
    if not methods:
        await callback.answer("В этой категории нет подключённых способов оплаты", show_alert=True)
        return

    if len(methods) == 1:
        method = methods[0]
        if method == "stars":
            await _start_stars_payment(callback, session, order_id)
        else:
            await _show_external_payment(
                callback,
                session,
                method,
                PROVIDER_TITLES[method],
                order_id=order_id,
            )
        return

    owned = await _owned_pending_order(session, callback.from_user.id, order_id)
    if not owned:
        await callback.answer("Заказ уже оплачен, отменен или не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"{get_payment_category_title(category)}\n\n"
        "Выберите способ оплаты:",
        reply_markup=get_payment_category_methods_keyboard(order_id, category),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_yookassa_"))
async def pay_yookassa(callback: CallbackQuery, session: AsyncSession):
    await _show_external_payment(callback, session, "yookassa", "ЮKassa")


@router.callback_query(F.data.startswith("pay_yoomoney_"))
async def pay_yoomoney(callback: CallbackQuery, session: AsyncSession):
    await _show_external_payment(callback, session, "yoomoney", "ЮMoney")


@router.callback_query(F.data.startswith("pay_heleket_"))
async def pay_heleket(callback: CallbackQuery, session: AsyncSession):
    await _show_external_payment(callback, session, "heleket", "Heleket")


@router.callback_query(F.data.startswith("pay_lava_"))
async def pay_lava(callback: CallbackQuery, session: AsyncSession):
    await _show_external_payment(callback, session, "lava", "Lava")


@router.callback_query(F.data.startswith("pay_cryptobot_"))
async def pay_cryptobot(callback: CallbackQuery, session: AsyncSession):
    await _show_external_payment(callback, session, "cryptobot", "CryptoBot")


async def _start_stars_payment(
    callback: CallbackQuery, session: AsyncSession, order_id: int
) -> None:
    """Создать счёт Telegram Stars для уже проверенного заказа."""
    if not PaymentService.provider_enabled("stars"):
        await callback.answer("Telegram Stars сейчас отключены", show_alert=True)
        return
    stars_payment: Payment | None = None
    created = False
    invoice_sent = False
    try:
        order_preview = await session.get(Order, order_id)
        if order_preview is None:
            raise CheckoutError("Заказ не найден")
        stars_payment, created = await get_or_create_pending_stars_payment(
            session,
            order_id=order_id,
            telegram_user_id=callback.from_user.id,
            stars_amount=stars_amount(order_preview.total_amount),
        )
        await session.commit()
        if not created:
            await callback.answer(
                "Счет Telegram Stars уже отправлен. Используйте ранее полученный счет.",
                show_alert=True,
            )
            return

        amount = stars_payment.provider_amount or stars_amount(stars_payment.amount)
        await callback.message.answer_invoice(
            title=f"Заказ #{order_id}",
            description=f"Оплата цифрового товара по заказу #{order_id}",
            payload=_stars_payload(stars_payment),
            currency="XTR",
            prices=[LabeledPrice(label=f"Заказ #{order_id}", amount=amount)],
        )
        invoice_sent = True
        try:
            await callback.message.edit_text(
                f"⭐ <b>Оплата Telegram Stars</b>\n\n"
                f"Счет на {amount} Stars отправлен отдельным сообщением.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К заказу", callback_data=f"pay_order_{order_id}")]
                    ]
                ),
                parse_mode="HTML",
            )
        except Exception:
            # Счет уже доставлен Telegram. Ошибка обновления навигационного
            # сообщения не должна делать такой платеж недействительным.
            logger.warning("Could not update Stars payment screen for order %s", order_id, exc_info=True)
    except CheckoutError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        # Если Telegram не принял invoice, разрешаем пользователю повторить
        # создание счета. Иначе запись PENDING заблокировала бы покупку навсегда.
        if stars_payment and created and not invoice_sent:
            try:
                result = await session.execute(
                    select(Payment)
                    .where(Payment.id == stars_payment.id)
                    .with_for_update()
                )
                pending_payment = result.scalar_one_or_none()
                if pending_payment and pending_payment.status == PENDING_PAYMENT_STATUS:
                    pending_payment.status = FAILED_PAYMENT_STATUS
                    await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Could not release failed Stars invoice %s", stars_payment.id)
        logger.exception("Could not create Stars invoice for order %s", order_id)
        await callback.answer("Не удалось создать счет Telegram Stars", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars(callback: CallbackQuery, session: AsyncSession):
    try:
        order_id = int(callback.data.rsplit("_", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректный заказ", show_alert=True)
        return
    await _start_stars_payment(callback, session, order_id)


@router.pre_checkout_query()
async def handle_pre_checkout_query(
    pre_checkout_query: PreCheckoutQuery, session: AsyncSession
):
    # This request refers to an invoice Telegram has already shown to the
    # customer. The enabled flag blocks only _new_ invoices; rejecting this
    # one after an admin toggle would charge the user inconsistently.
    parsed = _parse_stars_payload(pre_checkout_query.invoice_payload)
    if not parsed:
        await pre_checkout_query.answer(ok=False, error_message="Некорректный платеж")
        return
    payment_record_id, idempotency_key = parsed
    try:
        quoted = await validate_telegram_stars_invoice(
            session,
            payment_record_id=payment_record_id,
            idempotency_key=idempotency_key,
            telegram_user_id=pre_checkout_query.from_user.id,
        )
        if quoted is None:
            await session.rollback()
            await pre_checkout_query.answer(
                ok=False, error_message="Заказ недоступен для оплаты"
            )
            return
        _rubles, expected_stars = quoted
        if (
            pre_checkout_query.currency != "XTR"
            or pre_checkout_query.total_amount != expected_stars
        ):
            await session.rollback()
            await pre_checkout_query.answer(
                ok=False, error_message="Неверная сумма или валюта"
            )
            return

        # Commit the authorization before replying OK.  Telegram may send a
        # successful_payment immediately after this reply, so cancellation
        # must see the durable STARS_AUTHORIZED state first.
        authorized_amount = await authorize_telegram_stars_invoice(
            session,
            payment_record_id=payment_record_id,
            idempotency_key=idempotency_key,
            telegram_user_id=pre_checkout_query.from_user.id,
        )
        if authorized_amount is None:
            await session.rollback()
            await pre_checkout_query.answer(
                ok=False, error_message="Заказ недоступен для оплаты"
            )
            return
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Could not authorize Telegram Stars pre-checkout")
        await pre_checkout_query.answer(
            ok=False, error_message="Не удалось подтвердить платеж"
        )
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message, session: AsyncSession):
    payment = message.successful_payment
    parsed = _parse_stars_payload(payment.invoice_payload)
    if not parsed:
        return
    try:
        payment_record_id, idempotency_key = parsed
        quoted = await validate_telegram_stars_invoice(
            session,
            payment_record_id=payment_record_id,
            idempotency_key=idempotency_key,
            telegram_user_id=message.from_user.id,
            allow_completed=True,
        )
        if (
            quoted is None
            or payment.currency != "XTR"
            or payment.total_amount != quoted[1]
        ):
            raise CheckoutError("Некорректная сумма или валюта Telegram Stars")
        local_payment = await session.get(Payment, payment_record_id)
        if local_payment is None:
            raise CheckoutError("Платёж Telegram Stars не найден")
        if local_payment.batch_id:
            completed_batch = await complete_telegram_stars_batch_payment(
                session,
                payment_record_id=payment_record_id,
                idempotency_key=idempotency_key,
                telegram_user_id=message.from_user.id,
                telegram_charge_id=payment.telegram_payment_charge_id,
            )
            completed = None
        else:
            completed = await complete_telegram_stars_payment(
                session,
                payment_record_id=payment_record_id,
                idempotency_key=idempotency_key,
                telegram_user_id=message.from_user.id,
                telegram_charge_id=payment.telegram_payment_charge_id,
            )
            completed_batch = []
        await session.commit()
    except (ValueError, CheckoutError) as exc:
        await session.rollback()
        logger.warning("Telegram Stars payment was rejected: %s", exc)
        return
    except Exception:
        await session.rollback()
        logger.exception("Telegram Stars payment processing failed")
        return
    if completed is not None:
        if not completed.already_completed:
            await deliver_completed_order(message.bot, completed)
    else:
        for item in completed_batch:
            if not item.already_completed:
                await deliver_completed_order(message.bot, item)
