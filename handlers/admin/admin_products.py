"""Административные обработчики: products."""
from .admin_context import *
from .admin_core import CATALOG_LEVELS
from .admin_catalog import _catalog_tree, _products_screen

@router.callback_query(F.data == "admin_orders_date")
async def admin_orders_date_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Фильтр заказов по дате"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_order_date_from)
    keyboard = _input_cancel_keyboard("admin_orders")
    await callback.message.edit_text(
        "📅 <b>Фильтр заказов по дате</b>\n\n"
        "Введите дату начала (формат: ДД.ММ.ГГГГ, например: 01.01.2024):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminStates.waiting_order_date_from)
async def admin_orders_date_from(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка даты начала"""
    try:
        date_from = datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(date_from=date_from)
        await state.set_state(AdminStates.waiting_order_date_to)
        await message.answer(
            "Введите дату окончания (формат: ДД.ММ.ГГГГ):",
            reply_markup=_input_cancel_keyboard("admin_orders"),
        )
    except ValueError:
        await message.answer(
            "Неверный формат даты. Используйте ДД.ММ.ГГГГ (например: 01.01.2024):",
            reply_markup=_input_cancel_keyboard("admin_orders"),
        )


@router.message(AdminStates.waiting_order_date_to)
async def admin_orders_date_to(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка даты окончания и показ результатов"""
    try:
        date_to = datetime.strptime(message.text, "%d.%m.%Y")
        data = await state.get_data()
        date_from = data.get("date_from")
        if not date_from or date_to < date_from:
            await message.answer("Дата окончания не может быть раньше даты начала.")
            return
        date_to_exclusive = date_to + timedelta(days=1)

        stmt = select(Order).where(
            Order.created_at >= date_from,
            Order.created_at < date_to_exclusive
        ).order_by(Order.created_at.desc()).limit(50)
        result = await session.execute(stmt)
        orders = result.scalars().all()

        if not orders:
            await message.answer("Заказов за указанный период не найдено")
            await state.clear()
            return

        text = f"📅 <b>Заказы с {date_from.strftime('%d.%m.%Y')} по {date_to.strftime('%d.%m.%Y')}</b>\n\n"
        for order in orders:
            text += f"#{order.id} - {order.status} - {order.total_amount:.2f} ₽ - {order.created_at.strftime('%d.%m.%Y')}\n"

        await message.answer(text, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(
            "Неверный формат даты. Используйте ДД.ММ.ГГГГ:",
            reply_markup=_input_cancel_keyboard("admin_orders"),
        )


@router.callback_query(F.data == "admin_orders_status")
async def admin_orders_status_filter(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Фильтр заказов по статусу"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Ожидает оплаты", callback_data="filter_status_ОЖИДАЕТ ОПЛАТЫ")],
        [InlineKeyboardButton(text="✅ Оплачено", callback_data="filter_status_ОПЛАЧЕНО")],
        [InlineKeyboardButton(text="✔️ Выполнено", callback_data="filter_status_ВЫПОЛНЕНО")],
        [InlineKeyboardButton(text="❌ Отменено", callback_data="filter_status_ОТМЕНЕНО")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders")]
    ])

    await callback.message.edit_text("📊 Выберите статус:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("filter_status_"))
async def admin_orders_status_result(callback: CallbackQuery, session: AsyncSession):
    """Результаты фильтра по статусу"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    status = callback.data.replace("filter_status_", "")

    stmt = select(Order).where(Order.status == status).order_by(Order.created_at.desc()).limit(50)
    result = await session.execute(stmt)
    orders = result.scalars().all()

    if not orders:
        await callback.message.edit_text(
            f"Заказов со статусом «{status}» не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders_status")]
            ]),
        )
        await callback.answer()
        return

    text = f"📊 <b>Заказы со статусом: {status}</b>\n\n"
    for order in orders:
        text += f"#{order.id} - {order.total_amount:.2f} ₽ - {order.created_at.strftime('%d.%m.%Y %H:%M')}\n"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_orders_status")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_orders_user")
async def admin_orders_user_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Фильтр заказов по пользователю"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_order_user_filter)
    keyboard = _input_cancel_keyboard("admin_orders")
    await callback.message.edit_text("👤 Введите Telegram ID пользователя:", reply_markup=keyboard)
    await callback.answer()


@router.message(AdminStates.waiting_order_user_filter)
async def admin_orders_user_result(message: Message, state: FSMContext, session: AsyncSession):
    """Результаты фильтра по пользователю"""
    try:
        telegram_id = int(message.text)

        stmt_user = select(User).where(User.telegram_id == telegram_id)
        result_user = await session.execute(stmt_user)
        user = result_user.scalar_one_or_none()

        if not user:
            await message.answer("Пользователь не найден")
            await state.clear()
            return

        stmt = select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(50)
        result = await session.execute(stmt)
        orders = result.scalars().all()

        if not orders:
            await message.answer(
                f"Заказов у пользователя @{escape(user.username or 'N/A')} не найдено"
            )
            await state.clear()
            return

        text = (
            "👤 <b>Заказы пользователя "
            f"@{escape(user.username or user.first_name or 'N/A')}</b>\n\n"
        )
        for order in orders:
            text += f"#{order.id} - {order.status} - {order.total_amount:.2f} ₽ - {order.created_at.strftime('%d.%m.%Y')}\n"

        await message.answer(text, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(
            "Введите корректный Telegram ID (число):",
            reply_markup=_input_cancel_keyboard("admin_orders"),
        )

# ========== РЕДАКТИРОВАНИЕ ТОВАРОВ ==========

EDIT_PRODUCTS_PAGE_SIZE = 10


async def render_edit_products_list(
    target_message,
    state: FSMContext,
    session: AsyncSession,
    page: int = 1
):
    """Отрисовка списка товаров с пагинацией, поиском и сортировкой"""
    data = await state.get_data()
    query_text = data.get("edit_products_query")
    category_id = data.get("edit_products_category_id")
    sort_mode = data.get("edit_products_sort", "recent")

    stmt = select(Product).where(Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES))
    count_stmt = select(func.count(Product.id)).where(Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES))

    if sort_mode == "category":
        stmt = stmt.join(Category)
        count_stmt = count_stmt.join(Category)

    if query_text:
        stmt = stmt.where(Product.name.ilike(f"%{query_text}%"))
        count_stmt = count_stmt.where(Product.name.ilike(f"%{query_text}%"))

    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
        count_stmt = count_stmt.where(Product.category_id == category_id)

    if sort_mode == "category":
        stmt = stmt.order_by(Category.name.asc(), Product.name.asc())
    else:
        stmt = stmt.order_by(Product.id.desc())

    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one() or 0

    total_pages = max(1, (total + EDIT_PRODUCTS_PAGE_SIZE - 1) // EDIT_PRODUCTS_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * EDIT_PRODUCTS_PAGE_SIZE

    result = await session.execute(stmt.limit(EDIT_PRODUCTS_PAGE_SIZE).offset(offset))
    products = result.scalars().all()

    if not products:
        await target_message.edit_text(
            "❌ Нет товаров для редактирования по заданным фильтрам.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Сбросить фильтры", callback_data="admin_edit_products_reset")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")]
            ])
        )
        return

    # Подгружаем категории для отображения
    category_map = {}
    if sort_mode == "category" or category_id:
        stmt_cat = select(Category)
        result_cat = await session.execute(stmt_cat)
        categories = result_cat.scalars().all()
        category_map = {c.id: c.name for c in categories}

    buttons = []
    for product in products:
        category_label = category_map.get(product.category_id, "")
        if category_label:
            text = f"#{product.id} · {product.name} · {category_label}"
        else:
            text = f"#{product.id} · {product.name}"
        buttons.append([InlineKeyboardButton(
            text=text,
            callback_data=f"admin_edit_product_select_{product.id}"
        )])

    # Навигация
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_edit_products_page_{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="admin_edit_products_page_info"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_edit_products_page_{page + 1}"))
    buttons.append(nav_buttons)

    # Фильтры и поиск
    sort_label = "категория" if sort_mode == "category" else "последние"
    filter_row = [
        InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_edit_products_search"),
        InlineKeyboardButton(text="📂 Категория", callback_data="admin_edit_products_filter_category"),
        InlineKeyboardButton(text=f"↕️ Сортировка: {sort_label}", callback_data="admin_edit_products_toggle_sort")
    ]
    buttons.append(filter_row)
    buttons.append([InlineKeyboardButton(text="🔎 Сбросить фильтры", callback_data="admin_edit_products_reset")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")])

    await state.update_data(edit_products_page=page)

    await target_message.edit_text(
        "✏️ <b>Выберите товар для редактирования:</b>\n"
        f"Всего товаров: {total}\n"
        f"Фильтр: {query_text or '—'} | Категория: {category_id or '—'} | Сорт: {sort_label}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "admin_edit_product")
async def admin_edit_product_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Совместимость со старыми кнопками: открыть обычный список товаров."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _products_screen(session)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_edit_products_return")
async def admin_edit_products_return(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    """Старый возврат теперь ведёт в единый список товаров."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _products_screen(session)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_edit_products_page_"))
async def admin_edit_products_page(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Пагинация списка товаров"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Проверяем, что это не кнопка "info" (показывает текущую страницу)
    last_part = callback.data.split("_")[-1]
    if last_part == "info":
        # Это кнопка с информацией о странице, просто отвечаем без действий
        await callback.answer()
        return

    try:
        page = int(last_part)
    except ValueError:
        await callback.answer("Ошибка: некорректный номер страницы", show_alert=True)
        return

    await render_edit_products_list(callback.message, state, session, page=page)
    await callback.answer()


@router.callback_query(F.data == "admin_edit_products_search")
async def admin_edit_products_search(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Запрос поискового текста"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_edit_product_search)
    await callback.message.edit_text(
        "🔍 Введите часть названия товара для поиска:",
        reply_markup=_input_cancel_keyboard("admin_edit_product"),
    )
    await callback.answer()


@router.message(AdminStates.waiting_edit_product_search)
async def admin_edit_products_search_apply(message: Message, state: FSMContext, session: AsyncSession):
    """Применение поиска по названию"""

    query_text = (message.text or "").strip()
    if not query_text:
        await message.answer(
            "Введите текст для поиска.",
            reply_markup=_input_cancel_keyboard("admin_edit_product"),
        )
        return

    await state.update_data(edit_products_query=query_text, edit_products_page=1)
    await state.set_state(AdminStates.waiting_edit_product_id)
    await render_edit_products_list(message, state, session, page=1)


@router.callback_query(F.data == "admin_edit_products_filter_category")
async def admin_edit_products_filter_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Выбор категории для фильтра"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    stmt = select(Category).where(Category.is_active == True)
    result = await session.execute(stmt)
    categories = result.scalars().all()

    if not categories:
        await callback.answer("Нет активных категорий", show_alert=True)
        return

    buttons = []
    for cat in categories:
        buttons.append([InlineKeyboardButton(
            text=f"📂 {cat.name}",
            callback_data=f"admin_edit_products_set_category_{cat.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_edit_product")])

    await callback.message.edit_text(
        "📂 Выберите категорию для фильтра:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_edit_products_set_category_"))
async def admin_edit_products_set_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Установка фильтра по категории"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    category_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_products_category_id=category_id, edit_products_page=1)
    await state.set_state(AdminStates.waiting_edit_product_id)
    await render_edit_products_list(callback.message, state, session, page=1)
    await callback.answer()


@router.callback_query(F.data == "admin_edit_products_toggle_sort")
async def admin_edit_products_toggle_sort(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Переключение сортировки"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    data = await state.get_data()
    current = data.get("edit_products_sort", "recent")
    new_sort = "category" if current == "recent" else "recent"
    await state.update_data(edit_products_sort=new_sort, edit_products_page=1)
    await render_edit_products_list(callback.message, state, session, page=1)
    await callback.answer()


@router.callback_query(F.data == "admin_edit_products_reset")
async def admin_edit_products_reset(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Сброс фильтров"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.update_data(
        edit_products_page=1,
        edit_products_query=None,
        edit_products_category_id=None,
        edit_products_sort="recent"
    )
    await state.set_state(AdminStates.waiting_edit_product_id)
    await render_edit_products_list(callback.message, state, session, page=1)
    await callback.answer()

@router.callback_query(F.data.startswith("admin_edit_product_select_"))
async def admin_edit_product_select_callback(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Выбор товара для редактирования через кнопки"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[-1])
    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await state.update_data(product_id=product_id)
    data = await state.get_data()
    back_callback = data.get("product_editor_back_callback", "admin_catalog_products")

    text, keyboard = _product_editor_screen(product, back_callback)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminStates.waiting_edit_product_field)
    await callback.answer()


def _product_editor_screen(
    product: Product, back_callback: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка товара и переключатель активности без промежуточного экрана."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Название", callback_data="edit_field_name"),
            InlineKeyboardButton(text="💰 Фикс. цена", callback_data="edit_field_price"),
        ],
        [
            InlineKeyboardButton(
                text="📈 Себестоимость + %",
                callback_data="edit_field_cost_plus",
            ),
        ],
        [
            InlineKeyboardButton(text="📄 Описание", callback_data="edit_field_description"),
            InlineKeyboardButton(text="📂 Категория", callback_data="edit_field_category"),
        ],
        [
            InlineKeyboardButton(
                text="🔴 Выключить" if product.is_active else "🟢 Включить",
                callback_data="edit_field_active",
            ),
            InlineKeyboardButton(text="ℹ️ Формат", callback_data="edit_field_format"),
        ],
        [InlineKeyboardButton(text="💡 Рекомендации", callback_data="edit_field_recommendations")],
        *([] if is_virtual_product(product) else [[InlineKeyboardButton(
            text="📦 Содержимое", callback_data=f"admin_accounts_product_{product.id}"
        )]]),
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)]
    ])
    pricing_mode = getattr(product, "pricing_mode", PRICING_MODE_FIXED)
    if pricing_mode == PRICING_MODE_COST_PLUS:
        cost_text = (
            f"Себестоимость: {product.cost_price:.2f} ₽"
            if getattr(product, "cost_price", None) is not None
            else "Себестоимость: ещё не рассчитана"
        )
        pricing_text = (
            "Авто-себестоимость Fragment\n"
            f"{cost_text}\n"
            f"API-сбор: {settings.FRAGMENT_API_FEE_PERCENT:.4f}%\n"
            "Сеть: включена в сумму Fragment\n"
            f"Наценка: {product.markup_percent:.4f}%\n"
            f"Итоговая цена: {product.price:.2f} ₽"
        )
    else:
        pricing_text = f"Фиксированная цена: {product.price:.2f} ₽"
    text = (
        f"✏️ <b>Редактирование товара</b>\n\n"
        f"ID: {product.id}\n"
        f"Название: {escape(product.name)}\n"
        f"{pricing_text}\n"
        f"Остаток: {product.stock_count} шт.\n"
        f"Активен: {'Да' if product.is_active else 'Нет'}\n\n"
        f"Выберите поле для редактирования:"
    )
    return text, keyboard


