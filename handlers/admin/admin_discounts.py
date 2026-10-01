"""Административные обработчики: discounts."""
from .admin_context import *
from .admin_catalog import _catalog_tree

def _global_discount_draft(data: dict) -> dict:
    draft = {**GLOBAL_DISCOUNT_DRAFT_DEFAULTS, **(data.get("global_discount_draft") or {})}
    draft["target_ids"] = sorted({int(item) for item in draft["target_ids"] if str(item).isdigit()})
    return draft


def _global_discount_config_from_draft(draft: dict) -> GlobalDiscountConfig:
    return GlobalDiscountConfig(
        enabled=bool(draft["enabled"]),
        scope=draft["scope"],
        mode=draft["mode"],
        discount_type=draft["discount_type"],
        value=Decimal(str(draft["value"])),
        target_ids=frozenset(draft["target_ids"]),
    )


def _global_discount_editor_text(draft: dict, notice: str = "") -> str:
    status = "🟢 Включена" if draft["enabled"] else "🔴 Выключена"
    discount_type = "Процентная" if draft["discount_type"] == "PERCENT" else "Фиксированная"
    value = f"{draft['value']:g}%" if draft["discount_type"] == "PERCENT" else f"{draft['value']:.2f} ₽"
    mode = "Суммируется" if draft["mode"] == "STACK" else "Максимальная"
    scope = GLOBAL_DISCOUNT_SCOPE_LABELS[draft["scope"]]
    targets = "все товары" if draft["scope"] == "ALL" else f"выбрано: {len(draft['target_ids'])}"
    return (
        "🏷 <b>Настройка глобальной скидки</b>\n\n"
        f"<blockquote>• <b>Статус:</b> {status}\n"
        f"• <b>Тип:</b> {discount_type}\n"
        f"• <b>Скидка:</b> {value}\n"
        f"• <b>Режим:</b> {mode}\n"
        f"• <b>Влияние:</b> {scope} ({targets})</blockquote>\n"
        "Изменения вступят в силу только после нажатия «Принять»."
        + (f"\n\n{notice}" if notice else "")
    )


def _global_discount_editor_keyboard(draft: dict) -> InlineKeyboardMarkup:
    enabled_button = "🔴 Выключить" if draft["enabled"] else "🟢 Включить"
    percent_button = "🔘 Процентная" if draft["discount_type"] == "PERCENT" else "⚪ Процентная"
    fixed_button = "🔘 Фиксированная" if draft["discount_type"] == "FIXED" else "⚪ Фиксированная"
    value = f"{draft['value']:g}%" if draft["discount_type"] == "PERCENT" else f"{draft['value']:.2f} ₽"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=enabled_button, callback_data="admin_global_discount_toggle")],
        [
            InlineKeyboardButton(text="📌 Влияние", callback_data="admin_global_discount_scope"),
            InlineKeyboardButton(text="⚙️ Режим", callback_data="admin_global_discount_mode"),
        ],
        [
            InlineKeyboardButton(text=percent_button, callback_data="admin_global_discount_type_PERCENT"),
            InlineKeyboardButton(text=fixed_button, callback_data="admin_global_discount_type_FIXED"),
        ],
        [InlineKeyboardButton(text=f"🏷 Скидка: {value}", callback_data="admin_global_discount_value")],
        [
            InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_menu"),
            InlineKeyboardButton(text="✅ Принять", callback_data="admin_global_discount_accept"),
        ],
    ])


def _global_discount_percent_keyboard(draft: dict) -> InlineKeyboardMarkup:
    """Сетка быстрого выбора процентной скидки."""
    current = Decimal(str(draft["value"]))
    current_label = "🚫 Нет скидки" if current == ZERO else f"🏷 {current:g}%"
    rows = [[
        InlineKeyboardButton(
            text=f"{percent}%",
            callback_data=f"admin_global_discount_percent_{percent}",
        )
        for percent in range(start, start + 25, 5)
    ] for start in range(5, 81, 25)]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"[{current_label}]", callback_data="admin_global_discount_percent_zero")],
        *rows,
        [InlineKeyboardButton(text="✏️ Ручной ввод", callback_data="admin_global_discount_percent_manual")],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_global_discount_percent_cancel"),
            InlineKeyboardButton(text="✅ Принять", callback_data="admin_global_discount_percent_accept"),
        ],
    ])


