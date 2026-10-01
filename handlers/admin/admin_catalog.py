"""Административные обработчики: catalog."""
from .admin_context import *
from .admin_core import CATALOG_LEVELS


async def _move_catalog_item(
    session: AsyncSession, item: Category | Product, direction: str
) -> bool:
    """Переместить элемент среди соседей и сохранить его позицию."""
    if isinstance(item, Category):
        siblings = (await session.execute(
            select(Category)
            .where(Category.parent_id == item.parent_id)
            .order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
            .with_for_update()
        )).scalars().all()
    else:
        siblings = (await session.execute(
            select(Product)
            .where(
                Product.category_id == item.category_id,
                Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
            )
            .order_by(Product.sort_order.asc(), Product.name.asc(), Product.id.asc())
            .with_for_update()
        )).scalars().all()

    try:
        current_index = next(index for index, sibling in enumerate(siblings) if sibling.id == item.id)
    except StopIteration:
        return False
    if direction != "up":
        return False
    target_index = current_index - 1
    if target_index < 0:
        return False

    # Сначала нормализуем старые/дублированные значения, затем меняем два места.
    for index, sibling in enumerate(siblings):
        sibling.sort_order = index
    siblings[current_index].sort_order = target_index
    siblings[target_index].sort_order = current_index
    await session.commit()
    return True


async def _next_category_sort_order(
    session: AsyncSession, parent_id: int | None
) -> int:
    current_max = await session.scalar(
        select(func.max(Category.sort_order)).where(Category.parent_id == parent_id)
    )
    return int(current_max) + 1 if current_max is not None else 0


def _catalog_item_sort_key(
    item: Category, by_id: dict[int, Category]
) -> tuple[int, str, int, str, int]:
    parent = by_id.get(item.parent_id) if item.parent_id is not None else None
    return (
        parent.sort_order if parent is not None else -1,
        parent.name.casefold() if parent is not None else "",
        item.sort_order,
        item.name.casefold(),
        item.id,
    )


def _catalog_reorder_buttons(entity: str, item_id: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text="⬆️",
            callback_data=f"admin_catalog_reorder_{entity}_{item_id}_up",
        ),
    ]


async def _catalog_tree(
    session: AsyncSession,
) -> tuple[list[Category], dict[int, Category], dict[int, int]]:
    """Загрузить дерево каталога и безопасно вычислить глубину каждого узла."""
    nodes = (await session.execute(select(Category).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc()))).scalars().all()
    by_id = {node.id: node for node in nodes}
    depths: dict[int, int] = {}

    def resolve_depth(node: Category, trail: set[int] | None = None) -> int:
        if node.id in depths:
            return depths[node.id]
        trail = set() if trail is None else trail
        if node.id in trail:
            # Повреждённый цикл не должен превращать экран каталога в рекурсию.
            return -1
        if node.parent_id is None:
            depths[node.id] = 0
            return 0
        parent = by_id.get(node.parent_id)
        if parent is None:
            depths[node.id] = -1
            return -1
        parent_depth = resolve_depth(parent, trail | {node.id})
        depths[node.id] = parent_depth + 1 if parent_depth >= 0 else -1
        return depths[node.id]

    for node in nodes:
        resolve_depth(node)
    return nodes, by_id, depths


def _catalog_path(node: Category, by_id: dict[int, Category]) -> str:
    parts = [node.name]
    parent_id = node.parent_id
    visited = {node.id}
    while parent_id is not None and parent_id not in visited:
        visited.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            break
        parts.append(parent.name)
        parent_id = parent.parent_id
    return " → ".join(reversed(parts))


async def _catalog_node_has_depth(
    session: AsyncSession, node: Category | None, depth: int
) -> bool:
    if node is None:
        return False
    _, _, depths = await _catalog_tree(session)
    return depths.get(node.id) == depth