@router.callback_query(F.data.regexp(r"^admin_catalog_product_edit_\d+$"))
async def admin_catalog_product_edit(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    """Открыть товар из раздела «Товары» и вернуться туда же."""
    await state.update_data(product_editor_back_callback="admin_catalog_products")
    await admin_edit_product_select_callback(callback, state, session)


@router.message(AdminStates.waiting_edit_product_id)
async def admin_edit_product_select(message: Message, state: FSMContext, session: AsyncSession):
    """Выбор поля для редактирования"""
    try:
        product_id = int(message.text)

        stmt = select(Product).where(Product.id == product_id)
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()

        if not product:
            await message.answer(
                "Товар не найден. Введите корректный ID:",
                reply_markup=_input_cancel_keyboard("admin_catalog_products"),
            )
            return

        await state.update_data(product_id=product_id)
        data = await state.get_data()
        back_callback = data.get("product_editor_back_callback", "admin_catalog_products")

        text, keyboard = _product_editor_screen(product, back_callback)

        await message.answer(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_edit_product_field)

    except ValueError:
        await message.answer(
            "Введите корректный ID товара (число):",
            reply_markup=_input_cancel_keyboard("admin_catalog_products"),
        )


@router.callback_query(F.data.startswith("edit_field_"))
async def admin_edit_product_field(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Обработка выбора поля"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    field = callback.data.replace("edit_field_", "")
    allowed_fields = {
        "name", "price", "cost_plus", "description", "category", "active",
        "format", "recommendations",
    }
    if field not in allowed_fields:
        await callback.answer("Некорректное поле товара", show_alert=True)
        return
    data = await state.get_data()
    product_id = data.get("product_id")

    if not product_id:
        await callback.answer("Ошибка: товар не выбран", show_alert=True)
        return

    # Для активности - сразу переключаем
    if field == "active":
        stmt = select(Product).where(Product.id == product_id)
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()

        if product:
            product.is_active = not product.is_active
            await session.commit()
            back_callback = data.get(
                "product_editor_back_callback", "admin_catalog_products"
            )
            text, keyboard = _product_editor_screen(product, back_callback)
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="HTML"
            )
            await callback.answer(
                "Товар включён" if product.is_active else "Товар выключен"
            )
        return

    # Для категории - показываем список
    if field == "category":
        stmt = select(Category).where(
            Category.parent_id.is_(None), Category.is_active.is_(True)
        ).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
        result = await session.execute(stmt)
        categories = result.scalars().all()

        buttons = []
        for cat in categories:
            buttons.append([InlineKeyboardButton(
                text=f"📂 {cat.name}",
                callback_data=f"edit_product_category_root_{cat.id}"
            )])
        buttons.append([InlineKeyboardButton(
            text="◀️ Назад", callback_data=f"admin_edit_product_select_{product_id}"
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            "📂 <b>Выберите категорию:</b>", reply_markup=keyboard, parse_mode="HTML"
        )
        await callback.answer()
        return

    # Для остальных полей - запрашиваем новое значение
    field_names = {
        "name": "название",
        "price": "цену (число)",
        "cost_plus": "наценку на себестоимость (%)",
        "description": "описание",
        "format": "формат",
        "recommendations": "рекомендации"
    }

    await state.update_data(edit_field=field)
    await state.set_state(AdminStates.waiting_edit_product_value)
    prompt = f"Введите новое значение для поля '{field_names.get(field, field)}':"
    if field == "cost_plus":
        prompt = (
            "Введите только наценку в процентах.\n"
            "Бот сам получит актуальную цену Fragment, учтёт стоимость товара и API-сбор, "
            "затем применит эту наценку.\n"
            "Пример: 25"
        )
    await callback.message.edit_text(
        prompt,
        reply_markup=_input_cancel_keyboard("admin_edit_product"),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^edit_product_category_root_\d+$"))
async def admin_edit_product_category_root(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    category_id = int(callback.data.rsplit("_", 1)[1])
    data = await state.get_data()
    product_id = data.get("product_id")
    product = await session.get(Product, product_id) if isinstance(product_id, int) else None
    category = await session.get(Category, category_id)
    if product is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if category is None or not category.is_active:
        await callback.answer("Категория недоступна", show_alert=True)
        return

    children = (await session.execute(
        select(Category).where(
            Category.parent_id == category.id,
            Category.is_active.is_(True),
        ).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    )).scalars().all()
    if children:
        _, _, depths = await _catalog_tree(session)
        child_depth = depths.get(category.id, 0) + 1
        child_meta = next(
            (meta for meta in CATALOG_LEVELS.values() if meta["depth"] == child_depth),
            {"title": "Раздел", "icon": "📂"},
        )
        buttons = [[InlineKeyboardButton(
            text=f"{child_meta['icon']} {child.name}",
            callback_data=f"edit_product_category_root_{child.id}",
        )] for child in children]
        back_callback = (
            f"edit_product_category_root_{category.parent_id}"
            if category.parent_id is not None
            else "edit_field_category"
        )
        buttons.append([InlineKeyboardButton(
            text="◀️ Назад", callback_data=back_callback
        )])
        await callback.message.edit_text(
            f"{child_meta['icon']} <b>{escape(category.name)}</b>\n\n"
            f"Выберите {child_meta['title'].lower()}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    product.category_id = category.id
    await session.commit()
    await callback.message.edit_text(
        f"✅ Раздел товара изменён на «{escape(category.name)}».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"admin_edit_product_select_{product.id}"
            )]
        ]),
        parse_mode="HTML",
    )
    await callback.answer("Категория изменена")


@router.callback_query(F.data.startswith("set_category_"))
async def admin_edit_product_set_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Установка категории"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    category_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await callback.answer("Товар не выбран", show_alert=True)
        return

    category_result = await session.execute(select(Category).where(
        Category.id == category_id, Category.is_active.is_(True)
    ))
    category = category_result.scalar_one_or_none()
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if product:
        product.category_id = category_id
        await session.commit()
        await callback.answer("Категория изменена")
        await callback.message.edit_text(
            f"✅ Категория товара изменена на «{escape(category.name)}».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"admin_edit_product_select_{product_id}",
                )]
            ]),
        )
    else:
        await callback.answer("Товар не найден", show_alert=True)