async def _show_global_discount_editor(callback: CallbackQuery, state: FSMContext, notice: str = "") -> None:
    draft = _global_discount_draft(await state.get_data())
    await callback.message.edit_text(
        _global_discount_editor_text(draft, notice),
        reply_markup=_global_discount_editor_keyboard(draft),
        parse_mode="HTML",
    )


async def _show_global_discount_percent_editor(callback: CallbackQuery, state: FSMContext) -> None:
    draft = _global_discount_draft(await state.get_data())
    await callback.message.edit_text(
        "🏷 <b>Размер скидки</b>\n\nВыберите процент скидки:",
        reply_markup=_global_discount_percent_keyboard(draft),
        parse_mode="HTML",
    )


async def _show_global_discount_percent_editor_from_message(
    message: Message, state: FSMContext
) -> None:
    data = await state.get_data()
    draft = _global_discount_draft(data)
    await message.bot.edit_message_text(
        chat_id=data.get("global_discount_chat_id", message.chat.id),
        message_id=data.get("global_discount_message_id", message.message_id),
        text="🏷 <b>Размер скидки</b>\n\nВыберите процент скидки:",
        reply_markup=_global_discount_percent_keyboard(draft),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete global discount input", exc_info=True)


async def _show_global_discount_editor_from_message(
    message: Message, state: FSMContext, notice: str = ""
) -> None:
    data = await state.get_data()
    draft = _global_discount_draft(data)
    await message.bot.edit_message_text(
        chat_id=data.get("global_discount_chat_id", message.chat.id),
        message_id=data.get("global_discount_message_id", message.message_id),
        text=_global_discount_editor_text(draft, notice),
        reply_markup=_global_discount_editor_keyboard(draft),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete global discount input", exc_info=True)


@router.callback_query(F.data == "admin_global_discount")
async def admin_global_discount(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_global_discount_config(session)
    await state.clear()
    await state.update_data(
        global_discount_draft={
            "enabled": config.enabled,
            "scope": config.scope,
            "mode": config.mode,
            "discount_type": config.discount_type,
            "value": config.value,
            "target_ids": list(config.target_ids),
        },
        global_discount_chat_id=callback.message.chat.id,
        global_discount_message_id=callback.message.message_id,
    )
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_editor")
async def admin_global_discount_editor(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.set_state(None)
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_toggle")
async def admin_global_discount_toggle(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    draft["enabled"] = not draft["enabled"]
    await state.update_data(global_discount_draft=draft)
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_scope")
async def admin_global_discount_scope(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.set_state(None)
    draft = _global_discount_draft(await state.get_data())

    def scope_label(scope: str, icon: str, label: str) -> str:
        marker = "✅ " if draft["scope"] == scope else ""
        return f"{marker}{icon} {label}"

    await callback.message.edit_text(
        "📌 <b>Влияние скидки</b>\n\nВыберите, на что она распространяется.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=scope_label("ALL", "🌐", "Всё"),
                callback_data="admin_global_discount_scope_ALL",
            )],
            [InlineKeyboardButton(
                text=scope_label("CATEGORY", "📂", "Категории"),
                callback_data="admin_global_discount_scope_CATEGORY",
            )],
            [InlineKeyboardButton(
                text=scope_label("SUBCATEGORY", "🗂", "Подкатегории"),
                callback_data="admin_global_discount_scope_SUBCATEGORY",
            )],
            [InlineKeyboardButton(
                text=scope_label("GROUP", "📁", "Группы"),
                callback_data="admin_global_discount_scope_GROUP",
            )],
            [InlineKeyboardButton(
                text=scope_label("TYPE", "🏷", "Типы"),
                callback_data="admin_global_discount_scope_TYPE",
            )],
            [InlineKeyboardButton(
                text=scope_label("PRODUCT", "📦", "Товары"),
                callback_data="admin_global_discount_scope_PRODUCT",
            )],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_global_discount_editor")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_global_discount_scope_"))
async def admin_global_discount_scope_select(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    scope = callback.data.removeprefix("admin_global_discount_scope_")
    if scope not in SCOPES:
        await callback.answer("Некорректное влияние", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    # Возврат к уже открытому типу влияния не должен сбрасывать отмеченные
    # категории/товары. Выбор очищается только при реальной смене типа.
    if draft["scope"] != scope:
        draft["target_ids"] = []
    draft["scope"] = scope
    await state.update_data(global_discount_draft=draft)
    if scope == "ALL":
        await _show_global_discount_editor(callback, state)
    else:
        await _show_global_discount_targets(callback, state, session, scope)
    await callback.answer()


async def _show_global_discount_targets(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, scope: str
) -> None:
    draft = _global_discount_draft(await state.get_data())
    selected = set(draft["target_ids"])
    if scope in {"CATEGORY", "SUBCATEGORY", "GROUP", "TYPE"}:
        depth_by_scope = {"CATEGORY": 0, "SUBCATEGORY": 1, "GROUP": 2, "TYPE": 3}
        title_by_scope = {
            "CATEGORY": "категории", "SUBCATEGORY": "подкатегории",
            "GROUP": "группы", "TYPE": "типы",
        }
        nodes, _, depths = await _catalog_tree(session)
        items = [
            item for item in nodes
            if item.is_active and depths.get(item.id) == depth_by_scope[scope]
        ]
        title = title_by_scope[scope]
    else:
        result = await session.execute(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
        )
        items = result.scalars().all()
        title = "товары"

    if not items:
        detail = "Подходящих элементов пока нет."
        await callback.message.edit_text(
            f"📌 <b>Выбор: {title}</b>\n\n{detail}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_global_discount_scope")]
            ]),
            parse_mode="HTML",
        )
        return

    buttons = []
    for item in items[:80]:
        marker = "✅ " if item.id in selected else "☑️ "
        name = item.name[:46]
        buttons.append([InlineKeyboardButton(
            text=f"{marker}{name}",
            callback_data=f"admin_global_discount_target_{scope}_{item.id}",
        )])
    if len(items) > 80:
        buttons.append([InlineKeyboardButton(text="ℹ️ Показаны первые 80 элементов", callback_data="admin_global_discount_targets_info")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_global_discount_scope")])
    await callback.message.edit_text(
        f"📌 <b>Выберите {title}</b>\n\n"
        "Отмеченные позиции получат глобальную скидку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin_global_discount_target_"))
async def admin_global_discount_target_toggle(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    parts = callback.data.removeprefix("admin_global_discount_target_").rsplit("_", 1)
    if len(parts) != 2 or parts[0] not in SCOPES or not parts[1].isdigit():
        await callback.answer("Некорректный элемент", show_alert=True)
        return
    scope, target_id = parts[0], int(parts[1])
    draft = _global_discount_draft(await state.get_data())
    if draft["scope"] != scope:
        await callback.answer("Откройте выбор заново", show_alert=True)
        return
    selected = set(draft["target_ids"])
    if target_id in selected:
        selected.remove(target_id)
    else:
        selected.add(target_id)
    draft["target_ids"] = sorted(selected)
    await state.update_data(global_discount_draft=draft)
    await _show_global_discount_targets(callback, state, session, scope)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_targets_info")
async def admin_global_discount_targets_info(callback: CallbackQuery):
    await callback.answer("Для большого каталога настройте скидку по категориям.", show_alert=True)


@router.callback_query(F.data == "admin_global_discount_mode")
async def admin_global_discount_mode(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ <b>Режим скидки</b>\n\n"
        "Суммировать — складывает глобальную скидку со скидкой за объём.\n"
        "Максимальная — применяет только большую из них.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Суммировать", callback_data="admin_global_discount_mode_STACK")],
            [InlineKeyboardButton(text="🏆 Максимальная", callback_data="admin_global_discount_mode_MAXIMUM")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_global_discount_editor")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_global_discount_mode_"))
async def admin_global_discount_mode_select(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    mode = callback.data.removeprefix("admin_global_discount_mode_")
    if mode not in {"STACK", "MAXIMUM"}:
        await callback.answer("Некорректный режим", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    draft["mode"] = mode
    await state.update_data(global_discount_draft=draft)
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_global_discount_type_"))
async def admin_global_discount_type(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    discount_type = callback.data.removeprefix("admin_global_discount_type_")
    if discount_type not in {"PERCENT", "FIXED"}:
        await callback.answer("Некорректный тип", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    draft["discount_type"] = discount_type
    await state.update_data(global_discount_draft=draft)
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_value")
async def admin_global_discount_value(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    if draft["discount_type"] == "PERCENT":
        await state.update_data(global_discount_value_original=str(draft["value"]))
        await state.set_state(None)
        await _show_global_discount_percent_editor(callback, state)
        await callback.answer()
        return
    instruction = (
        "Отправьте размер скидки в рублях: число больше или равное 0."
    )
    await state.set_state(AdminStates.waiting_global_discount_value)
    await callback.message.edit_text(
        f"🏷 <b>Размер скидки</b>\n\n{instruction}",
        reply_markup=_input_cancel_keyboard("admin_global_discount_editor"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_global_discount_percent_\d+$"))
async def admin_global_discount_percent_select(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    percent = int(callback.data.rsplit("_", 1)[1])
    if percent == 100:
        await callback.answer(
            "Скидка 100% недоступна: бесплатные заказы отключены.",
            show_alert=True,
        )
        return
    if percent not in range(5, 101, 5):
        await callback.answer("Некорректный размер скидки", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    draft["value"] = Decimal(percent)
    await state.update_data(global_discount_draft=draft)
    await _show_global_discount_percent_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_percent_zero")
async def admin_global_discount_percent_zero(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    draft["value"] = ZERO
    await state.update_data(global_discount_draft=draft)
    await _show_global_discount_percent_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_percent_manual")
async def admin_global_discount_percent_manual(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_global_discount_value)
    await callback.message.edit_text(
        "✏️ <b>Ручной ввод скидки</b>\n\n"
        "Отправьте число от 0 до 99.99.",
        reply_markup=_input_cancel_keyboard("admin_global_discount_percent_back"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_percent_back")
async def admin_global_discount_percent_back(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.set_state(None)
    await _show_global_discount_percent_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_percent_cancel")
async def admin_global_discount_percent_cancel(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    draft = _global_discount_draft(data)
    draft["value"] = Decimal(str(data.get("global_discount_value_original", draft["value"])))
    await state.update_data(
        global_discount_draft=draft,
        global_discount_value_original=None,
    )
    await state.set_state(None)
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_global_discount_percent_accept")
async def admin_global_discount_percent_accept(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.update_data(global_discount_value_original=None)
    await state.set_state(None)
    await _show_global_discount_editor(callback, state)
    await callback.answer()


@router.message(AdminStates.waiting_global_discount_value)
async def admin_global_discount_value_input(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    draft = _global_discount_draft(await state.get_data())
    try:
        value = to_money(message.text or "", minimum=ZERO)
    except ValueError:
        value = Decimal("-1")
    maximum = Decimal("99.99") if draft["discount_type"] == "PERCENT" else None
    if value < 0 or (maximum is not None and value > maximum):
        data = await state.get_data()
        await edit_input_screen(
            message,
            state,
            "❌ Введите корректный размер скидки.",
            reply_markup=_input_cancel_keyboard("admin_global_discount_editor"),
            state_data=data,
        )
        return
    draft["value"] = value
    await state.update_data(global_discount_draft=draft)
    await state.set_state(None)
    if draft["discount_type"] == "PERCENT":
        await _show_global_discount_percent_editor_from_message(message, state)
    else:
        await _show_global_discount_editor_from_message(message, state)


@router.callback_query(F.data == "admin_global_discount_accept")
async def admin_global_discount_accept(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _global_discount_draft(await state.get_data())
    if draft["enabled"] and draft["value"] <= 0:
        await callback.answer("Укажите размер скидки больше 0", show_alert=True)
        return
    if draft["discount_type"] == "PERCENT" and draft["value"] >= Decimal("100"):
        await callback.answer("Процентная скидка должна быть меньше 100%", show_alert=True)
        return
    if draft["enabled"] and draft["scope"] != "ALL" and not draft["target_ids"]:
        await callback.answer("Выберите хотя бы одну позицию", show_alert=True)
        return
    await save_global_discount_config(session, _global_discount_config_from_draft(draft))
    await session.commit()
    await state.clear()
    await callback.message.edit_text(
        "✅ <b>Глобальная скидка сохранена.</b>\n\n"
        "Новые заказы будут рассчитаны по выбранным условиям.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏷 К скидке", callback_data="admin_global_discount")],
            [InlineKeyboardButton(text="◀️ В пункт управления", callback_data="admin_menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer("Скидка сохранена")


# ========== ПРОМОКОДЫ ==========

COUPON_DRAFT_DEFAULTS = {
    "name": "",
    "code": "",
    "discount_type": "PERCENT",
    "percent_mode": "ONE_TIME",
    "reward": 0.0,
    "valid_days": 0,
    "max_uses": 0,
    "max_uses_per_user": 0,
}


def _coupon_draft(data: dict) -> dict:
    return {**COUPON_DRAFT_DEFAULTS, **(data.get("coupon_draft") or {})}


def _coupon_type_label(draft: dict) -> str:
    if draft["discount_type"] == "FIXED":
        return "Фиксированная сумма на баланс"
    mode = "Одноразовая скидка" if draft["percent_mode"] == "ONE_TIME" else "Постоянная скидка"
    return f"Процентный — {mode}"


def _coupon_editor_text(draft: dict, notice: str = "") -> str:
    reward = f"{draft['reward']:g}%" if draft["discount_type"] == "PERCENT" else f"{draft['reward']:.2f} ₽"
    validity = "∞" if not draft["valid_days"] else f"{draft['valid_days']} дн."
    overall = "∞" if not draft["max_uses"] else str(draft["max_uses"])
    personal = "∞" if not draft["max_uses_per_user"] else str(draft["max_uses_per_user"])
    prefix = f"{notice}\n\n" if notice else ""
    return (
        f"{prefix}🎟 <b>Создание промокода</b>\n\n"
        f"• <b>Название:</b> {escape(draft['name']) if draft['name'] else 'не задано'}\n"
        f"• <b>Код:</b> <code>{escape(draft['code']) if draft['code'] else 'не задан'}</code>\n"
        f"• <b>Тип:</b> {_coupon_type_label(draft)}\n"
        f"• <b>Награда:</b> {reward}\n"
        f"• <b>Срок действия:</b> {validity}\n"
        f"• <b>Количество:</b> общий {overall}, личный {personal}\n\n"
        "Выберите пункт для изменения."
    )


def _coupon_editor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Название", callback_data="admin_coupon_edit_name"),
            InlineKeyboardButton(text="🏷 Код", callback_data="admin_coupon_edit_code"),
        ],
        [
            InlineKeyboardButton(text="🧩 Тип", callback_data="admin_coupon_edit_type"),
            InlineKeyboardButton(text="🎁 Награда", callback_data="admin_coupon_edit_reward"),
        ],
        [
            InlineKeyboardButton(text="⌛ Срок действия", callback_data="admin_coupon_edit_validity"),
            InlineKeyboardButton(text="🔢 Количество", callback_data="admin_coupon_edit_limits"),
        ],
        [
            InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_coupons"),
            InlineKeyboardButton(text="✅ Принять", callback_data="admin_coupon_accept"),
        ],
    ])


async def _show_coupon_editor(callback: CallbackQuery, state: FSMContext, notice: str = "") -> None:
    data = await state.get_data()
    draft = _coupon_draft(data)
    await callback.message.edit_text(
        _coupon_editor_text(draft, notice),
        reply_markup=_coupon_editor_keyboard(),
        parse_mode="HTML",
    )


async def _show_coupon_editor_from_message(message: Message, state: FSMContext, notice: str = "") -> None:
    data = await state.get_data()
    draft = _coupon_draft(data)
    await message.bot.edit_message_text(
        chat_id=data.get("coupon_chat_id", message.chat.id),
        message_id=data.get("coupon_message_id", message.message_id),
        text=_coupon_editor_text(draft, notice),
        reply_markup=_coupon_editor_keyboard(),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete coupon input", exc_info=True)


@router.callback_query(F.data == "admin_coupons")
async def admin_coupons(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🎟 <b>Промокоды</b>\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data="admin_coupon_create")],
            [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_coupons_list")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_create")
async def admin_coupon_create(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        coupon_draft=COUPON_DRAFT_DEFAULTS.copy(),
        coupon_chat_id=callback.message.chat.id,
        coupon_message_id=callback.message.message_id,
    )
    await _show_coupon_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_editor")
async def admin_coupon_editor(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.set_state(None)
    await _show_coupon_editor(callback, state)
    await callback.answer()


async def _start_coupon_input(callback: CallbackQuery, state: FSMContext, field: str, instruction: str) -> None:
    await state.update_data(coupon_edit_field=field)
    await state.set_state(AdminStates.waiting_coupon_draft_value)
    await callback.message.edit_text(
        f"🎟 <b>Создание промокода</b>\n\n{instruction}",
        reply_markup=_input_cancel_keyboard("admin_coupon_editor"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_coupon_edit_name")
async def admin_coupon_edit_name(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await _start_coupon_input(callback, state, "name", "Отправьте название промокода: от 1 до 255 символов.")
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_edit_code")
async def admin_coupon_edit_code(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await _start_coupon_input(
        callback, state, "code",
        "Отправьте код: 3–50 символов, латинские буквы, цифры, <code>_</code> или <code>-</code>.",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_edit_reward")
async def admin_coupon_edit_reward(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _coupon_draft(await state.get_data())
    instruction = (
        "Отправьте количество процентов: число от 0 до 99.99."
        if draft["discount_type"] == "PERCENT"
        else "Отправьте сумму в рублях, которая будет зачисляться на баланс: число больше 0."
    )
    await _start_coupon_input(callback, state, "reward", instruction)
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_edit_validity")
async def admin_coupon_edit_validity(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await _start_coupon_input(
        callback, state, "valid_days",
        "Отправьте срок действия в днях: <code>0</code> — бессрочно, любое положительное число — количество дней.",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_edit_limits")
async def admin_coupon_edit_limits(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _coupon_draft(await state.get_data())
    overall = "∞" if not draft["max_uses"] else str(draft["max_uses"])
    personal = "∞" if not draft["max_uses_per_user"] else str(draft["max_uses_per_user"])
    await state.set_state(None)
    await callback.message.edit_text(
        "🔢 <b>Количество активаций</b>\n\n"
        f"Общий лимит: <b>{overall}</b>\nЛичный лимит: <b>{personal}</b>\n\n"
        "Значение <code>0</code> означает безлимит.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Общий лимит", callback_data="admin_coupon_edit_limit_global")],
            [InlineKeyboardButton(text="👤 Личный лимит", callback_data="admin_coupon_edit_limit_personal")],
            [InlineKeyboardButton(text="◀️ К редактору", callback_data="admin_coupon_editor")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.in_({"admin_coupon_edit_limit_global", "admin_coupon_edit_limit_personal"}))
async def admin_coupon_edit_limit_value(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    field = "max_uses" if callback.data.endswith("global") else "max_uses_per_user"
    label = "общий" if field == "max_uses" else "личный"
    await _start_coupon_input(
        callback, state, field,
        f"Отправьте {label} лимит: <code>0</code> — безлимит, или положительное целое число.",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_edit_type")
async def admin_coupon_edit_type(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.set_state(None)
    await callback.message.edit_text(
        "🧩 <b>Тип награды</b>\n\nВыберите тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Процентный", callback_data="admin_coupon_type_percent")],
            [InlineKeyboardButton(text="💵 Фиксированный", callback_data="admin_coupon_type_fixed")],
            [InlineKeyboardButton(text="◀️ К редактору", callback_data="admin_coupon_editor")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_type_percent")
async def admin_coupon_type_percent(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _coupon_draft(await state.get_data())
    draft.update(discount_type="PERCENT", percent_mode="ONE_TIME")
    await state.update_data(coupon_draft=draft)
    await callback.message.edit_text(
        "📊 <b>Процентная награда</b>\n\nВыберите режим скидки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Одноразовая скидка", callback_data="admin_coupon_percent_mode_ONE_TIME")],
            [InlineKeyboardButton(text="Постоянная скидка", callback_data="admin_coupon_percent_mode_PERMANENT")],
            [InlineKeyboardButton(text="◀️ К редактору", callback_data="admin_coupon_editor")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_coupon_type_fixed")
async def admin_coupon_type_fixed(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _coupon_draft(await state.get_data())
    draft.update(discount_type="FIXED", percent_mode=None)
    await state.update_data(coupon_draft=draft)
    await _show_coupon_editor(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_coupon_percent_mode_"))
async def admin_coupon_percent_mode(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    mode = callback.data.removeprefix("admin_coupon_percent_mode_")
    if mode not in {"ONE_TIME", "PERMANENT"}:
        await callback.answer("Некорректный режим", show_alert=True)
        return
    draft = _coupon_draft(await state.get_data())
    draft.update(discount_type="PERCENT", percent_mode=mode)
    await state.update_data(coupon_draft=draft)
    await _show_coupon_editor(callback, state)
    await callback.answer()


@router.message(AdminStates.waiting_coupon_draft_value)
async def admin_coupon_draft_value(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    data = await state.get_data()
    field = data.get("coupon_edit_field")
    draft = _coupon_draft(data)
    raw_value = (message.text or "").strip()
    error = None
    if field == "name":
        if not 1 <= len(raw_value) <= 255:
            error = "Название должно содержать от 1 до 255 символов."
        else:
            draft[field] = raw_value
    elif field == "code":
        code = raw_value.upper()
        if not re.fullmatch(r"[A-Z0-9_-]{3,50}", code):
            error = "Код: 3–50 латинских букв, цифр, <code>_</code> или <code>-</code>."
        else:
            exists = await session.execute(select(Coupon.id).where(Coupon.code == code))
            if exists.scalar_one_or_none() is not None:
                error = "Такой промокод уже существует."
            else:
                draft[field] = code
    elif field == "reward":
        try:
            reward = to_money(raw_value, minimum=Decimal("0.01"))
        except ValueError:
            reward = ZERO
        maximum = Decimal("99.99") if draft["discount_type"] == "PERCENT" else Decimal("1000000000")
        if not 0 < reward <= maximum:
            error = "Награда должна быть числом от 0 до 99.99." if maximum == Decimal("99.99") else "Награда должна быть числом больше 0."
        else:
            draft[field] = reward
    elif field in {"valid_days", "max_uses", "max_uses_per_user"}:
        try:
            number = int(raw_value)
        except ValueError:
            number = -1
        if number < 0:
            error = "Введите 0 или положительное целое число."
        else:
            draft[field] = number
    else:
        error = "Не удалось определить редактируемое поле."

    if error:
        await message.bot.edit_message_text(
            chat_id=data.get("coupon_chat_id", message.chat.id),
            message_id=data.get("coupon_message_id", message.message_id),
            text=f"❌ {error}\n\nОтправьте значение ещё раз.",
            reply_markup=_input_cancel_keyboard("admin_coupon_editor"),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            logger.debug("Could not delete invalid coupon input", exc_info=True)
        return

    await state.update_data(coupon_draft=draft)
    await state.set_state(None)
    await _show_coupon_editor_from_message(message, state)


@router.callback_query(F.data == "admin_coupon_accept")
async def admin_coupon_accept(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    draft = _coupon_draft(await state.get_data())
    if not draft["name"] or not draft["code"] or draft["reward"] <= 0:
        await callback.answer("Заполните название, код и награду", show_alert=True)
        return
    if draft["discount_type"] == "PERCENT" and draft["reward"] >= Decimal("100"):
        await callback.answer("Процентная скидка должна быть меньше 100%", show_alert=True)
        return
    existing = await session.execute(select(Coupon.id).where(Coupon.code == draft["code"]))
    if existing.scalar_one_or_none() is not None:
        await callback.answer("Такой промокод уже существует", show_alert=True)
        return
    now = datetime.now()
    try:
        valid_until = now + timedelta(days=draft["valid_days"]) if draft["valid_days"] else None
    except OverflowError:
        await callback.answer("Срок действия слишком большой", show_alert=True)
        return
    coupon = Coupon(
        name=draft["name"],
        code=draft["code"],
        discount_type=draft["discount_type"],
        percent_mode=draft["percent_mode"] if draft["discount_type"] == "PERCENT" else None,
        discount_value=draft["reward"],
        max_uses=draft["max_uses"] or None,
        max_uses_per_user=draft["max_uses_per_user"] or None,
        valid_from=now,
        valid_until=valid_until,
        is_active=True,
    )
    session.add(coupon)
    await session.commit()
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Промокод {escape(coupon.name)} создан.</b>\n\n"
        f"Код: <code>{escape(coupon.code)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎟 К промокодам", callback_data="admin_coupons")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer("Промокод создан")


@router.callback_query(F.data == "admin_coupons_list")
async def admin_coupons_list(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    result = await session.execute(
        select(Coupon)
        .where(Coupon.is_active.is_(True))
        .order_by(Coupon.created_at.desc())
        .limit(50)
    )
    coupons = result.scalars().all()
    buttons = []
    if not coupons:
        text = "🎟 <b>Список промокодов</b>\n\nПромокоды пока не созданы."
    else:
        text = (
            "🎟 <b>Список промокодов</b>\n\n"
            "Нажмите на промокод, чтобы отключить его. История активаций и "
            "заказов сохраняется для корректного учёта."
        )
        for coupon in coupons:
            label = coupon.name or coupon.code
            buttons.append([InlineKeyboardButton(
                text=f"🗑 {label[:36]} — {coupon.code[:18]}",
                callback_data=f"admin_coupon_remove_{coupon.id}",
            )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_coupons")])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_coupon_remove_"))
async def admin_coupon_remove_confirm(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    try:
        coupon_id = int(callback.data.removeprefix("admin_coupon_remove_"))
    except ValueError:
        await callback.answer("Некорректный промокод", show_alert=True)
        return
    result = await session.execute(select(Coupon).where(Coupon.id == coupon_id))
    coupon = result.scalar_one_or_none()
    if coupon is None:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"Отключить промокод <b>{escape(coupon.name or coupon.code)}</b> "
        f"(<code>{escape(coupon.code)}</code>)?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔ Отключить", callback_data=f"admin_coupon_delete_{coupon.id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_coupons_list")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_coupon_delete_"))
async def admin_coupon_delete(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    try:
        coupon_id = int(callback.data.removeprefix("admin_coupon_delete_"))
    except ValueError:
        await callback.answer("Некорректный промокод", show_alert=True)
        return
    result = await session.execute(select(Coupon).where(Coupon.id == coupon_id).with_for_update())
    coupon = result.scalar_one_or_none()
    if coupon is None:
        await callback.answer("Промокод уже удалён", show_alert=True)
        return
    # Promocodes can already be referenced by CouponUsage, CouponActivation
    # and pending orders. A physical delete would either violate foreign keys
    # or erase the financial/audit trail, so "delete" in the UI means a safe
    # irreversible deactivation.
    coupon.is_active = False
    coupon.valid_until = datetime.now()
    await session.commit()
    await callback.message.edit_text(
        "✅ Промокод отключён. История его применений сохранена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 К списку промокодов", callback_data="admin_coupons_list")]
        ]),
    )
    await callback.answer()


# ========== РЕФЕРАЛЬНАЯ СИСТЕМА ==========