async def _categories_screen(
    session: AsyncSession, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    categories = (await session.execute(
        select(Category).where(Category.parent_id.is_(None)).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    )).scalars().all()
    buttons = []
    for category in categories[:80]:
        buttons.append([
            InlineKeyboardButton(
                text=f"{'✅' if category.is_active else '❌'} {category.name}"[:64],
                callback_data=f"admin_category_edit_{category.id}",
            ),
            *_catalog_reorder_buttons("category", category.id),
        ])
    buttons.extend([
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_category"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_delete_category"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")],
    ])
    detail = f"Категорий: <b>{len(categories)}</b>. Нажмите на название для редактирования."
    text = "📂 <b>Категории</b>\n\n" + detail
    if notice:
        text = f"{notice}\n\n{text}"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _subcategories_screen(
    session: AsyncSession, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    nodes, by_id, depths = await _catalog_tree(session)
    subcategories = [item for item in nodes if depths.get(item.id) == 1]
    subcategories.sort(key=lambda item: _catalog_item_sort_key(item, by_id))
    buttons = []
    for item in subcategories[:80]:
        buttons.append([
            InlineKeyboardButton(
                text=(
                    f"{'✅' if item.is_active else '❌'} {item.name} · "
                    f"{by_id[item.parent_id].name if item.parent_id in by_id else 'без категории'}"
                )[:64],
                callback_data=f"admin_subcategory_edit_{item.id}",
            ),
            *_catalog_reorder_buttons("subcategory", item.id),
        ])
    buttons.extend([
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_subcategory"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_delete_subcategory"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")],
    ])
    detail = f"Подкатегорий: <b>{len(subcategories)}</b>. Нажмите на название для редактирования."
    text = "🗂 <b>Подкатегории</b>\n\n" + detail
    if notice:
        text = f"{notice}\n\n{text}"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _catalog_level_screen(
    session: AsyncSession, level: str, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран групп или типов без смешивания соседних уровней."""
    meta = CATALOG_LEVELS[level]
    nodes, by_id, depths = await _catalog_tree(session)
    items = [item for item in nodes if depths.get(item.id) == meta["depth"]]
    items.sort(key=lambda item: _catalog_item_sort_key(item, by_id))
    buttons = []
    for item in items[:80]:
        buttons.append([
            InlineKeyboardButton(
                text=(
                    f"{'✅' if item.is_active else '❌'} {item.name} · "
                    f"{by_id[item.parent_id].name if item.parent_id in by_id else 'без родителя'}"
                )[:64],
                callback_data=f"admin_catalog_node_edit_{level}_{item.id}",
            ),
            *_catalog_reorder_buttons(level, item.id),
        ])
    buttons.extend([
        [InlineKeyboardButton(
            text="➕ Добавить", callback_data=f"admin_add_{level}"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")],
    ])
    text = (
        f"{meta['icon']} <b>{meta['title_plural']}</b>\n\n"
        f"{meta['title_plural']}: <b>{len(items)}</b>. "
        "Нажмите на название для редактирования."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _products_screen(
    session: AsyncSession, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    products = (await session.execute(
        select(Product)
        .join(Category, Product.category_id == Category.id)
        .where(Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES))
        .order_by(
            Category.sort_order.asc(), Category.name.asc(),
            Product.sort_order.asc(), Product.name.asc(), Product.id.asc()
        )
    )).scalars().all()
    category_ids = {product.category_id for product in products}
    category_names = {}
    if category_ids:
        category_rows = (await session.execute(
            select(Category.id, Category.name).where(Category.id.in_(category_ids))
        )).all()
        category_names = dict(category_rows)
    buttons = []
    for product in products[:50]:
        category_name = category_names.get(product.category_id, "без раздела")
        buttons.append([
            InlineKeyboardButton(
                text=f"{'✅' if product.is_active else '❌'} {product.name} · {category_name}"[:64],
                callback_data=f"admin_catalog_product_edit_{product.id}",
            ),
            *_catalog_reorder_buttons("product", product.id),
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")])
    shown = min(len(products), 50)
    detail = f"Товаров: <b>{len(products)}</b>. Нажмите на название для редактирования."
    if len(products) > shown:
        detail += f"\nПоказаны последние {shown}; полный список доступен по кнопке «Все товары»."
    text = "📦 <b>Товары</b>\n\n" + detail
    if notice:
        text = f"{notice}\n\n{text}"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_catalog")
async def admin_catalog_menu(callback: CallbackQuery, session: AsyncSession):
    """Меню управления каталогом"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "📂 <b>Управление каталогом</b>\n\nВыберите действие:",
        reply_markup=get_admin_catalog_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_catalog_categories")
async def admin_catalog_categories(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Открыть действия над категориями верхнего уровня."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _categories_screen(session)
    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_catalog_subcategories")
async def admin_catalog_subcategories(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Открыть действия над подкатегориями."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _subcategories_screen(session)
    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.in_({"admin_catalog_groups", "admin_catalog_types"}))
async def admin_catalog_deeper_levels(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    """Открыть отдельный список групп или типов."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    level = "group" if callback.data == "admin_catalog_groups" else "type"
    await state.clear()
    text, keyboard = await _catalog_level_screen(session, level)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_catalog_reorder_(category|subcategory|group|type|product)_\d+_up$"))
async def admin_catalog_reorder(
    callback: CallbackQuery, session: AsyncSession
):
    """Переместить категорию, раздел или товар вверх среди соседей."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(
        r"admin_catalog_reorder_(category|subcategory|group|type|product)_(\d+)_up",
        callback.data,
    )
    if match is None:
        await callback.answer("Некорректная команда", show_alert=True)
        return
    entity, item_id_text, direction = match.groups()
    item_id = int(item_id_text)
    if entity == "product":
        item = await session.get(Product, item_id)
        if item is None or item.delivery_type not in VIRTUAL_DELIVERY_TYPES:
            await callback.answer("Товар не найден", show_alert=True)
            return
    else:
        item = await _get_typed_catalog_node(session, entity, item_id)
        if item is None:
            await callback.answer("Раздел не найден", show_alert=True)
            return
    moved = await _move_catalog_item(session, item, direction)
    if not moved:
        await callback.answer(
            "Элемент уже находится у границы списка", show_alert=True
        )
        return

    if entity == "category":
        text, keyboard = await _categories_screen(session, "✅ Порядок изменён.")
    elif entity == "subcategory":
        text, keyboard = await _subcategories_screen(session, "✅ Порядок изменён.")
    elif entity in {"group", "type"}:
        text, keyboard = await _catalog_level_screen(session, entity, "✅ Порядок изменён.")
    else:
        text, keyboard = await _products_screen(session, "✅ Порядок изменён.")
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer("Порядок изменён")


@router.callback_query(F.data.in_({"admin_add_group", "admin_add_type"}))
async def admin_add_catalog_node_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    """Выбрать родительский раздел перед созданием группы или типа."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    level = "group" if callback.data == "admin_add_group" else "type"
    meta = CATALOG_LEVELS[level]
    parent_depth = meta["depth"] - 1
    nodes, by_id, depths = await _catalog_tree(session)
    parents = [
        item for item in nodes
        if depths.get(item.id) == parent_depth and item.is_active
    ]
    back_callback = f"admin_catalog_{level}s"
    if not parents:
        parent_title = CATALOG_LEVELS[
            "subcategory" if level == "group" else "group"
        ]["title_plural"].lower()
        await callback.message.edit_text(
            f"❌ Сначала создайте активные {parent_title}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)]
            ]),
        )
        await callback.answer()
        return
    buttons = [[InlineKeyboardButton(
        text=f"{CATALOG_LEVELS[level]['icon']} {_catalog_path(parent, by_id)}"[:64],
        callback_data=f"admin_catalog_node_parent_{level}_{parent.id}",
    )] for parent in parents]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    parent_title = CATALOG_LEVELS[
        "subcategory" if level == "group" else "group"
    ]["title"].lower()
    await state.clear()
    await callback.message.edit_text(
        f"{meta['icon']} <b>{meta['new_title']}</b>\n\n"
        f"Выберите {parent_title}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_catalog_node_parent_(group|type)_\d+$"))
async def admin_add_catalog_node_parent(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(r"admin_catalog_node_parent_(group|type)_(\d+)", callback.data)
    if match is None:
        await callback.answer("Некорректный раздел", show_alert=True)
        return
    level, parent_id_text = match.groups()
    parent = await session.get(Category, int(parent_id_text))
    expected_depth = CATALOG_LEVELS[level]["depth"] - 1
    if not await _catalog_node_has_depth(session, parent, expected_depth) or not parent.is_active:
        await callback.answer("Родительский раздел недоступен", show_alert=True)
        return
    await state.update_data(catalog_node_level=level, catalog_node_parent_id=parent.id)
    await state.set_state(AdminStates.waiting_catalog_node_name)
    meta = CATALOG_LEVELS[level]
    await callback.message.edit_text(
        f"{meta['icon']} <b>{meta['title']} в «{escape(parent.name)}»</b>\n\n"
        "Введите название:",
        reply_markup=_input_cancel_keyboard(f"admin_add_{level}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_catalog_node_name)
async def admin_add_catalog_node_finish(
    message: Message, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    data = await state.get_data()
    level = data.get("catalog_node_level")
    parent_id = data.get("catalog_node_parent_id")
    name = (message.text or "").strip()
    if level not in {"group", "type"}:
        await state.clear()
        return
    back_callback = f"admin_add_{level}"
    if not name:
        await edit_input_screen(
            message, state, "Название не может быть пустым. Попробуйте ещё раз.",
            reply_markup=_input_cancel_keyboard(back_callback), state_data=data,
        )
        return
    parent = await session.get(Category, parent_id) if isinstance(parent_id, int) else None
    if not await _catalog_node_has_depth(
        session, parent, CATALOG_LEVELS[level]["depth"] - 1
    ):
        await edit_input_screen(
            message, state, "Родительский раздел не найден. Начните создание заново.",
            reply_markup=_input_cancel_keyboard(f"admin_catalog_{level}s"), state_data=data,
        )
        await state.clear()
        return
    duplicate = await session.scalar(select(Category.id).where(Category.name == name))
    if duplicate is not None:
        await edit_input_screen(
            message, state, "Такое название уже занято. Введите другое:",
            reply_markup=_input_cancel_keyboard(back_callback), state_data=data,
        )
        return
    session.add(Category(
        name=name, parent_id=parent.id,
        sort_order=await _next_category_sort_order(session, parent.id),
    ))
    await session.commit()
    meta = CATALOG_LEVELS[level]
    text, keyboard = await _catalog_level_screen(
        session, level,
        f"✅ {meta['added']}: «{escape(name)}» — «{escape(parent.name)}».",
    )
    await edit_input_screen(
        message, state, text, reply_markup=keyboard, parse_mode="HTML", state_data=data,
    )
    await state.clear()


async def _catalog_node_editor_screen(
    session: AsyncSession, level: str, node: Category, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    meta = CATALOG_LEVELS[level]
    parent = await session.get(Category, node.parent_id)
    children_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == node.id)
    )
    products_count = await session.scalar(
        select(func.count(Product.id)).where(Product.category_id == node.id)
    )
    child_label = "Типов" if level == "group" else "Дочерних разделов"
    text = (
        f"{meta['icon']} <b>Редактирование: {meta['title'].lower()}</b>\n\n"
        f"Название: <b>{escape(node.name)}</b>\n"
        f"Родитель: <b>{escape(parent.name) if parent else 'не найден'}</b>\n"
        f"Статус: <b>{'Активна' if node.is_active else 'Отключена'}</b>\n"
        f"{child_label}: <b>{children_count or 0}</b>\n"
        f"Товаров: <b>{products_count or 0}</b>"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📝 Название", callback_data=f"admin_catalog_node_rename_{level}_{node.id}"
            ),
            InlineKeyboardButton(
                text="📂 Родитель", callback_data=f"admin_catalog_node_move_{level}_{node.id}"
            ),
        ],
        [InlineKeyboardButton(
            text="🔴 Отключить" if node.is_active else "🟢 Включить",
            callback_data=f"admin_catalog_node_toggle_{level}_{node.id}",
        )],
        [InlineKeyboardButton(
            text="🗑️ Удалить", callback_data=f"admin_catalog_node_delete_{level}_{node.id}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_catalog_{level}s")],
    ])
    return text, keyboard


async def _get_typed_catalog_node(
    session: AsyncSession, level: str, node_id: int
) -> Category | None:
    node = await session.get(Category, node_id)
    if not await _catalog_node_has_depth(session, node, CATALOG_LEVELS[level]["depth"]):
        return None
    return node


@router.callback_query(F.data.regexp(r"^admin_catalog_node_edit_(group|type)_\d+$"))
async def admin_catalog_node_edit(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(r"admin_catalog_node_edit_(group|type)_(\d+)", callback.data)
    level, node_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    if node is None:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _catalog_node_editor_screen(session, level, node)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_catalog_node_rename_(group|type)_\d+$"))
async def admin_catalog_node_rename(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(r"admin_catalog_node_rename_(group|type)_(\d+)", callback.data)
    level, node_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    if node is None:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    await state.update_data(catalog_node_level=level, catalog_node_edit_id=node.id)
    await state.set_state(AdminStates.waiting_catalog_node_edit_name)
    await callback.message.edit_text(
        f"📝 <b>Название</b>\n\nСейчас: <b>{escape(node.name)}</b>\n\n"
        "Введите новое название:",
        reply_markup=_input_cancel_keyboard(f"admin_catalog_node_edit_{level}_{node.id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_catalog_node_edit_name)
async def admin_catalog_node_rename_finish(
    message: Message, state: FSMContext, session: AsyncSession
):
    data = await state.get_data()
    level = data.get("catalog_node_level")
    node_id = data.get("catalog_node_edit_id")
    name = (message.text or "").strip()
    node = (
        await _get_typed_catalog_node(session, level, node_id)
        if level in {"group", "type"} and isinstance(node_id, int) else None
    )
    if node is None:
        await state.clear()
        return
    cancel = _input_cancel_keyboard(f"admin_catalog_node_edit_{level}_{node.id}")
    if not name:
        await edit_input_screen(
            message, state, "Название не может быть пустым. Введите другое:",
            reply_markup=cancel, state_data=data,
        )
        return
    duplicate = await session.scalar(
        select(Category.id).where(Category.name == name, Category.id != node.id)
    )
    if duplicate is not None:
        await edit_input_screen(
            message, state, "Такое название уже занято. Введите другое:",
            reply_markup=cancel, state_data=data,
        )
        return
    node.name = name
    await session.commit()
    text, keyboard = await _catalog_node_editor_screen(
        session, level, node, "✅ Название изменено."
    )
    await edit_input_screen(
        message, state, text, reply_markup=keyboard, parse_mode="HTML", state_data=data,
    )
    await state.clear()


@router.callback_query(F.data.regexp(r"^admin_catalog_node_toggle_(group|type)_\d+$"))
async def admin_catalog_node_toggle(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(r"admin_catalog_node_toggle_(group|type)_(\d+)", callback.data)
    level, node_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    if node is None:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    node.is_active = not node.is_active
    await session.commit()
    text, keyboard = await _catalog_node_editor_screen(session, level, node)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer("Раздел включён" if node.is_active else "Раздел выключен")


@router.callback_query(F.data.regexp(r"^admin_catalog_node_move_(group|type)_\d+$"))
async def admin_catalog_node_move(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(r"admin_catalog_node_move_(group|type)_(\d+)", callback.data)
    level, node_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    if node is None:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    nodes, by_id, depths = await _catalog_tree(session)
    parents = [
        item for item in nodes
        if depths.get(item.id) == CATALOG_LEVELS[level]["depth"] - 1 and item.is_active
    ]
    buttons = [[InlineKeyboardButton(
        text=("✅ " if item.id == node.parent_id else "📂 ") + _catalog_path(item, by_id)[:60],
        callback_data=f"admin_catalog_node_set_parent_{level}_{node.id}_{item.id}",
    )] for item in parents]
    buttons.append([InlineKeyboardButton(
        text="◀️ Назад", callback_data=f"admin_catalog_node_edit_{level}_{node.id}"
    )])
    await callback.message.edit_text(
        f"📂 <b>Родитель для «{escape(node.name)}»</b>\n\nВыберите новый раздел:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_catalog_node_set_parent_(group|type)_\d+_\d+$"))
async def admin_catalog_node_set_parent(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(
        r"admin_catalog_node_set_parent_(group|type)_(\d+)_(\d+)", callback.data
    )
    level, node_id_text, parent_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    parent = await session.get(Category, int(parent_id_text))
    if node is None or not await _catalog_node_has_depth(
        session, parent, CATALOG_LEVELS[level]["depth"] - 1
    ) or not parent.is_active:
        await callback.answer("Раздел недоступен", show_alert=True)
        return
    node.parent_id = parent.id
    await session.commit()
    text, keyboard = await _catalog_node_editor_screen(
        session, level, node, "✅ Родительский раздел изменён."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_catalog_node_delete_(group|type)_\d+$"))
async def admin_catalog_node_delete(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(r"admin_catalog_node_delete_(group|type)_(\d+)", callback.data)
    level, node_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    if node is None:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    children_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == node.id)
    ) or 0
    products_count = await session.scalar(
        select(func.count(Product.id)).where(Product.category_id == node.id)
    ) or 0
    if children_count or products_count:
        await callback.answer(
            "Сначала перенесите или удалите вложенные разделы и товары",
            show_alert=True,
        )
        return
    await callback.message.edit_text(
        f"⚠️ Удалить {CATALOG_LEVELS[level]['title'].lower()} "
        f"«<b>{escape(node.name)}</b>»?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Удалить",
                callback_data=f"admin_catalog_node_delete_confirm_{level}_{node.id}",
            )],
            [InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"admin_catalog_node_edit_{level}_{node.id}"
            )],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_catalog_node_delete_confirm_(group|type)_\d+$"))
async def admin_catalog_node_delete_confirm(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    match = re.fullmatch(
        r"admin_catalog_node_delete_confirm_(group|type)_(\d+)", callback.data
    )
    level, node_id_text = match.groups()
    node = await _get_typed_catalog_node(session, level, int(node_id_text))
    if node is None:
        await callback.answer("Раздел уже удалён", show_alert=True)
        return
    children_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == node.id)
    ) or 0
    products_count = await session.scalar(
        select(func.count(Product.id)).where(Product.category_id == node.id)
    ) or 0
    if children_count or products_count:
        await callback.answer("Раздел больше не пуст", show_alert=True)
        return
    name = node.name
    await session.delete(node)
    await session.commit()
    text, keyboard = await _catalog_level_screen(
        session, level, f"✅ «{escape(name)}» удалено."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_catalog_products")
async def admin_catalog_products(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Открыть действия над товарами и складом."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _products_screen(session)
    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()




@router.callback_query(F.data == "admin_add_category")
async def admin_add_category_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать добавление категории"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_category_name)
    keyboard = _input_cancel_keyboard("admin_catalog_categories")
    await callback.message.edit_text("Введите название категории:", reply_markup=keyboard)
    await callback.answer()


@router.message(AdminStates.waiting_category_name)
async def admin_add_category_finish(message: Message, state: FSMContext, session: AsyncSession):
    """Завершить добавление категории"""
    state_data = await state.get_data()
    category_name = (message.text or "").strip()

    if not category_name:
        await edit_input_screen(
            message,
            state,
            "Название не может быть пустым. Попробуйте снова:",
            reply_markup=_input_cancel_keyboard("admin_catalog_categories"),
            state_data=state_data,
        )
        return

    # Проверяем на дубликаты
    stmt = select(Category).where(Category.name == category_name)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        await edit_input_screen(
            message,
            state,
            "Категория с таким названием уже существует. Введите другое название:",
            reply_markup=_input_cancel_keyboard("admin_catalog_categories"),
            state_data=state_data,
        )
        return

    category = Category(
        name=category_name,
        sort_order=await _next_category_sort_order(session, None),
    )
    session.add(category)
    await session.commit()

    text, keyboard = await _categories_screen(
        session, f"✅ Категория «{escape(category_name)}» добавлена."
    )
    await edit_input_screen(
        message,
        state,
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        state_data=state_data,
    )
    await state.clear()


@router.callback_query(F.data == "admin_add_subcategory")
async def admin_add_subcategory_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Выбрать основную категорию для новой подкатегории."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    categories = (await session.execute(
        select(Category).where(Category.parent_id.is_(None), Category.is_active.is_(True)).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    )).scalars().all()
    if not categories:
        await callback.message.edit_text(
            "❌ Сначала создайте хотя бы одну категорию.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")]
            ]),
        )
        await callback.answer()
        return
    buttons = [[InlineKeyboardButton(
        text=f"📂 {category.name}",
        callback_data=f"admin_subcategory_parent_{category.id}",
    )] for category in categories]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")])
    await callback.message.edit_text(
        "🗂 <b>Новая подкатегория</b>\n\nВыберите родительскую категорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_subcategory_parent_"))
async def admin_subcategory_parent_select(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    parent_id = int(callback.data.removeprefix("admin_subcategory_parent_"))
    parent = await session.get(Category, parent_id)
    if not parent or parent.parent_id is not None or not parent.is_active:
        await callback.answer("Категория недоступна", show_alert=True)
        return
    await state.update_data(subcategory_parent_id=parent_id)
    await state.set_state(AdminStates.waiting_subcategory_name)
    await callback.message.edit_text(
        f"🗂 <b>Подкатегория в «{escape(parent.name)}»</b>\n\nВведите название:",
        reply_markup=_input_cancel_keyboard("admin_add_subcategory"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_subcategory_name)
async def admin_add_subcategory_finish(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    state_data = await state.get_data()
    name = (message.text or "").strip()
    parent_id = state_data.get("subcategory_parent_id")
    parent = await session.get(Category, parent_id) if isinstance(parent_id, int) else None
    if not name:
        await edit_input_screen(
            message,
            state,
            "Название не может быть пустым. Попробуйте ещё раз.",
            reply_markup=_input_cancel_keyboard("admin_add_subcategory"),
            state_data=state_data,
        )
        return
    if not parent or parent.parent_id is not None:
        await edit_input_screen(
            message,
            state,
            "Категория не найдена. Начните создание подкатегории заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")]
            ]),
            state_data=state_data,
        )
        await state.clear()
        return
    existing = await session.scalar(select(Category).where(Category.name == name))
    if existing:
        await edit_input_screen(
            message,
            state,
            "Подкатегория с таким названием уже существует. Введите другое название.",
            reply_markup=_input_cancel_keyboard("admin_add_subcategory"),
            state_data=state_data,
        )
        return
    session.add(Category(
        name=name, parent_id=parent.id,
        sort_order=await _next_category_sort_order(session, parent.id),
    ))
    await session.commit()
    text, keyboard = await _subcategories_screen(
        session,
        f"✅ Подкатегория «{escape(name)}» добавлена в «{escape(parent.name)}».",
    )
    await edit_input_screen(
        message,
        state,
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        state_data=state_data,
    )
    await state.clear()


async def _category_editor_screen(
    session: AsyncSession, category: Category, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    subcategories_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == category.id)
    )
    products_count = await session.scalar(
        select(func.count(Product.id)).where(Product.category_id == category.id)
    )
    text = (
        "📂 <b>Редактирование категории</b>\n\n"
        f"Название: <b>{escape(category.name)}</b>\n"
        f"Статус: <b>{'Активна' if category.is_active else 'Отключена'}</b>\n"
        f"Подкатегорий: <b>{subcategories_count or 0}</b>\n"
        f"Товаров: <b>{products_count or 0}</b>"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📝 Изменить название",
            callback_data=f"admin_category_rename_{category.id}",
        )],
        [InlineKeyboardButton(
            text="🔴 Отключить" if category.is_active else "🟢 Включить",
            callback_data=f"admin_category_toggle_{category.id}",
        )],
        [InlineKeyboardButton(
            text="🗑️ Удалить",
            callback_data=f"delete_category_{category.id}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_categories")],
    ])
    return text, keyboard


@router.callback_query(F.data.regexp(r"^admin_category_edit_\d+$"))
async def admin_category_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    category_id = int(callback.data.rsplit("_", 1)[1])
    category = await session.get(Category, category_id)
    if category is None or category.parent_id is not None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _category_editor_screen(session, category)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_category_rename_\d+$"))
async def admin_category_rename(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    category_id = int(callback.data.rsplit("_", 1)[1])
    category = await session.get(Category, category_id)
    if category is None or category.parent_id is not None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.update_data(catalog_edit_category_id=category.id)
    await state.set_state(AdminStates.waiting_category_edit_name)
    await callback.message.edit_text(
        "📝 <b>Название категории</b>\n\n"
        f"Сейчас: <b>{escape(category.name)}</b>\n\nВведите новое название:",
        reply_markup=_input_cancel_keyboard(f"admin_category_edit_{category.id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_category_edit_name)
async def admin_category_rename_finish(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    category = await session.get(Category, data.get("catalog_edit_category_id"))
    name = (message.text or "").strip()
    if category is None or category.parent_id is not None:
        await edit_input_screen(
            message, state, "Категория не найдена.",
            reply_markup=get_admin_categories_keyboard(), state_data=data,
        )
        await state.clear()
        return
    if not name:
        await edit_input_screen(
            message, state, "Название не может быть пустым. Введите новое название:",
            reply_markup=_input_cancel_keyboard(f"admin_category_edit_{category.id}"),
            state_data=data,
        )
        return
    duplicate = await session.scalar(
        select(Category.id).where(Category.name == name, Category.id != category.id)
    )
    if duplicate is not None:
        await edit_input_screen(
            message, state, "Такое название уже занято. Введите другое:",
            reply_markup=_input_cancel_keyboard(f"admin_category_edit_{category.id}"),
            state_data=data,
        )
        return
    category.name = name
    await session.commit()
    text, keyboard = await _category_editor_screen(session, category, "✅ Название изменено.")
    await edit_input_screen(
        message, state, text, reply_markup=keyboard, parse_mode="HTML", state_data=data,
    )
    await state.clear()


@router.callback_query(F.data.regexp(r"^admin_category_toggle_\d+$"))
async def admin_category_toggle(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    category_id = int(callback.data.rsplit("_", 1)[1])
    category = await session.get(Category, category_id)
    if category is None or category.parent_id is not None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    category.is_active = not category.is_active
    await session.commit()
    text, keyboard = await _category_editor_screen(session, category, "✅ Статус изменён.")
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def _subcategory_editor_screen(
    session: AsyncSession, subcategory: Category, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    parent = await session.get(Category, subcategory.parent_id)
    groups_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == subcategory.id)
    )
    products_count = await session.scalar(
        select(func.count(Product.id)).where(Product.category_id == subcategory.id)
    )
    text = (
        "🗂 <b>Редактирование подкатегории</b>\n\n"
        f"Название: <b>{escape(subcategory.name)}</b>\n"
        f"Категория: <b>{escape(parent.name) if parent else 'не найдена'}</b>\n"
        f"Статус: <b>{'Активна' if subcategory.is_active else 'Отключена'}</b>\n"
        f"Групп: <b>{groups_count or 0}</b>\n"
        f"Товаров: <b>{products_count or 0}</b>"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📝 Изменить название",
            callback_data=f"admin_subcategory_rename_{subcategory.id}",
        )],
        [InlineKeyboardButton(
            text="📂 Изменить категорию",
            callback_data=f"catalog_subcategory_parent_{subcategory.id}",
        )],
        [InlineKeyboardButton(
            text="🔴 Отключить" if subcategory.is_active else "🟢 Включить",
            callback_data=f"admin_subcategory_toggle_{subcategory.id}",
        )],
        [InlineKeyboardButton(
            text="🗑️ Удалить",
            callback_data=f"delete_subcategory_{subcategory.id}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")],
    ])
    return text, keyboard


@router.callback_query(F.data.regexp(r"^admin_subcategory_edit_\d+$"))
async def admin_subcategory_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    subcategory_id = int(callback.data.rsplit("_", 1)[1])
    subcategory = await session.get(Category, subcategory_id)
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _subcategory_editor_screen(session, subcategory)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_subcategory_rename_\d+$"))
async def admin_subcategory_rename(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    subcategory_id = int(callback.data.rsplit("_", 1)[1])
    subcategory = await session.get(Category, subcategory_id)
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return
    await state.update_data(catalog_edit_subcategory_id=subcategory.id)
    await state.set_state(AdminStates.waiting_subcategory_edit_name)
    await callback.message.edit_text(
        "📝 <b>Название подкатегории</b>\n\n"
        f"Сейчас: <b>{escape(subcategory.name)}</b>\n\nВведите новое название:",
        reply_markup=_input_cancel_keyboard(f"admin_subcategory_edit_{subcategory.id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_subcategory_edit_name)
async def admin_subcategory_rename_finish(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    subcategory = await session.get(Category, data.get("catalog_edit_subcategory_id"))
    name = (message.text or "").strip()
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await edit_input_screen(
            message, state, "Подкатегория не найдена.",
            reply_markup=get_admin_subcategories_keyboard(), state_data=data,
        )
        await state.clear()
        return
    if not name:
        await edit_input_screen(
            message, state, "Название не может быть пустым. Введите новое название:",
            reply_markup=_input_cancel_keyboard(f"admin_subcategory_edit_{subcategory.id}"),
            state_data=data,
        )
        return
    duplicate = await session.scalar(
        select(Category.id).where(Category.name == name, Category.id != subcategory.id)
    )
    if duplicate is not None:
        await edit_input_screen(
            message, state, "Такое название уже занято. Введите другое:",
            reply_markup=_input_cancel_keyboard(f"admin_subcategory_edit_{subcategory.id}"),
            state_data=data,
        )
        return
    subcategory.name = name
    await session.commit()
    text, keyboard = await _subcategory_editor_screen(
        session, subcategory, "✅ Название изменено."
    )
    await edit_input_screen(
        message, state, text, reply_markup=keyboard, parse_mode="HTML", state_data=data,
    )
    await state.clear()


@router.callback_query(F.data.regexp(r"^admin_subcategory_toggle_\d+$"))
async def admin_subcategory_toggle(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    subcategory_id = int(callback.data.rsplit("_", 1)[1])
    subcategory = await session.get(Category, subcategory_id)
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return
    subcategory.is_active = not subcategory.is_active
    await session.commit()
    text, keyboard = await _subcategory_editor_screen(
        session, subcategory, "✅ Статус изменён."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^catalog_subcategory_parent_\d+$"))
async def admin_subcategory_parent_edit(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    subcategory_id = int(callback.data.rsplit("_", 1)[1])
    subcategory = await session.get(Category, subcategory_id)
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return
    categories = (await session.execute(
        select(Category).where(Category.parent_id.is_(None), Category.is_active.is_(True))
        .order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    )).scalars().all()
    buttons = [[InlineKeyboardButton(
        text=("✅ " if item.id == subcategory.parent_id else "📂 ") + item.name,
        callback_data=f"catalog_subcategory_set_parent_{subcategory.id}_{item.id}",
    )] for item in categories]
    buttons.append([InlineKeyboardButton(
        text="◀️ Назад", callback_data=f"admin_subcategory_edit_{subcategory.id}"
    )])
    await callback.message.edit_text(
        "📂 <b>Категория подкатегории</b>\n\nВыберите новую категорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^catalog_subcategory_set_parent_\d+_\d+$"))
async def admin_subcategory_parent_save(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    tail = callback.data.removeprefix("catalog_subcategory_set_parent_")
    subcategory_id, parent_id = map(int, tail.split("_"))
    subcategory = await session.get(Category, subcategory_id)
    parent = await session.get(Category, parent_id)
    if (
        not await _catalog_node_has_depth(session, subcategory, 1)
        or parent is None or parent.parent_id is not None or not parent.is_active
    ):
        await callback.answer("Категория недоступна", show_alert=True)
        return
    subcategory.parent_id = parent.id
    await session.commit()
    text, keyboard = await _subcategory_editor_screen(
        session, subcategory, "✅ Категория изменена."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_delete_subcategory")
async def admin_delete_subcategory_start(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    nodes, _, depths = await _catalog_tree(session)
    subcategories = [item for item in nodes if depths.get(item.id) == 1]
    if not subcategories:
        await callback.message.edit_text(
            "🗂 Подкатегорий пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")]
            ]),
        )
        await callback.answer()
        return
    parent_categories = (
        await session.execute(select(Category).where(Category.parent_id.is_(None)))
    ).scalars().all()
    parent_names = {category.id: category.name for category in parent_categories}
    buttons = []
    for subcategory in subcategories:
        parent_name = parent_names.get(subcategory.parent_id, "удалённая категория")
        buttons.append([InlineKeyboardButton(
            text=f"🗑️ {subcategory.name} · {parent_name}",
            callback_data=f"delete_subcategory_{subcategory.id}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")])
    await callback.message.edit_text(
        "🗑️ <b>Удаление подкатегории</b>\n\nВыберите подкатегорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_subcategory_"))
async def admin_delete_subcategory_confirm(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    subcategory_id = int(callback.data.removeprefix("delete_subcategory_"))
    subcategory = await session.get(Category, subcategory_id)
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return
    children_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == subcategory.id)
    ) or 0
    products_count = await session.scalar(
        select(func.count(Product.id)).where(Product.category_id == subcategory.id)
    ) or 0
    if children_count:
        await callback.answer(
            "Сначала удалите или перенесите группы этой подкатегории",
            show_alert=True,
        )
        return
    await callback.message.edit_text(
        "⚠️ <b>Подтвердите удаление</b>\n\n"
        f"Подкатегория: <b>{escape(subcategory.name)}</b>\n"
        f"Товаров: <b>{products_count or 0}</b>\n\n"
        "Товары в подкатегории будут деактивированы.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Удалить", callback_data=f"confirm_delete_subcategory_{subcategory.id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_delete_subcategory")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_subcategory_"))
async def admin_delete_subcategory_execute(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    subcategory_id = int(callback.data.removeprefix("confirm_delete_subcategory_"))
    subcategory = await session.get(Category, subcategory_id)
    if not await _catalog_node_has_depth(session, subcategory, 1):
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return
    children_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == subcategory.id)
    ) or 0
    if children_count:
        await callback.answer(
            "Сначала удалите или перенесите группы этой подкатегории",
            show_alert=True,
        )
        return
    products = (await session.execute(select(Product).where(Product.category_id == subcategory.id))).scalars().all()
    if products:
        for product in products:
            product.is_active = False
        subcategory.is_active = False
        result_text = f"✅ Подкатегория «{escape(subcategory.name)}» и {len(products)} товар(а) деактивированы."
    else:
        await session.delete(subcategory)
        result_text = f"✅ Подкатегория «{escape(subcategory.name)}» удалена."
    await session.commit()
    await callback.message.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_subcategories")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_delete_category")
async def admin_delete_category_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать удаление категории - показываем список"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Получаем все категории
    stmt = select(Category).where(Category.parent_id.is_(None)).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    result = await session.execute(stmt)
    categories = result.scalars().all()

    if not categories:
        await callback.message.edit_text(
            "❌ Категории не найдены",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_categories")]
            ])
        )
        await callback.answer()
        return

    # Формируем список категорий с кнопками
    buttons = []
    text = "🗑️ <b>Удаление категории</b>\n\nВыберите категорию для удаления:\n\n"

    for category in categories:
        # Проверяем количество товаров
        stmt_products = select(func.count(Product.id)).where(Product.category_id == category.id)
        result_products = await session.execute(stmt_products)
        products_count = result_products.scalar()

        status = "✅" if category.is_active else "❌"
        text += f"{status} <b>{escape(category.name)}</b> (ID: {category.id}, товаров: {products_count})\n"
        buttons.append([InlineKeyboardButton(
            text=f"🗑️ {category.name}",
            callback_data=f"delete_category_{category.id}"
        )])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_categories")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_category_"))
async def admin_delete_category_confirm(callback: CallbackQuery, session: AsyncSession):
    """Подтверждение удаления категории"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    category_id = int(callback.data.split("_")[2])

    stmt = select(Category).where(Category.id == category_id)
    result = await session.execute(stmt)
    category = result.scalar_one_or_none()

    if not category:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    if category.parent_id is not None:
        await callback.answer("Выберите подкатегорию в отдельном разделе", show_alert=True)
        return

    subcategories_count = await session.scalar(
        select(func.count(Category.id)).where(Category.parent_id == category.id)
    )
    if subcategories_count:
        await callback.message.edit_text(
            "⚠️ <b>В категории есть подкатегории.</b>\n\n"
            "Сначала удалите или деактивируйте их в разделе «Подкатегории».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_categories")]
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Проверяем, есть ли товары в категории
    stmt_products = select(func.count(Product.id)).where(Product.category_id == category_id)
    result_products = await session.execute(stmt_products)
    products_count = result_products.scalar()

    if products_count > 0:
        keyboard = get_confirm_keyboard("delete_category", category_id)
        await callback.message.edit_text(
            f"⚠️ <b>Внимание!</b>\n\n"
            f"Категория: <b>{escape(category.name)}</b>\n"
            f"В категории находится <b>{products_count}</b> товар(ов).\n\n"
            f"При удалении категории все товары будут деактивированы.\n\n"
            f"Вы уверены, что хотите удалить категорию?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    else:
        keyboard = get_confirm_keyboard("delete_category", category_id)
        await callback.message.edit_text(
            f"⚠️ <b>Подтвердите удаление</b>\n\n"
            f"Категория: <b>{escape(category.name)}</b>\n"
            f"Товаров в категории: 0\n\n"
            f"Вы уверены?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_category_"))
async def admin_delete_category_execute(callback: CallbackQuery, session: AsyncSession):
    """Выполнить удаление категории"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    category_id = int(callback.data.split("_")[3])

    stmt = select(Category).where(Category.id == category_id)
    result = await session.execute(stmt)
    category = result.scalar_one_or_none()

    if not category:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    # Проверяем, есть ли товары в категории
    stmt_products = select(Product).where(Product.category_id == category_id)
    result_products = await session.execute(stmt_products)
    products = result_products.scalars().all()

    if products:
        # Деактивируем все товары в категории
        for product in products:
            product.is_active = False

        # Деактивируем категорию вместо удаления
        category.is_active = False
        await session.commit()

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_categories")]
        ])
        await callback.message.edit_text(
            f"✅ Категория <b>{escape(category.name)}</b> деактивирована\n\n"
            f"Деактивировано товаров: {len(products)}\n\n"
            f"Категория и товары скрыты из каталога, но сохранены в базе данных.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    else:
        # Удаляем категорию полностью, если нет товаров
        await session.delete(category)
        await session.commit()

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog_categories")]
        ])
        await callback.message.edit_text(
            f"✅ Категория <b>{escape(category.name)}</b> удалена",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    await callback.answer()


@router.callback_query(F.data.startswith("cancel_delete_category_"))
async def admin_delete_category_cancel(callback: CallbackQuery, session: AsyncSession):
    """Отмена удаления категории"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "❌ Удаление категории отменено",
        reply_markup=get_admin_categories_keyboard()
    )
    await callback.answer("Удаление отменено")


@router.callback_query(F.data == "admin_add_product")
async def admin_add_product_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать добавление товара"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await callback.answer("Товары Stars и Premium создаются системно; изменяйте их карточки", show_alert=True)
    return

    await state.set_state(AdminStates.waiting_product_name)
    await callback.message.edit_text(
        "Введите название товара:",
        reply_markup=_input_cancel_keyboard("admin_catalog_products"),
    )
    await callback.answer()


async def _product_category_root_keyboard(session: AsyncSession) -> InlineKeyboardMarkup:
    categories = (await session.execute(
        select(Category).where(
            Category.parent_id.is_(None), Category.is_active.is_(True)
        ).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    )).scalars().all()
    buttons = [[InlineKeyboardButton(
        text=f"📂 {category.name}",
        callback_data=f"admin_select_category_{category.id}",
    )] for category in categories]
    buttons.append([InlineKeyboardButton(
        text="⛔ Отмена", callback_data="admin_cancel_add_product"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(AdminStates.waiting_product_name)
async def admin_add_product_name(message: Message, state: FSMContext):
    """Обработка названия товара"""
    data = await state.get_data()
    name = (message.text or "").strip()
    if not name:
        await edit_input_screen(
            message,
            state,
            "Название не может быть пустым. Введите название товара:",
            reply_markup=_input_cancel_keyboard("admin_cancel_add_product"),
            state_data=data,
        )
        return
    await state.update_data(name=name)
    await state.set_state(AdminStates.waiting_product_price)
    await edit_input_screen(
        message,
        state,
        "Введите цену за единицу (число):",
        reply_markup=_input_cancel_keyboard("admin_cancel_add_product"),
        state_data=data,
    )


@router.message(AdminStates.waiting_product_price)
async def admin_add_product_price(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка цены товара"""
    data = await state.get_data()
    try:
        price = to_money(message.text or "", minimum=Decimal("0.01"))

        await state.update_data(price=price)
        await state.set_state(AdminStates.waiting_product_category)

        # Сначала показываем только корневые категории. Подкатегории появятся
        # отдельным следующим шагом после выбора родителя.
        stmt = select(Category).where(
            Category.parent_id.is_(None), Category.is_active.is_(True)
        )
        result = await session.execute(stmt)
        categories = result.scalars().all()

        if not categories:
            await edit_input_screen(
                message,
                state,
                "❌ Нет активных категорий. Сначала создайте категорию.\n"
                "Или введите ID категории вручную:",
                reply_markup=_input_cancel_keyboard("admin_cancel_add_product"),
                state_data=data,
            )
            return

        keyboard = await _product_category_root_keyboard(session)

        await edit_input_screen(
            message,
            state,
            "📂 <b>Выберите категорию для товара:</b>",
            reply_markup=keyboard,
            parse_mode="HTML",
            state_data=data,
        )

    except ValueError:
        await edit_input_screen(
            message,
            state,
            "Введите корректную цену (число):",
            reply_markup=_input_cancel_keyboard("admin_cancel_add_product"),
            state_data=data,
        )


@router.callback_query(F.data.startswith("admin_select_category_"))
async def admin_select_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Обработка выбора категории при добавлении товара"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != AdminStates.waiting_product_category:
        await callback.answer("Ошибка: неверное состояние", show_alert=True)
        return

    # Извлекаем ID категории
    category_id = int(callback.data.split("_")[-1])

    # Проверяем существование категории
    stmt = select(Category).where(Category.id == category_id, Category.is_active.is_(True))
    result = await session.execute(stmt)
    category = result.scalar_one_or_none()

    if not category:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    # Проходим дерево последовательно: категория → подкатегория → группа → тип.
    # Товар привязывается к выбранному конечному разделу.
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
            callback_data=f"admin_select_category_{child.id}",
        )] for child in children]
        back_callback = (
            f"admin_select_category_{category.parent_id}"
            if category.parent_id is not None
            else "admin_product_category_roots"
        )
        buttons.extend([
            [InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)],
            [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
        ])
        await callback.message.edit_text(
            f"{child_meta['icon']} <b>{escape(category.name)}</b>\n\n"
            f"Выберите {child_meta['title'].lower()}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Получаем данные из состояния
    data = await state.get_data()
    product_name = data.get("name")
    product_price = data.get("price")

    if not product_name or not product_price:
        await callback.answer("Ошибка: данные товара не найдены", show_alert=True)
        await state.clear()
        return

    # Сохраняем category_id и переходим к опциональным полям
    await state.update_data(category_id=category_id)
    await state.set_state(AdminStates.waiting_product_description)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="admin_skip_description")],
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
    ])

    await callback.message.edit_text(
        f"📂 Раздел выбран: <b>{escape(category.name)}</b>\n\n"
        f"Теперь можно добавить опциональные поля:\n\n"
        f"1️⃣ <b>Описание</b>\n\n"
        f"Введите описание товара или нажмите кнопку для пропуска:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_product_category_roots")
async def admin_product_category_roots(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    if await state.get_state() != AdminStates.waiting_product_category:
        await callback.answer("Добавление товара уже завершено", show_alert=True)
        return
    await callback.message.edit_text(
        "📂 <b>Выберите категорию для товара:</b>",
        reply_markup=await _product_category_root_keyboard(session),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_cancel_add_product")
async def admin_cancel_add_product(callback: CallbackQuery, state: FSMContext):
    """Отмена добавления товара"""
    await state.clear()
    await callback.message.edit_text(
        "📦 <b>Управление товарами</b>\n\nДобавление товара отменено.",
        reply_markup=get_admin_products_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_product_category)
async def admin_add_product_category(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка категории товара (резервный метод через ввод ID)"""
    data = await state.get_data()
    if message.text == "/cancel":
        await edit_input_screen(
            message,
            state,
            "📦 <b>Управление товарами</b>\n\nДобавление товара отменено.",
            reply_markup=get_admin_products_keyboard(),
            state_data=data,
        )
        await state.clear()
        return

    try:
        category_id = int(message.text)

        stmt = select(Category).where(Category.id == category_id, Category.is_active.is_(True))
        result = await session.execute(stmt)
        category = result.scalar_one_or_none()

        if not category:
            await edit_input_screen(
                message,
                state,
                "Категория не найдена. Введите корректный ID:",
                reply_markup=_input_cancel_keyboard("admin_cancel_add_product"),
                state_data=data,
            )
            return

        # Сохраняем category_id и переходим к опциональным полям
        await state.update_data(category_id=category_id)
        await state.set_state(AdminStates.waiting_product_description)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="admin_skip_description")],
            [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
        ])

        await edit_input_screen(
            message,
            state,
            f"📂 Категория выбрана: <b>{escape(category.name)}</b>\n\n"
            f"Теперь можно добавить опциональные поля:\n\n"
            f"1️⃣ <b>Описание</b>\n\n"
            f"Введите описание товара или нажмите кнопку для пропуска:",
            reply_markup=keyboard,
            parse_mode="HTML",
            state_data=data,
        )

    except (TypeError, ValueError):
        await edit_input_screen(
            message,
            state,
            "Введите корректный ID категории (число) или выберите категорию из списка выше:",
            reply_markup=_input_cancel_keyboard("admin_cancel_add_product"),
            state_data=data,
        )


@router.message(AdminStates.waiting_product_description)
async def admin_add_product_description(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка описания товара"""
    data = await state.get_data()
    description = None
    if message.text and message.text.strip().lower() != "/skip":
        description = message.text.strip()

    await state.update_data(description=description)
    await state.set_state(AdminStates.waiting_product_format)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="admin_skip_format")],
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
    ])

    await edit_input_screen(
        message,
        state,
        f"2️⃣ <b>Формат</b>\n\n"
        f"Введите формат выдаваемых аккаунтов (например: 'login:password') или нажмите кнопку для пропуска:",
        reply_markup=keyboard,
        parse_mode="HTML",
        state_data=data,
    )


@router.callback_query(F.data == "admin_skip_description")
async def admin_skip_description(callback: CallbackQuery, state: FSMContext):
    """Пропустить описание товара"""
    await state.update_data(description=None)
    await state.set_state(AdminStates.waiting_product_format)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="admin_skip_format")],
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
    ])

    await callback.message.edit_text(
        f"2️⃣ <b>Формат</b>\n\n"
        f"Введите формат выдаваемых аккаунтов (например: 'login:password') или нажмите кнопку для пропуска:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer("Описание пропущено")


@router.message(AdminStates.waiting_product_format)
async def admin_add_product_format(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка формата товара"""
    data = await state.get_data()
    format_info = None
    if message.text and message.text.strip().lower() != "/skip":
        format_info = message.text.strip()

    await state.update_data(format_info=format_info)
    await state.set_state(AdminStates.waiting_product_recommendations)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="admin_skip_recommendations")],
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
    ])

    await edit_input_screen(
        message,
        state,
        f"3️⃣ <b>Рекомендации</b>\n\n"
        f"Введите рекомендации к покупке или нажмите кнопку для пропуска:",
        reply_markup=keyboard,
        parse_mode="HTML",
        state_data=data,
    )


