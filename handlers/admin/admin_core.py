"""Административные обработчики: core."""
from .admin_context import *
from aiogram.exceptions import TelegramAPIError

@router.callback_query(F.data == "admin_menu")
async def admin_menu_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Главное меню админки (callback)"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.clear()
    is_developer = await is_developer_async(callback.from_user.id, session)

    await callback.message.edit_text(
        "⚙️ <b>Пункт управления</b>\n\nВыберите раздел:",
        reply_markup=get_admin_menu_keyboard(is_developer=is_developer),
        parse_mode="HTML"
    )
    await callback.answer()


# Управление заказами
@router.callback_query(F.data == "admin_orders")
async def admin_orders_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Меню управления заказами"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        "📦 <b>Управление заказами</b>\n\nВыберите действие:",
        reply_markup=get_admin_orders_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_orders_all")
async def admin_orders_all(callback: CallbackQuery, session: AsyncSession):
    """Все заказы"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    stmt = select(Order).order_by(Order.created_at.desc()).limit(50)
    result = await session.execute(stmt)
    orders = result.scalars().all()

    if not orders:
        await callback.message.edit_text(
            "📭 Заказов пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders")]
            ]),
        )
        await callback.answer()
        return

    text = "📦 <b>Текущие заказы:</b>\n\n"
    buttons = []

    for order in orders:
        # Получаем пользователя
        stmt_user = select(User).where(User.id == order.user_id)
        result_user = await session.execute(stmt_user)
        user = result_user.scalar_one_or_none()

        # Получаем товар
        stmt_product = select(Product).where(Product.id == order.product_id)
        result_product = await session.execute(stmt_product)
        product = result_product.scalar_one_or_none()

        user_name_html = (
            f"@{escape(user.username)}"
            if user and user.username
            else escape(user.first_name if user else "Неизвестно")
        )
        # Inline-button text is not parsed as HTML, so preserve the readable
        # original there and escape only the HTML message body.
        user_name_button = (
            f"@{user.username}"
            if user and user.username
            else (user.first_name if user else "Неизвестно")
        )
        product_name = escape(product.name) if product else f"Товар ID: {order.product_id}"

        status_emoji = {
            "ОЖИДАЕТ ОПЛАТЫ": "⏳",
            "ОПЛАЧЕНО": "✅",
            "ВЫПОЛНЕНО": "✔️",
            "ОТМЕНЕНО": "❌"
        }.get(order.status, "❓")

        text += f"{status_emoji} <b>Заказ #{order.id}</b>\n"
        text += f"👤 Покупатель: {user_name_html}\n"
        text += f"📦 Товар: {product_name}\n"
        text += f"📊 Количество: {order.quantity} шт.\n"
        text += f"💰 Сумма: {order.total_amount:.2f} ₽\n"
        text += f"📋 Статус: {order.status}\n\n"

        # Добавляем кнопку для просмотра деталей и отмены
        if order.status in ["ОЖИДАЕТ ОПЛАТЫ", "ОПЛАЧЕНО"]:
            buttons.append([InlineKeyboardButton(
                text=f"📋 Заказ #{order.id} - {user_name_button}",
                callback_data=f"admin_order_detail_{order.id}"
            )])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_orders_search")
async def admin_orders_search_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать поиск заказа по ID"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_order_id)
    keyboard = _input_cancel_keyboard("admin_orders")
    await callback.message.edit_text("Введите ID заказа:", reply_markup=keyboard)
    await callback.answer()


@router.message(AdminStates.waiting_order_id)
async def admin_orders_search_result(message: Message, state: FSMContext, session: AsyncSession):
    """Результат поиска заказа"""
    try:
        order_id = int(message.text)

        stmt = select(Order).where(Order.id == order_id)
        result = await session.execute(stmt)
        order = result.scalar_one_or_none()

        if not order:
            await message.answer("Заказ не найден")
            await state.clear()
            return

        stmt_user = select(User).where(User.id == order.user_id)
        result_user = await session.execute(stmt_user)
        user = result_user.scalar_one_or_none()

        username_html = escape(user.username) if user and user.username else "N/A"
        text = f"""📦 <b>Заказ #{order.id}</b>

