"""Административные обработчики: accounts_roles."""
from .admin_context import *
from aiogram.exceptions import TelegramAPIError


@router.callback_query(F.data == "admin_manage_accounts")
async def admin_manage_accounts_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Меню управления аккаунтами - выбор товара"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Получаем все товары
    stmt = select(Product).where(Product.is_active == True).order_by(Product.name)
    result = await session.execute(stmt)
    products = result.scalars().all()

    if not products:
        await callback.message.edit_text(
            "❌ Нет активных товаров. Сначала создайте товар.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")]
            ])
        )
        await callback.answer()
        return

    buttons = []
    for product in products:
        # Получаем количество аккаунтов на складе
        stmt_count = select(func.count(Account.id)).where(
            Account.product_id == product.id,
            Account.is_sold == False
        )
        result_count = await session.execute(stmt_count)
        stock_count = result_count.scalar() or 0

        buttons.append([InlineKeyboardButton(
            text=f"📦 {product.name} (остаток: {stock_count})",
            callback_data=f"admin_accounts_product_{product.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")])

    await callback.message.edit_text(
        "📦 <b>Управление аккаунтами</b>\n\n"
        "Выберите товар для управления аккаунтами:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_accounts_product_"))
async def admin_accounts_product_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Меню действий с аккаунтами для выбранного товара"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    navigation = (await state.get_data()).get("product_editor_back_callback")
    await state.clear()
    if navigation:
        await state.update_data(product_editor_back_callback=navigation)

    product_id = int(callback.data.split("_")[3])

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    # Получаем статистику аккаунтов
    stmt_total = select(func.count(Account.id)).where(Account.product_id == product_id)
    result_total = await session.execute(stmt_total)
    total_accounts = result_total.scalar() or 0

    stmt_available = select(func.count(Account.id)).where(
        Account.product_id == product_id,
        Account.is_sold == False
    )
    result_available = await session.execute(stmt_available)
    available_accounts = result_available.scalar() or 0

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"admin_account_add_{product_id}"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"admin_account_delete_{product_id}"),
        ],
        [InlineKeyboardButton(text="📥 Импорт из файла", callback_data=f"admin_account_import_{product_id}")],
        [InlineKeyboardButton(
            text="◀️ Назад", callback_data=f"admin_edit_product_select_{product_id}"
        )]
    ])

    await callback.message.edit_text(
        f"📦 <b>Содержимое товара</b>\n\n"
        f"Товар: <b>{escape(product.name)}</b>\n"
        f"Всего аккаунтов: {total_accounts}\n"
        f"Доступно на складе: {available_accounts}\n\n"
        f"Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_account_add_"))