@router.callback_query(F.data == "admin_skip_format")
async def admin_skip_format(callback: CallbackQuery, state: FSMContext):
    """Пропустить формат товара"""
    await state.update_data(format_info=None)
    await state.set_state(AdminStates.waiting_product_recommendations)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="admin_skip_recommendations")],
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_cancel_add_product")],
    ])

    await callback.message.edit_text(
        f"3️⃣ <b>Рекомендации</b>\n\n"
        f"Введите рекомендации к покупке или нажмите кнопку для пропуска:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer("Формат пропущен")


@router.message(AdminStates.waiting_product_recommendations)
async def admin_add_product_recommendations(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка рекомендаций и создание товара"""

    recommendations = None
    if message.text and message.text.strip().lower() != "/skip":
        recommendations = message.text.strip()

    # Создаем товар
    await _create_product_from_state(state, session, message, recommendations)


@router.callback_query(F.data == "admin_skip_recommendations")
async def admin_skip_recommendations(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Пропустить рекомендации и создать товар"""
    await _create_product_from_state(state, session, callback.message, None)
    await callback.answer("Рекомендации пропущены")


async def _create_product_from_state(state: FSMContext, session: AsyncSession, message_obj, recommendations=None):
    """Вспомогательная функция для создания товара из состояния"""
    # Получаем все данные из состояния
    data = await state.get_data()
    product_name = data.get("name")
    product_price = data.get("price")
    category_id = data.get("category_id")
    description = data.get("description")
    format_info = data.get("format_info")

    if recommendations is None:
        recommendations = data.get("recommendations")

    if not product_name or not product_price or not category_id:
        error_text = "❌ Ошибка: данные товара не найдены"
        await edit_input_screen(
            message_obj,
            state,
            error_text,
            reply_markup=get_admin_products_keyboard(),
            state_data=data,
        )
        await state.clear()
        return

    # Получаем категорию для отображения
    stmt = select(Category).where(Category.id == category_id)
    result = await session.execute(stmt)
    category = result.scalar_one_or_none()

    if not category:
        error_text = "❌ Категория не найдена"
        await edit_input_screen(
            message_obj,
            state,
            error_text,
            reply_markup=get_admin_products_keyboard(),
            state_data=data,
        )
        await state.clear()
        return

    # This route is retained for compatibility with old callbacks, but the
    # The virtual catalog is intentionally immutable: system plans are edited
    # from their cards instead of creating legacy account products.
    await edit_input_screen(
        message_obj,
        state,
        "Нельзя добавлять дополнительные товары вручную; изменяйте системные планы Stars и Premium.",
        reply_markup=get_admin_products_keyboard(),
        state_data=data,
    )
    await state.clear()
    return

    # Создаем товар
    product = Product(
        name=product_name,
        price=product_price,
        category_id=category_id,
        stock_count=0,
        description=description,
        format_info=format_info,
        recommendations=recommendations
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)

    text, keyboard = await _products_screen(
        session, f"✅ Товар «{escape(product_name)}» добавлен."
    )

    await edit_input_screen(
        message_obj,
        state,
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        state_data=data,
    )

    await state.clear()


# Статистика
SALES_STATUSES = ("ОПЛАЧЕНО", "ВЫПОЛНЕНО")
SALES_PER_PAGE = 8
USERS_PER_PAGE = 10
RUSSIAN_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


