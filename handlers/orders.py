"""Обработчик заказов"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import Order, OrderBatch, User, Product
from utils.service import get_accounts_for_order, create_accounts_file
from utils.checkout import CheckoutError, cancel_pending_order, CompletedOrder
from utils.fulfillment import deliver_completed_order
from utils.keyboards import get_back_keyboard, get_orders_keyboard, get_order_detail_keyboard
from utils.private_chat import apply_private_chat_guard
from aiogram.types import BufferedInputFile
from html import escape
from utils.stars_catalog import is_premium_product, is_virtual_product
import logging

logger = logging.getLogger(__name__)

router = Router()
apply_private_chat_guard(router)


@router.callback_query(F.data == "my_orders")
async def show_orders_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать заказы (callback)"""
    await state.clear()
    user_id = callback.from_user.id

    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    stmt = (
        select(Order)
        .where(Order.user_id == user.id, Order.batch_id.is_(None))
        .order_by(Order.created_at.desc())
    )
    result = await session.execute(stmt)
    orders = result.scalars().all()
    batch_result = await session.execute(
        select(OrderBatch)
        .where(OrderBatch.user_id == user.id)
        .order_by(OrderBatch.created_at.desc())
    )
    batches = batch_result.scalars().all()

    if not orders and not batches:
        await callback.message.edit_text(
            "У вас пока нет заказов",
            reply_markup=get_back_keyboard("back_to_menu"),
        )
        await callback.answer()
        return

    batch_buttons = []
    status_emoji = {
        "PENDING_PAYMENT": "⏳",
        "PAID": "✅",
        "PARTIAL": "⚠️",
        "COMPLETED": "✔️",
        "CANCELLED": "❌",
    }
    for batch in batches:
        batch_buttons.append([InlineKeyboardButton(
            text=f"{status_emoji.get(batch.status, '❓')} Корзина #{batch.id} — {batch.total_amount:.2f} ₽",
            callback_data=f"batch_view_{batch.id}",
        )])
    legacy_keyboard = get_orders_keyboard(orders)
    await callback.message.edit_text(
        "📦 Ваши заказы:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=batch_buttons + legacy_keyboard.inline_keyboard)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("batch_view_"))
async def show_batch_detail(callback: CallbackQuery, session: AsyncSession):
    """Показать состав пакетного оформления и единую кнопку оплаты."""
    try:
        batch_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректное оформление", show_alert=True)
        return
    user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    batch = await session.scalar(select(OrderBatch).where(
        OrderBatch.id == batch_id, OrderBatch.user_id == (user.id if user else -1)
    ))
    if not batch:
        await callback.answer("Оформление не найдено", show_alert=True)
        return
    rows = (await session.execute(
        select(Order, Product).join(Product, Product.id == Order.product_id)
        .where(Order.batch_id == batch.id)
        .order_by(Order.id)
    )).all()
    lines = [f"📦 <b>Корзина #{batch.id}</b>", "", f"Статус: <b>{batch.status}</b>", ""]
    for index, (order, product) in enumerate(rows, 1):
        lines.append(
            f"{index}. {escape(product.name)} — @{escape(order.target_username or 'не указан')} "
            f"({order.quantity} шт.) — {order.total_amount:.2f} ₽"
        )
    lines.extend(["", f"💰 Итого: <b>{batch.total_amount:.2f} ₽</b>"])
    buttons = []
    if batch.status == "PENDING_PAYMENT":
        from handlers.cart import _batch_payment_keyboard
        buttons = _batch_payment_keyboard(batch.id).inline_keyboard
    buttons.extend([
        [InlineKeyboardButton(text=f"🔎 Позиция #{order.id}", callback_data=f"order_{order.id}")]
        for order, _product in rows
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="my_orders")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("order_"))
async def show_order_detail(callback: CallbackQuery, session: AsyncSession):
    """Показать детали заказа"""
    order_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    stmt = select(Order).where(Order.id == order_id, Order.user_id == user.id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Получаем товар
    stmt_product = select(Product).where(Product.id == order.product_id)
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    status_emoji = {
        "ОЖИДАЕТ ОПЛАТЫ": "⏳",
        "ОПЛАЧЕНО": "✅",
        "ВЫПОЛНЕНО": "✔️",
        "ОТМЕНЕНО": "❌"
    }.get(order.status, "❓")

    if is_premium_product(product):
        quantity_line = f"💎 Срок: {product.premium_months} мес.\n"
        price_line = f"💰 Цена тарифа: {order.price_per_unit:.2f} ₽\n"
    else:
        quantity_line = f"📊 Количество: {order.quantity} шт.\n"
        price_line = f"💰 Цена за единицу: {order.price_per_unit:.2f} ₽\n"
    text = f"""📦 <b>Заказ #{order.id}</b>

{status_emoji} Статус: {order.status}
📦 Товар: {escape(product.name) if product else 'Неизвестно'}
{quantity_line}{price_line}
"""

    if getattr(order, "discount_amount", 0) > 0:
        text += f"🎁 Скидка: {order.discount_amount:.2f} ₽\n"
    elif order.discount > 0:
        text += f"🎁 Скидка: {order.discount}%\n"

    text += f"💰 Итого: {order.total_amount:.2f} ₽\n"

    if product and is_virtual_product(product):
        recipient_icon = "💎" if is_premium_product(product) else "⭐"
        fulfillment_label = {
            "PENDING": "Ожидает отправки",
            "SENDING": "Отправляется",
            "UNKNOWN": "Требуется проверка",
            "FAILED": "Не отправлен — можно повторить",
            "SENT": "Отправлен",
        }.get(order.fulfillment_status, order.fulfillment_status)
        text += f"{recipient_icon} Получатель: @{escape(order.target_username or 'не указан')}\n"
        text += f"📤 Отправка: {escape(fulfillment_label)}\n"
        if order.fulfillment_status == "UNKNOWN":
            text += "⚠️ Результат Fragment не подтверждён. Не повторяйте заказ — поддержка проверит отправку.\n"
        elif order.fulfillment_status == "SENDING":
            text += "⏳ Отправка выполняется. Повтор пока недоступен.\n"
        elif order.fulfillment_status == "FAILED":
            text += "⚠️ Fragment отклонил последнюю попытку — её можно безопасно повторить.\n"

    if order.payment_method:
        text += f"💳 Способ оплаты: {order.payment_method}\n"

    text += f"📅 Дата создания: {order.created_at.strftime('%d.%m.%Y %H:%M')}\n"

    if order.paid_at:
        text += f"✅ Оплачен: {order.paid_at.strftime('%d.%m.%Y %H:%M')}\n"

    if order.completed_at:
        text += f"✔️ Выполнен: {order.completed_at.strftime('%d.%m.%Y %H:%M')}\n"

    await callback.message.edit_text(
        text,
        reply_markup=get_order_detail_keyboard(
            order_id, order.status, getattr(order, "fulfillment_status", None)
        ),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_order_"))
async def pay_order(callback: CallbackQuery, session: AsyncSession):
    """Оплатить неоплаченный заказ"""
    order_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    stmt = select(Order).where(Order.id == order_id, Order.user_id == user.id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    if order.status != "ОЖИДАЕТ ОПЛАТЫ":
        await callback.answer("Заказ уже оплачен или отменен", show_alert=True)
        return

    # Получаем товар
    stmt_product = select(Product).where(Product.id == order.product_id)
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    # Показываем способы оплаты
    from utils.keyboards import get_payment_methods_keyboard

    quantity_line = (
        f"💎 Срок: {product.premium_months} мес."
        if product and is_premium_product(product)
        else f"Количество: {order.quantity} шт."
    )
    text = f"""📦 <b>Заказ #{order.id}</b>

Товар: {escape(product.name) if product else 'Неизвестно'}
{quantity_line}
💰 Итого: {order.total_amount:.2f} ₽

Выберите категорию оплаты:"""

    await callback.message.edit_text(
        text,
        reply_markup=get_payment_methods_keyboard(order.id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_order_"))
async def cancel_order_from_detail(callback: CallbackQuery, session: AsyncSession):
    """Отменить заказ и снять резерв в единой блокируемой транзакции."""
    try:
        order_id = int(callback.data.split("_")[2])
        await cancel_pending_order(
            session,
            order_id,
            telegram_user_id=callback.from_user.id,
        )
        await session.commit()
    except (ValueError, CheckoutError) as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not cancel order")
        await callback.answer("Не удалось отменить заказ", show_alert=True)
        return

    from utils.keyboards import get_back_keyboard
    await callback.message.edit_text(
        "❌ Заказ отменен\n\n"
        "✅ Товар возвращен в каталог",
        reply_markup=get_back_keyboard("my_orders")
    )
    await callback.answer("Заказ отменен, товар возвращен на склад")


@router.callback_query(F.data.startswith("download_"))
async def download_order(callback: CallbackQuery, session: AsyncSession):
    """Скачать товар из заказа"""
    order_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    stmt = select(Order).where(Order.id == order_id, Order.user_id == user.id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    if order.status != "ВЫПОЛНЕНО":
        await callback.answer("Заказ еще не выполнен", show_alert=True)
        return

    product = await session.get(Product, order.product_id)
    if product and is_virtual_product(product):
        if order.fulfillment_status == "SENT":
            await callback.answer(
                "💎 Premium уже отправлен" if is_premium_product(product) else "⭐ Звёзды уже отправлены",
                show_alert=True,
            )
            return
        if order.fulfillment_status == "UNKNOWN":
            await callback.answer(
                "Результат Fragment не подтверждён. Повтор заблокирован — обратитесь в поддержку.",
                show_alert=True,
            )
            return
        ok = await deliver_completed_order(
            callback.bot,
            CompletedOrder(order=order, accounts=[], already_completed=True),
        )
        await session.refresh(order)
        if ok:
            answer = "✅ Отправка подтверждена"
        elif order.fulfillment_status == "UNKNOWN":
            answer = "Результат не подтверждён. Повтор заблокирован — обратитесь в поддержку."
        elif order.fulfillment_status == "SENDING":
            answer = "Отправка уже выполняется; повторный запрос не отправлен."
        else:
            answer = "Fragment отклонил отправку. Повтор доступен в карточке заказа."
        await callback.answer(answer, show_alert=True)
        return

    try:
        # Получаем аккаунты
        accounts = await get_accounts_for_order(session, order_id)

        if len(accounts) != order.quantity:
            logger.error(
                "Completed order %s has %s accounts instead of %s",
                order.id,
                len(accounts),
                order.quantity,
            )
            await callback.answer(
                "Не удалось получить полный состав заказа. Обратитесь в поддержку.",
                show_alert=True,
            )
            return

        # Создаем файл
        file_obj = await create_accounts_file(accounts)

        await callback.message.answer_document(
            BufferedInputFile(
                file_obj.read(),
                filename=file_obj.name
            ),
            caption=f"📦 Товар по заказу #{order_id}"
        )
        await callback.answer("✅ Файл отправлен")

    except Exception as e:
        logger.error(f"Error downloading order {order_id}: {e}")
        await callback.answer("Ошибка при загрузке товара", show_alert=True)


@router.callback_query(F.data.startswith("retry_delivery_"))
async def retry_stars_delivery(callback: CallbackQuery, session: AsyncSession):
    """Explicitly retry a failed Fragment delivery without changing payment."""
    try:
        order_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный заказ", show_alert=True)
        return
    user = (await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )).scalar_one_or_none()
    order = (await session.execute(
        select(Order).where(Order.id == order_id, Order.user_id == (user.id if user else -1))
    )).scalar_one_or_none()
    if not order or order.status != "ВЫПОЛНЕНО":
        await callback.answer("Заказ недоступен", show_alert=True)
        return
    product = await session.get(Product, order.product_id)
    if not product or not is_virtual_product(product):
        await callback.answer("Повторная отправка доступна только для Stars и Premium", show_alert=True)
        return
    if order.fulfillment_status == "UNKNOWN":
        await callback.answer(
            "Результат Fragment не подтверждён. Повтор заблокирован во избежание двойного списания; обратитесь в поддержку.",
            show_alert=True,
        )
        return
    if order.fulfillment_status not in {"FAILED", "SENDING"}:
        await callback.answer("Действие недоступно для текущего статуса отправки", show_alert=True)
        return
    ok = await deliver_completed_order(
        callback.bot,
        CompletedOrder(order=order, accounts=[], already_completed=True),
    )
    await session.refresh(order)
    if ok:
        answer = "✅ Premium отправлен" if is_premium_product(product) else "✅ Stars отправлены"
    elif order.fulfillment_status == "UNKNOWN":
        answer = "Результат не подтверждён. Повтор заблокирован — обратитесь в поддержку."
    elif order.fulfillment_status == "SENDING":
        answer = "Отправка уже выполняется; повторный запрос не отправлен."
    else:
        answer = "Fragment отклонил отправку. Её можно повторить позже из заказа."
    await callback.answer(answer, show_alert=True)