async def admin_account_add_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать добавление аккаунта"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[3])

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await state.update_data(account_product_id=product_id)
    await state.set_state(AdminStates.waiting_add_account)

    keyboard = _input_cancel_keyboard(f"admin_accounts_product_{product_id}")

    await callback.message.edit_text(
        f"Товар: <b>{escape(product.name)}</b>\n\n"
        f"Введите содержимое товара (например <code>user:pass@host:port</code>):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminStates.waiting_add_account)
async def admin_account_add_process(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка добавления аккаунта"""

    data = await state.get_data()
    product_id = data.get("account_product_id")

    if not product_id:
        await edit_input_screen(
            message, state, "Ошибка: товар не выбран. Начните заново.",
            reply_markup=get_admin_products_keyboard(), state_data=data,
        )
        await state.clear()
        return

    account_data = (message.text or "").strip()

    if not account_data:
        await edit_input_screen(
            message,
            state,
            "Введите содержимое товара:",
            reply_markup=_input_cancel_keyboard(f"admin_accounts_product_{product_id}"),
            state_data=data,
        )
        return

    # Проверяем на дубликаты
    stmt = select(Account).where(
        Account.product_id == product_id,
        Account.account_data == account_data
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        await edit_input_screen(
            message,
            state,
            "❌ Такое содержимое уже существует. Введите другое:",
            reply_markup=_input_cancel_keyboard(f"admin_accounts_product_{product_id}"),
            state_data=data,
        )
        return

    # Создаем аккаунт
    account = Account(
        product_id=product_id,
        account_data=account_data,
        is_sold=False
    )
    session.add(account)

    # Получаем текущее количество на складе перед обновлением
    # Проверяем реальное количество аккаунтов из таблицы Account
    stmt_count_before = select(func.count(Account.id)).where(
        Account.product_id == product_id,
        Account.is_sold == False
    )
    result_count_before = await session.execute(stmt_count_before)
    actual_stock_before = result_count_before.scalar() or 0
    stock_was_zero = actual_stock_before == 0

    # Обновляем количество на складе
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(stock_count=Product.stock_count + 1)
    )

    await session.commit()

    stmt_product = select(Product).where(Product.id == product_id)
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    # Уведомляем пользователей о поступлении товара, если stock_count был 0 и стал >0
    if stock_was_zero:
        from utils.notifications import notify_stock_available
        await notify_stock_available(session, product_id, message.bot, check_stock_was_zero=False)

    await edit_input_screen(
        message,
        state,
        f"✅ Содержимое успешно добавлено к товару "
        f"<b>{escape(product.name) if product else 'N/A'}</b>!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_accounts_product_{product_id}")]
        ]),
        state_data=data,
    )
    navigation = data.get("product_editor_back_callback")
    await state.clear()
    if navigation:
        await state.update_data(product_editor_back_callback=navigation)


@router.callback_query(F.data.startswith("admin_account_import_"))
async def admin_account_import_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать импорт аккаунтов из файла"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[3])

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await state.update_data(account_import_product_id=product_id)
    await state.set_state(AdminStates.waiting_import_accounts_file)

    keyboard = _input_cancel_keyboard(f"admin_accounts_product_{product_id}")

    await callback.message.edit_text(
        f"📥 <b>Импорт аккаунтов</b>\n\n"
        f"Товар: <b>{escape(product.name)}</b>\n\n"
        f"Отправьте текстовый файл с аккаунтами.\n\n"
        f"<b>Формат:</b> каждая строка = один аккаунт\n"
        f"Пример:\n"
        f"<code>login1:password1</code>\n"
        f"<code>login2:password2</code>\n"
        f"<code>login3:password3</code>\n\n"
        f"Поддерживаются форматы TXT и CSV.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminStates.waiting_import_accounts_file)
async def admin_account_import_process(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка импорта аккаунтов из файла"""

    if not message.document:
        data = await state.get_data()
        product_id = data.get("account_import_product_id")
        await edit_input_screen(
            message,
            state,
            "Пожалуйста, отправьте текстовый файл с аккаунтами.",
            reply_markup=_input_cancel_keyboard(
                f"admin_accounts_product_{product_id}" if product_id else "admin_catalog_products"
            ),
            state_data=data,
        )
        return

    data = await state.get_data()
    product_id = data.get("account_import_product_id")

    if not product_id:
        await edit_input_screen(
            message, state, "Ошибка: товар не выбран. Начните заново.",
            reply_markup=get_admin_products_keyboard(), state_data=data,
        )
        await state.clear()
        return

    try:
        # Получаем файл
        file = await message.bot.get_file(message.document.file_id)
        file_content = await message.bot.download_file(file.file_path)

        if isinstance(file_content, (bytes, bytearray)):
            content_bytes = file_content
        elif hasattr(file_content, "read"):
            content_bytes = file_content.read()
        else:
            content_bytes = bytes(file_content)

        text_content = content_bytes.decode('utf-8', errors='ignore')

        # Получаем текущее количество на складе перед импортом
        # Проверяем реальное количество аккаунтов из таблицы Account
        stmt_count_before = select(func.count(Account.id)).where(
            Account.product_id == product_id,
            Account.is_sold == False
        )
        result_count_before = await session.execute(stmt_count_before)
        actual_stock_before = result_count_before.scalar() or 0
        stock_was_zero = actual_stock_before == 0

        # Используем существующую функцию импорта
        loaded, duplicates = await upload_accounts_from_file(session, product_id, text_content)

        # Коммитим изменения в базе данных
        await session.commit()

        stmt_product = select(Product).where(Product.id == product_id)
        result_product = await session.execute(stmt_product)
        product = result_product.scalar_one_or_none()

        # Уведомляем пользователей о поступлении товара, если stock_count был 0 и стал >0
        if loaded > 0 and stock_was_zero:
            from utils.notifications import notify_stock_available
            await notify_stock_available(session, product_id, message.bot, check_stock_was_zero=False)

        await edit_input_screen(
            message,
            state,
            f"✅ <b>Импорт завершен!</b>\n\n"
            f"Товар: <b>{escape(product.name) if product else 'N/A'}</b>\n"
            f"Загружено позиций: {loaded}\n"
            f"Пропущено дублей: {duplicates}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_accounts_product_{product_id}")]
            ]),
            state_data=data,
        )
        navigation = data.get("product_editor_back_callback")
        await state.clear()
        if navigation:
            await state.update_data(product_editor_back_callback=navigation)

    except Exception as e:
        logger.error(f"Error importing accounts: {e}", exc_info=True)
        await edit_input_screen(
            message,
            state,
            f"❌ Ошибка при импорте: {escape(str(e)[:500])}",
            reply_markup=_input_cancel_keyboard(f"admin_accounts_product_{product_id}"),
            state_data=data,
        )
        await state.clear()