👤 Пользователь: @{username_html} (ID: {user.telegram_id if user else 'N/A'})
📦 Товар ID: {order.product_id}
📊 Количество: {order.quantity} шт.
💰 Сумма: {order.total_amount:.2f} ₽
📋 Статус: {order.status}
💳 Способ оплаты: {order.payment_method or 'N/A'}
📅 Создан: {order.created_at.strftime('%d.%m.%Y %H:%M')}
"""

        if order.paid_at:
            text += f"✅ Оплачен: {order.paid_at.strftime('%d.%m.%Y %H:%M')}\n"

        await message.answer(text, parse_mode="HTML")
        await state.clear()

    except ValueError:
        await message.answer(
            "Введите корректный ID заказа (число):",
            reply_markup=_input_cancel_keyboard("admin_orders"),
        )
    except (SQLAlchemyError, TypeError, AttributeError):
        logger.exception("Error searching order")
        await message.answer("Ошибка при поиске заказа")
        await state.clear()


@router.callback_query(F.data.startswith("admin_order_detail_"))
async def admin_order_detail(callback: CallbackQuery, session: AsyncSession):
    """Детали заказа для администратора"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    order_id = int(callback.data.split("_")[3])

    stmt = select(Order).where(Order.id == order_id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()

    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Получаем пользователя
    stmt_user = select(User).where(User.id == order.user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    # Получаем товар
    stmt_product = select(Product).where(Product.id == order.product_id)
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    user_name = (
        f"@{escape(user.username)}"
        if user and user.username
        else escape(user.first_name if user else "Неизвестно")
    )
    user_id_display = user.telegram_id if user else "N/A"
    product_name = escape(product.name) if product else f"Товар ID: {order.product_id}"

    text = f"""📦 <b>Заказ #{order.id}</b>

👤 Покупатель: {user_name}
🆔 ID пользователя: {user_id_display}
📦 Товар: {product_name}
📊 Количество: {order.quantity} шт.
💰 Цена за единицу: {order.price_per_unit:.2f} ₽
"""

    if getattr(order, "discount_amount", 0) > 0:
        text += f"🎁 Скидка: {order.discount_amount:.2f} ₽\n"
    elif order.discount > 0:
        text += f"🎁 Скидка: {order.discount}%\n"

    text += f"💰 Сумма: {order.total_amount:.2f} ₽\n"
    text += f"📋 Статус: {order.status}\n"

    if product and is_virtual_product(product):
        text += f"📤 Отправка Fragment: {escape(order.fulfillment_status)}\n"
        if order.fulfillment_status == "UNKNOWN":
            text += "⚠️ Результат не подтверждён — повтор запрещён до ручной сверки в Fragment.\n"
        if order.fulfillment_error:
            text += f"⚠️ Причина: {escape(order.fulfillment_error[:500])}\n"

    if order.payment_method:
        text += f"💳 Способ оплаты: {order.payment_method}\n"

    text += f"📅 Создан: {order.created_at.strftime('%d.%m.%Y %H:%M')}\n"

    if order.paid_at:
        text += f"✅ Оплачен: {order.paid_at.strftime('%d.%m.%Y %H:%M')}\n"

    if order.completed_at:
        text += f"✔️ Выполнен: {order.completed_at.strftime('%d.%m.%Y %H:%M')}\n"

    # Кнопки управления
    buttons = []
    if order.status == "ОЖИДАЕТ ОПЛАТЫ":
        buttons.append([InlineKeyboardButton(
            text="❌ Отменить заказ",
            callback_data=f"admin_order_cancel_{order.id}"
        )])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders_all")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_order_cancel_"))
async def admin_cancel_order(callback: CallbackQuery, session: AsyncSession):
    """Отменить заказ (администратор)"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    order_id = int(callback.data.split("_")[3])

    from utils.checkout import CheckoutError, cancel_pending_order

    try:
        order = await cancel_pending_order(session, order_id)
        await session.commit()
    except CheckoutError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except (SQLAlchemyError, TypeError, ValueError):
        await session.rollback()
        logger.exception("Admin could not cancel order %s", order_id)
        await callback.answer("Не удалось отменить заказ", show_alert=True)
        return

    # Уведомляем пользователя
    stmt_user = select(User).where(User.id == order.user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if user:
        try:
            await callback.bot.send_message(
                user.telegram_id,
                f"❌ <b>Заказ отменен</b>\n\n"
                f"Заказ #{order.id} был отменен администратором.\n"
                f"Если заказ был оплачен, средства будут возвращены.",
                parse_mode="HTML"
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Could not notify user %s about cancelled order: %s",
                order.id,
                exc,
                exc_info=True,
            )

    await callback.answer("✅ Заказ отменен", show_alert=True)
    await callback.message.edit_text(
        f"✅ Заказ #{order_id} отменен",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders_all")]
        ]),
    )


# Управление каталогом
CATALOG_LEVELS = {
    "category": {"depth": 0, "title": "Категория", "title_plural": "Категории", "icon": "📂"},
    "subcategory": {"depth": 1, "title": "Подкатегория", "title_plural": "Подкатегории", "icon": "🗂"},
    "group": {
        "depth": 2, "title": "Группа", "title_plural": "Группы", "icon": "📁",
        "new_title": "Новая группа", "added": "Группа добавлена",
    },
    "type": {
        "depth": 3, "title": "Тип", "title_plural": "Типы", "icon": "🏷",
        "new_title": "Новый тип", "added": "Тип добавлен",
    },
}