@router.message(AdminStates.waiting_edit_product_value)
async def admin_edit_product_value(message: Message, state: FSMContext, session: AsyncSession):
    """Сохранение нового значения"""
    data = await state.get_data()
    product_id = data.get("product_id")
    field = data.get("edit_field")

    if not product_id or not field:
        await message.answer("Ошибка. Начните редактирование заново.")
        await state.clear()
        return

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await message.answer("Товар не найден")
        await state.clear()
        return

    try:
        if field == "price":
            set_fixed_pricing(product, message.text)
        elif field == "cost_plus":
            set_cost_plus_pricing(product, message.text)
            try:
                await refresh_product_cost_price(session, product)
            except FragmentPricingError as exc:
                await session.rollback()
                raise ValueError(
                    "Не удалось получить актуальную себестоимость Fragment. "
                    "Попробуйте ещё раз позже."
                ) from exc
        elif field == "name":
            product.name = message.text.strip()
        elif field == "description":
            product.description = message.text.strip()
        elif field == "format":
            product.format_info = message.text.strip()
        elif field == "recommendations":
            product.recommendations = message.text.strip()

        await session.commit()
        if field == "cost_plus":
            await message.answer(
                "✅ Цена рассчитана автоматически:\n"
                f"Себестоимость: {product.cost_price:.2f} ₽\n"
                f"Наценка: {product.markup_percent:.4f}%\n"
                f"Цена для пользователя: {product.price:.2f} ₽"
            )
        elif field == "price":
            await message.answer(
                f"✅ Установлена фиксированная цена: {product.price:.2f} ₽"
            )
        else:
            await message.answer(f"✅ Поле '{field}' обновлено!")
        await state.clear()

    except ValueError:
        await message.answer(
            "Неверный формат. Попробуйте снова:",
            reply_markup=_input_cancel_keyboard("admin_edit_product"),
        )