@router.callback_query(F.data.startswith("admin_account_delete_"))
async def admin_account_delete_start(callback: CallbackQuery, session: AsyncSession):
    """Начать удаление аккаунта - показываем список"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[3])

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    # Получаем все доступные аккаунты (не проданные)
    stmt_accounts = select(Account).where(
        Account.product_id == product_id,
        Account.is_sold == False
    ).order_by(Account.id.desc()).limit(50)
    result_accounts = await session.execute(stmt_accounts)
    accounts = result_accounts.scalars().all()

    if not accounts:
        await callback.message.edit_text(
            f"❌ Нет доступных аккаунтов для удаления\n\n"
            f"Товар: <b>{escape(product.name)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_accounts_product_{product_id}")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Формируем список аккаунтов с кнопками
    buttons = []
    text = f"🗑️ <b>Удаление аккаунта</b>\n\n"
    text += f"Товар: <b>{escape(product.name)}</b>\n"
    text += f"Доступно для удаления: {len(accounts)}\n\n"
    text += f"Выберите аккаунт для удаления:\n\n"

    for account in accounts:
        # Показываем первые 20 символов данных аккаунта
        account_preview = account.account_data[:20] + "..." if len(account.account_data) > 20 else account.account_data
        text += f"ID: {account.id} - {escape(account_preview)}\n"
        buttons.append([InlineKeyboardButton(
            text=f"🗑️ ID: {account.id}",
            callback_data=f"delete_account_{account.id}"
        )])

    if len(accounts) == 50:
        text += f"\n... показано 50 из доступных аккаунтов"

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_accounts_product_{product_id}")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_account_"))
async def admin_delete_account_confirm(callback: CallbackQuery, session: AsyncSession):
    """Подтверждение удаления аккаунта"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    account_id = int(callback.data.split("_")[2])

    stmt = select(Account).where(Account.id == account_id)
    result = await session.execute(stmt)
    account = result.scalar_one_or_none()

    if not account:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    if account.is_sold:
        await callback.answer("Нельзя удалить проданный аккаунт", show_alert=True)
        return

    # Получаем информацию о товаре
    stmt_product = select(Product).where(Product.id == account.product_id)
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    # Показываем превью данных аккаунта (первые 50 символов)
    account_preview = account.account_data[:50] + "..." if len(account.account_data) > 50 else account.account_data

    keyboard = get_confirm_keyboard("delete_account", account_id)
    text = f"⚠️ <b>Подтвердите удаление</b>\n\n"
    text += f"Товар: <b>{escape(product.name) if product else 'N/A'}</b>\n"
    text += f"ID аккаунта: {account.id}\n"
    text += f"Данные: <code>{escape(account_preview)}</code>\n\n"
    text += f"Вы уверены?"

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_account_"))
async def admin_delete_account_execute(callback: CallbackQuery, session: AsyncSession):
    """Выполнить удаление аккаунта"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    account_id = int(callback.data.split("_")[3])

    stmt = select(Account).where(Account.id == account_id)
    result = await session.execute(stmt)
    account = result.scalar_one_or_none()

    if not account:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    if account.is_sold:
        await callback.answer("Нельзя удалить проданный аккаунт", show_alert=True)
        return

    product_id = account.product_id

    # Получаем информацию о товаре
    stmt_product = select(Product).where(Product.id == product_id)
    result_product = await session.execute(stmt_product)
    product = result_product.scalar_one_or_none()

    # Удаляем аккаунт
    await session.delete(account)

    # Обновляем количество на складе
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(stock_count=Product.stock_count - 1)
    )

    await session.commit()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_accounts_product_{product_id}")]
    ])

    await callback.message.edit_text(
        f"✅ Аккаунт удален\n\n"
        f"Товар: <b>{escape(product.name) if product else 'N/A'}</b>\n"
        f"ID аккаунта: {account_id}",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_delete_account_"))
async def admin_delete_account_cancel(callback: CallbackQuery, session: AsyncSession):
    """Отмена удаления аккаунта"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    account_id = int(callback.data.split("_")[3])

    stmt = select(Account).where(Account.id == account_id)
    result = await session.execute(stmt)
    account = result.scalar_one_or_none()

    if not account:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    product_id = account.product_id

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_accounts_product_{product_id}")]
    ])

    await callback.message.edit_text(
        "❌ Удаление отменено",
        reply_markup=keyboard
    )
    await callback.answer()


# Управление ролями пользователей
@router.callback_query(F.data.startswith("admin_user_role_"))
async def admin_user_role_menu(callback: CallbackQuery, session: AsyncSession):
    """Меню управления ролью пользователя"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Только разработчики могут управлять ролями
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("Только разработчики могут управлять ролями", show_alert=True)
        return

    user_id = int(callback.data.split("_")[3])

    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Разработчик из DEVELOPER_IDS является владельцем технических прав.
    if user.telegram_id in settings.developer_ids_list:
        await callback.answer("Нельзя изменить роль разработчика из .env", show_alert=True)
        return

    # Определяем текущую роль
    current_role = user.role or "user"

    # Создаем кнопки для выбора роли
    keyboard_buttons = []

    if current_role != "user":
        keyboard_buttons.append([InlineKeyboardButton(text="👤 Установить роль: Пользователь", callback_data=f"admin_set_role_{user.id}_user")])
    if current_role != "admin":
        keyboard_buttons.append([InlineKeyboardButton(text="👑 Установить роль: Администратор", callback_data=f"admin_set_role_{user.id}_admin")])
    if current_role != "developer":
        keyboard_buttons.append([InlineKeyboardButton(text="⚙️ Установить роль: Разработчик", callback_data=f"admin_set_role_{user.id}_developer")])

    keyboard_buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"user_action_{user.telegram_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    role_text = "👤 Пользователь"
    if current_role == "admin":
        role_text = "👑 Администратор"
    elif current_role == "developer":
        role_text = "⚙️ Разработчик"

    await callback.message.edit_text(
        f"👑 <b>Управление ролью пользователя</b>\n\n"
        f"Пользователь: {escape(user.first_name or 'N/A')} "
        f"(@{escape(user.username or 'N/A')})\n"
        f"Текущая роль: {role_text}\n\n"
        f"Выберите новую роль:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_set_role_"))
async def admin_set_role(callback: CallbackQuery, session: AsyncSession):
    """Установка роли пользователя"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Только разработчики могут управлять ролями
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("Только разработчики могут управлять ролями", show_alert=True)
        return

    parts = callback.data.split("_")
    try:
        user_id = int(parts[3])
        new_role = parts[4]
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные роли", show_alert=True)
        return
    if new_role not in {"user", "admin", "developer"}:
        await callback.answer("Некорректная роль", show_alert=True)
        return

    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    if user.telegram_id in settings.developer_ids_list:
        await callback.answer("Нельзя изменить роль разработчика из .env", show_alert=True)
        return

    # Любой назначенный через бот администратор должен пережить перезапуск:
    # синхронизируем его роль не только с БД, но и с ADMIN_IDS в .env.
    old_role = user.role or "user"
    old_admin_ids = set(settings.admin_ids_list)
    new_admin_ids = set(old_admin_ids)
    if new_role == "admin":
        new_admin_ids.add(user.telegram_id)
    else:
        new_admin_ids.discard(user.telegram_id)

    try:
        new_admin_ids_value = save_admin_ids_to_env(new_admin_ids)
        settings.ADMIN_IDS = new_admin_ids_value
        user.role = new_role
        if new_role == "developer":
            user.is_blocked = False
        await session.commit()
    except (OSError, SQLAlchemyError, ValueError):
        await session.rollback()
        logger.exception("Could not synchronize role and ADMIN_IDS")
        try:
            restored_admin_ids_value = save_admin_ids_to_env(old_admin_ids)
            settings.ADMIN_IDS = restored_admin_ids_value
        except (OSError, ValueError):
            logger.exception("Could not restore ADMIN_IDS after role update failure")
        await callback.answer("Не удалось сохранить роль в .env", show_alert=True)
        return

    # Отправляем уведомление пользователю об изменении роли
    try:
        role_names = {
            "user": "Пользователь",
            "admin": "Администратор",
            "developer": "Разработчик"
        }

        from utils.keyboards import get_main_menu_keyboard
        is_admin = new_role in ("admin", "developer")
        interaction = await get_interaction_config(session)

        await callback.bot.send_message(
            user.telegram_id,
            f"🔄 <b>Ваша роль изменена!</b>\n\n"
            f"Новая роль: <b>{role_names.get(new_role, new_role)}</b>\n\n"
            f"Ваша клавиатура была обновлена.",
            reply_markup=get_main_menu_keyboard(
                is_admin=is_admin,
                balance=user.balance,
                community_url=interaction.community_source if interaction.community_enabled else None,
                support_url=interaction.support_source if interaction.support_enabled else None,
            ),
            parse_mode="HTML"
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Failed to notify user %s about role change: %s",
            user.telegram_id,
            exc,
            exc_info=True,
        )

    role_names_display = {
        "user": "Пользователь",
        "admin": "Администратор",
        "developer": "Разработчик"
    }

    callback_data_back = f"user_action_{user.telegram_id}"
    logger.debug(f"Setting callback_data for back button: {callback_data_back}")

    await callback.message.edit_text(
        f"✅ <b>Роль изменена</b>\n\n"
        f"Пользователь: {escape(user.first_name or 'N/A')} "
        f"(@{escape(user.username or 'N/A')})\n"
        f"Старая роль: {role_names_display.get(old_role, old_role)}\n"
        f"Новая роль: {role_names_display.get(new_role, new_role)}\n\n"
        f"Пользователю отправлено уведомление с обновленной клавиатурой.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к пользователю", callback_data=callback_data_back)]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()