# ========== УДАЛЕНИЕ ТОВАРОВ ==========

@router.callback_query(F.data == "admin_delete_product")
async def admin_delete_product_start(callback: CallbackQuery, session: AsyncSession):
    """Начать удаление товара - показываем список"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await callback.answer("Системные товары Stars и Premium нельзя удалить", show_alert=True)
    return

    # Получаем все товары
    stmt = select(Product).where(Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES)).order_by(Product.name)
    result = await session.execute(stmt)
    products = result.scalars().all()

    if not products:
        await callback.message.edit_text(
            "❌ Товары не найдены",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")]
            ])
        )
        await callback.answer()
        return

    # Формируем список товаров с кнопками
    buttons = []
    text = "🗑️ <b>Удаление товара</b>\n\nВыберите товар для удаления:\n\n"

    for product in products[:50]:  # Ограничиваем 50 товарами
        status = "✅" if product.is_active else "❌"
        text += f"{status} <b>{escape(product.name)}</b> (ID: {product.id}, цена: {product.price:.2f} ₽, остаток: {product.stock_count})\n"
        buttons.append([InlineKeyboardButton(
            text=f"🗑️ {product.name}",
            callback_data=f"delete_product_{product.id}"
        )])

    if len(products) > 50:
        text += f"\n... и еще {len(products) - 50} товаров"

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_product_"))
async def admin_delete_product_confirm(callback: CallbackQuery, session: AsyncSession):
    """Подтверждение удаления товара"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    product_id = int(callback.data.split("_")[2])

    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if product.delivery_type not in VIRTUAL_DELIVERY_TYPES:
        await callback.answer("Удаление неразрешённых товаров заблокировано", show_alert=True)
        return

    # Проверяем, есть ли заказы с этим товаром
    stmt_orders = select(func.count(Order.id)).where(Order.product_id == product_id)
    result_orders = await session.execute(stmt_orders)
    orders_count = result_orders.scalar()

    keyboard = get_confirm_keyboard("delete_product", product_id)
    text = f"⚠️ <b>Подтвердите удаление</b>\n\n"
    text += f"Товар: <b>{escape(product.name)}</b>\n"
    text += f"Цена: {product.price:.2f} ₽\n"
    text += f"Остаток: {product.stock_count} шт.\n"
    if orders_count > 0:
        text += f"Заказов с этим товаром: {orders_count}\n"
    text += f"\nВы уверены?"

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_product_"))
async def admin_delete_product_execute(callback: CallbackQuery, session: AsyncSession):
    """Выполнить удаление товара"""
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
    if product.delivery_type not in VIRTUAL_DELIVERY_TYPES:
        await callback.answer("Удаление неразрешённых товаров заблокировано", show_alert=True)
        return

    # Проверяем, есть ли заказы с этим товаром
    stmt_orders = select(func.count(Order.id)).where(Order.product_id == product_id)
    result_orders = await session.execute(stmt_orders)
    orders_count = result_orders.scalar()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_products")]
    ])

    if orders_count > 0:
        # Не удаляем, а деактивируем
        product.is_active = False
        await session.commit()
        await callback.message.edit_text(
            f"✅ Товар деактивирован (есть {orders_count} заказов)",
            reply_markup=keyboard
        )
    else:
        # Удаляем полностью
        await session.delete(product)
        await session.commit()
        await callback.message.edit_text(
            "✅ Товар удален",
            reply_markup=keyboard
        )

    await callback.answer()


@router.callback_query(F.data.startswith("cancel_delete_product_"))
async def admin_delete_product_cancel(callback: CallbackQuery, session: AsyncSession):
    """Вернуться к товарам после отмены удаления."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await callback.message.edit_text(
        "❌ Удаление товара отменено.",
        reply_markup=get_admin_products_keyboard(),
    )
    await callback.answer()


