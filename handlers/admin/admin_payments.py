"""Административные обработчики: payments."""
from .admin_context import *


def _interaction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Сообщество", callback_data="admin_interaction_community"),
            InlineKeyboardButton(text="💬 Поддержка", callback_data="admin_interaction_support"),
        ],
        [
            InlineKeyboardButton(text="📜 Соглашение", callback_data="admin_interaction_rules"),
            InlineKeyboardButton(text="❓ FAQ", callback_data="admin_interaction_faq"),
        ],
        [
            InlineKeyboardButton(text="🔒 Политика", callback_data="admin_interaction_privacy"),
            InlineKeyboardButton(text="👋 Приветствие", callback_data="setting_key_welcome_text"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
    ])


INTERACTION_META = {
    "community": {
        "title": "🌐 Сообщество",
        "source_label": "Указать сообщество",
        "description": "Ссылка на канал, группу, сайт или другой источник.",
    },
    "support": {
        "title": "💬 Поддержка",
        "source_label": "Указать поддержку",
        "description": "Ссылка на чат, бота, канал или страницу поддержки.",
    },
    "rules": {
        "title": "📜 Пользовательское соглашение",
        "source_label": "Назначить источник",
        "description": "Ссылка на опубликованное пользовательское соглашение.",
    },
    "privacy": {
        "title": "🔒 Политика конфиденциальности",
        "source_label": "Назначить источник",
        "description": "Ссылка на опубликованную политику конфиденциальности.",
    },
    "faq": {
        "title": "❓ FAQ",
        "source_label": "✏️ Ответить",
        "description": "Текст ответа, который увидят пользователи в разделе FAQ.",
    },
}


async def _interaction_text(session: AsyncSession, key: str, fallback: str) -> str:
    result = await session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting and setting.value else fallback


def _interaction_enabled(config, kind: str) -> bool:
    return bool(getattr(config, f"{kind}_enabled"))


def _interaction_source(config, kind: str) -> str:
    return str(getattr(config, f"{kind}_source", ""))


def _interaction_editor_keyboard(kind: str, enabled: bool) -> InlineKeyboardMarkup:
    meta = INTERACTION_META[kind]
    status_label = "🔴 Выключить" if enabled else "🟢 Включить"
    buttons = [
        [InlineKeyboardButton(
            text=status_label,
            callback_data=f"admin_interaction_toggle_{kind}",
        )],
        [InlineKeyboardButton(
            text=meta["source_label"],
            callback_data=f"admin_interaction_edit_{kind}",
        )],
    ]
    if kind == "support":
        buttons.append([InlineKeyboardButton(
            text="✏️ Контакт поддержки",
            callback_data="setting_key_support_chat",
        )])
    elif kind == "rules":
        buttons.append([InlineKeyboardButton(
            text="✏️ Текст соглашения",
            callback_data="setting_key_rules_text",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_interaction")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_interaction_editor(
    callback: CallbackQuery,
    session: AsyncSession,
    kind: str,
    notice: str = "",
) -> None:
    text, keyboard = await _interaction_editor_content(session, kind, notice)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


async def _interaction_editor_content(
    session: AsyncSession,
    kind: str,
    notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    config = await get_interaction_config(session)
    meta = INTERACTION_META[kind]
    enabled = _interaction_enabled(config, kind)
    status = "🟢 Включено" if enabled else "🔴 Выключено"
    if kind == "faq":
        value = await _interaction_text(session, "faq_text", FAQ_TEXT)
        preview = escape(value)
        if len(preview) > 900:
            preview = preview[:900] + "…"
        details = f"<b>Текущий ответ:</b>\n{preview}"
    else:
        source = _interaction_source(config, kind)
        details = f"<b>Текущий источник:</b>\n{escape(source) if source else 'не указан'}"

    text = (
        f"{meta['title']}\n\n"
        f"<b>Статус:</b> {status}\n"
        f"{meta['description']}\n\n"
        f"{details}"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    return text, _interaction_editor_keyboard(kind, enabled)


@router.callback_query(F.data == "admin_interaction")
async def admin_interaction(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🤝 <b>Взаимодействие</b>\n\nВыберите раздел.",
        reply_markup=_interaction_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.in_([
    "admin_interaction_community",
    "admin_interaction_support",
    "admin_interaction_rules",
    "admin_interaction_privacy",
    "admin_interaction_faq",
]))
async def admin_interaction_section(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    kind = callback.data.removeprefix("admin_interaction_")
    await _show_interaction_editor(callback, session, kind)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_interaction_toggle_"))
async def admin_interaction_toggle(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    kind = callback.data.removeprefix("admin_interaction_toggle_")
    if kind not in INTERACTION_META:
        await callback.answer("Некорректный раздел", show_alert=True)
        return
    config = await get_interaction_config(session)
    new_value = not _interaction_enabled(config, kind)
    await save_interaction_setting(session, INTERACTION_KEYS[kind]["enabled"], str(new_value).lower())
    await session.commit()
    await _show_interaction_editor(
        callback,
        session,
        kind,
        "✅ <b>Раздел включён.</b>" if new_value else "✅ <b>Раздел выключен.</b>",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_interaction_edit_"))
async def admin_interaction_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    kind = callback.data.removeprefix("admin_interaction_edit_")
    if kind not in INTERACTION_META:
        await callback.answer("Некорректный раздел", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        interaction_kind=kind,
        interaction_chat_id=callback.message.chat.id,
        interaction_message_id=callback.message.message_id,
    )
    await state.set_state(AdminStates.waiting_interaction_value)
    meta = INTERACTION_META[kind]
    instruction = (
        "Отправьте текст ответа для пользователей. Максимум 3500 символов."
        if kind == "faq"
        else "Отправьте ссылку: https://..., t.me/... или @username."
    )
    await callback.message.edit_text(
        f"{meta['title']}\n\n{instruction}",
        reply_markup=_input_cancel_keyboard(f"admin_interaction_{kind}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_interaction_value)
async def admin_interaction_value(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    data = await state.get_data()
    kind = data.get("interaction_kind")
    if kind not in INTERACTION_META:
        await state.clear()
        return
    value = (message.text or "").strip()
    error = ""
    if kind == "faq":
        if not value or len(value) > 3500:
            error = "Ответ FAQ должен содержать от 1 до 3500 символов."
        else:
            setting_key = "faq_text"
    else:
        try:
            value = normalize_external_source(value)
            setting_key = INTERACTION_KEYS[kind]["source"]
        except ValueError as exc:
            error = str(exc)

    chat_id = data.get("interaction_chat_id", message.chat.id)
    message_id = data.get("interaction_message_id")
    if not error:
        if kind in {"community", "support", "rules", "privacy"}:
            try:
                save_interaction_source_to_env(kind, value)
            except (OSError, RuntimeError, ValueError):
                error = "Не удалось обновить .env. Настройка не сохранена."
                logger.exception("Could not update interaction source in .env")
        if not error:
            await save_interaction_setting(session, setting_key, value)
            await session.commit()
            await state.clear()

    if error:
        await message.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ <b>{escape(error)}</b>\n\nПопробуйте ещё раз.",
            reply_markup=_input_cancel_keyboard(f"admin_interaction_{kind}"),
            parse_mode="HTML",
        )
    else:
        try:
            text, keyboard = await _interaction_editor_content(
                session, kind, "✅ <b>Настройка сохранена.</b>"
            )
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not update interaction setting message")
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete interaction input", exc_info=True)


@router.callback_query(F.data == "admin_payment_settings")
async def admin_payment_settings_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Показать категории способов оплаты."""
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        "💳 <b>Платёжные системы</b>\n\n"
        "Выберите категорию оплаты.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="admin_payment_stars")],
            [InlineKeyboardButton(text="💳 Банковские карты и СБП", callback_data="admin_payment_cards")],
            [InlineKeyboardButton(text="₿ Криптовалюта", callback_data="admin_payment_crypto")],
            [
                InlineKeyboardButton(text="💱 Курс", callback_data="admin_payment_rates"),
                InlineKeyboardButton(text="↕️ Позиционирование", callback_data="admin_payment_position"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


PAYMENT_RATE_FIELDS = {
    "usd": ("PAYMENT_USD_RATE", "💵 Доллар США (USD)"),
    "eur": ("PAYMENT_EUR_RATE", "💶 Евро (EUR)"),
    "stars": ("PAYMENT_STARS_RATE", "⭐ Telegram Stars"),
}


def _format_payment_rate(value: Decimal | int | str) -> str:
    """Показать цену валюты в рублях без лишних нулей."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return "не задан"
    if amount <= 0:
        return "не задан"
    return f"{amount:.2f}".replace(".00", "")


def _payment_rates_keyboard() -> InlineKeyboardMarkup:
    enabled = bool(settings.PAYMENT_RATE_SYNC_ENABLED)
    sync_label = "🟢 Синхронизировать" if enabled else "🔴 Синхронизировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sync_label, callback_data="admin_payment_rate_toggle_sync")],
        [
            InlineKeyboardButton(
                text=f"💵 $ — {_format_payment_rate(settings.PAYMENT_USD_RATE)} ₽",
                callback_data="admin_payment_rate_edit_usd",
            ),
            InlineKeyboardButton(
                text=f"💶 € — {_format_payment_rate(settings.PAYMENT_EUR_RATE)} ₽",
                callback_data="admin_payment_rate_edit_eur",
            ),
        ],
        [InlineKeyboardButton(
            text=f"⭐ Telegram Stars — {_format_payment_rate(settings.PAYMENT_STARS_RATE)} ₽",
            callback_data="admin_payment_rate_edit_stars",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_payment_settings")],
    ])


def _payment_rates_text(notice: str = "") -> str:
    synced = bool(settings.PAYMENT_RATE_SYNC_ENABLED)
    status = "🟢 включена" if synced else "🔴 выключена"
    mode_text = (
        "USD и EUR обновляются по курсу ЦБ, Stars рассчитываются как 0,015 USD."
        if synced
        else "Курсы можно установить вручную, нажав нужную валюту."
    )
    text = (
        "💱 <b>Курс</b>\n\n"
        f"Синхронизация: <b>{status}</b>\n"
        f"{mode_text}\n\n"
        f"💵 USD: <b>{_format_payment_rate(settings.PAYMENT_USD_RATE)} ₽</b>\n"
        f"💶 EUR: <b>{_format_payment_rate(settings.PAYMENT_EUR_RATE)} ₽</b>\n"
        f"⭐ Telegram Stars: <b>{_format_payment_rate(settings.PAYMENT_STARS_RATE)} ₽</b> за 1 Star\n\n"
        "Цена Stars применяется только к новым счетам. Уже выставленный счёт "
        "сохраняет исходное число Stars."
    )
    return f"{notice}\n\n{text}" if notice else text


async def _show_payment_rates(callback: CallbackQuery, notice: str = "") -> None:
    await callback.message.edit_text(
        _payment_rates_text(notice),
        reply_markup=_payment_rates_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_payment_rates")
async def admin_payment_rates(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await _show_payment_rates(callback)
    await callback.answer()


@router.callback_query(F.data == "admin_payment_rate_toggle_sync")
async def admin_payment_rate_toggle_sync(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    if settings.PAYMENT_RATE_SYNC_ENABLED:
        await save_payment_setting(session, "PAYMENT_RATE_SYNC_ENABLED", "false")
        await session.commit()
        await _show_payment_rates(callback, "✅ <b>Синхронизация выключена.</b>")
        await callback.answer()
        return

    try:
        rates = await synchronize_payment_rates(session)
        await save_payment_setting(session, "PAYMENT_RATE_SYNC_ENABLED", "true")
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("Manual payment-rate synchronization failed", exc_info=True)
        await callback.answer("Не удалось получить курс ЦБ. Попробуйте позже.", show_alert=True)
        return
    await _show_payment_rates(
        callback,
        "✅ <b>Синхронизация включена.</b> "
        f"USD: {rates['USD']:.2f} ₽, EUR: {rates['EUR']:.2f} ₽, "
        f"Stars: {rates['STARS']:.2f} ₽.",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_payment_rate_edit_"))
async def admin_payment_rate_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    code = callback.data.removeprefix("admin_payment_rate_edit_")
    field_meta = PAYMENT_RATE_FIELDS.get(code)
    if field_meta is None:
        await callback.answer("Неизвестная валюта", show_alert=True)
        return
    if settings.PAYMENT_RATE_SYNC_ENABLED:
        await callback.answer("Сначала выключите синхронизацию.", show_alert=True)
        return
    field, title = field_meta
    current = _format_payment_rate(getattr(settings, field))
    await state.clear()
    await state.update_data(
        payment_rate_field=field,
        payment_rate_title=title,
        payment_rate_chat_id=callback.message.chat.id,
        payment_rate_message_id=callback.message.message_id,
    )
    await state.set_state(AdminStates.waiting_payment_rate_value)
    await callback.message.edit_text(
        f"💱 <b>{title}</b>\n\n"
        f"Текущее значение: <b>{current} ₽</b>\n\n"
        "Отправьте новую цену в рублях за 1 единицу.",
        reply_markup=_input_cancel_keyboard("admin_payment_rates"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_payment_rate_value)
async def admin_payment_rate_value(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(message.from_user.id, session):
        await state.clear()
        return
    data = await state.get_data()
    field = data.get("payment_rate_field")
    title = data.get("payment_rate_title", "Курс")
    chat_id = data.get("payment_rate_chat_id", message.chat.id)
    message_id = data.get("payment_rate_message_id")
    try:
        value = Decimal((message.text or "").strip().replace(",", "."))
        value = value.quantize(Decimal("0.000001"))
        if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
            raise ValueError
    except (InvalidOperation, ValueError):
        error = "Введите цену больше 0 и не больше 1 000 000 ₽."
        if message_id:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ <b>{error}</b>\n\n💱 <b>{escape(str(title))}</b>",
                reply_markup=_input_cancel_keyboard("admin_payment_rates"),
                parse_mode="HTML",
            )
        try:
            await message.delete()
        except Exception:
            logger.debug("Could not delete invalid payment rate input", exc_info=True)
        return

    allowed_fields = {item[0] for item in PAYMENT_RATE_FIELDS.values()}
    if field not in allowed_fields:
        await state.clear()
        return
    await save_payment_setting(session, field, format(value, "f"))
    await session.commit()
    await state.clear()
    if message_id:
        await message.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_payment_rates_text("✅ <b>Курс сохранён.</b>"),
            reply_markup=_payment_rates_keyboard(),
            parse_mode="HTML",
        )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete payment rate input", exc_info=True)


def _positioned_payment_methods() -> list[str]:
    """Показывать только реально доступные покупателю способы оплаты."""
    from utils.payments import PaymentService

    return [
        method for method in get_payment_method_order()
        if PaymentService.provider_enabled(method)
    ]


def _payment_position_keyboard(order: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=PAYMENT_METHOD_TITLES[method],
                callback_data="admin_payment_position_noop",
            ),
            InlineKeyboardButton(
                text="🔼",
                callback_data=f"admin_payment_position_up_{method}",
            ),
        ]
        for method in order
    ]
    buttons.append([
        InlineKeyboardButton(text="⛔ Отмена", callback_data="admin_payment_position_cancel"),
        InlineKeyboardButton(text="✅ Принять", callback_data="admin_payment_position_accept"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_payment_position(callback: CallbackQuery, order: list[str]) -> None:
    await callback.message.edit_text(
        "🔢 <b>Изменить позиционирование</b>\n\n"
        "Нажмите 🔼 рядом со способом оплаты, чтобы поднять его выше. "
        "Изменения вступят в силу после нажатия «Принять».",
        reply_markup=_payment_position_keyboard(order),
        parse_mode="HTML",
    )


async def _delete_message_after(message: Message, delay: int = 5) -> None:
    """Удалить короткое служебное уведомление, не оставляя его в диалоге."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete temporary admin notification", exc_info=True)


@router.callback_query(F.data == "admin_payment_position")
async def admin_payment_position(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    order = _positioned_payment_methods()
    if not order:
        await callback.answer("Нет подключённых платёжных систем", show_alert=True)
        return
    await state.clear()
    await state.update_data(payment_position_order=order)
    await _show_payment_position(callback, order)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_payment_position_up_"))
async def admin_payment_position_up(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    method = callback.data.removeprefix("admin_payment_position_up_")
    data = await state.get_data()
    order = data.get("payment_position_order")
    if not isinstance(order, list) or method not in order:
        await callback.answer("Откройте позиционирование заново", show_alert=True)
        return
    index = order.index(method)
    if index == 0:
        await callback.answer("Этот способ уже сверху")
        return
    order[index - 1], order[index] = order[index], order[index - 1]
    await state.update_data(payment_position_order=order)
    await _show_payment_position(callback, order)
    await callback.answer()


@router.callback_query(F.data == "admin_payment_position_accept")
async def admin_payment_position_accept(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    draft = data.get("payment_position_order")
    active_methods = _positioned_payment_methods()
    if not isinstance(draft, list) or set(draft) != set(active_methods):
        await state.clear()
        await callback.answer("Список оплат изменился. Откройте раздел заново.", show_alert=True)
        return
    iterator = iter(draft)
    full_order = [
        next(iterator) if method in active_methods else method
        for method in get_payment_method_order()
    ]
    await save_payment_setting(session, "PAYMENT_METHOD_ORDER", ",".join(full_order))
    await session.commit()
    await state.clear()
    await admin_payment_settings_menu(callback, state, session)
    notification = await callback.message.answer("✅ Позиции сохранены.")
    asyncio.create_task(_delete_message_after(notification))


@router.callback_query(F.data == "admin_payment_position_cancel")
async def admin_payment_position_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await admin_payment_settings_menu(callback, state, session)


@router.callback_query(F.data == "admin_payment_position_noop")
async def admin_payment_position_noop(callback: CallbackQuery, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await callback.answer()


async def show_payment_providers(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, section: str
) -> None:
    """Показать провайдеров одной платёжной категории."""
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    config = PAYMENT_SECTIONS[section]
    await state.clear()
    from utils.payments import PaymentService

    buttons = []
    for provider in config["providers"]:
        provider_config = PAYMENT_PROVIDERS[provider]
        status = "✅ подключена" if PaymentService.provider_enabled(provider) else "⚪ не настроена"
        buttons.append([
            InlineKeyboardButton(
                text=f"{provider_config['title']} — {status}",
                callback_data=f"admin_payment_provider_{provider}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_payment_settings")])
    await callback.message.edit_text(
        f"💳 <b>{config['title']}</b>\n\n"
        "Выберите систему, чтобы указать или изменить её реквизиты.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_payment_cards")
async def admin_payment_cards(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await show_payment_providers(callback, state, session, "cards")


@router.callback_query(F.data == "admin_payment_crypto")
async def admin_payment_crypto(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await show_payment_providers(callback, state, session, "crypto")


@router.callback_query(F.data == "admin_payment_stars")
async def admin_payment_stars(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Настройки стандартной оплаты Telegram Stars."""
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    enabled = bool(settings.PAYMENT_STARS_ENABLED)
    await callback.message.edit_text(
        "⭐ <b>Telegram Stars</b>\n\n"
        f"Статус: <b>{'включена' if enabled else 'отключена'}</b>\n\n"
        "Это стандартная оплата Telegram и не требует ключей. При отключении "
        "Stars не показываются покупателям и новые счета не создаются.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔴 Отключить" if enabled else "🟢 Включить",
                callback_data="admin_payment_toggle_stars",
            )],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_payment_settings")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_payment_toggle_stars")
async def admin_payment_toggle_stars(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    enabled = not bool(settings.PAYMENT_STARS_ENABLED)
    try:
        save_payment_enabled_to_env("PAYMENT_STARS_ENABLED", enabled)
    except (OSError, RuntimeError, ValueError):
        # DB persistence is authoritative for container deployments where the
        # host .env is intentionally not mounted writable.
        logger.warning("Could not update Telegram Stars flag in .env", exc_info=True)
    await save_payment_enabled_setting(session, "PAYMENT_STARS_ENABLED", enabled)
    await session.commit()
    await admin_payment_stars(callback, state, session)


async def show_payment_provider(callback: CallbackQuery, state: FSMContext, provider: str) -> None:
    """Показать карточку платёжной системы без изменения callback Telegram."""
    config = PAYMENT_PROVIDERS.get(provider)
    if config is None:
        await callback.answer("Неизвестная платёжная система", show_alert=True)
        return

    section = "cards" if provider in PAYMENT_SECTIONS["cards"]["providers"] else "crypto"
    enabled_field = f"PAYMENT_{provider.upper()}_ENABLED"
    enabled = bool(getattr(settings, enabled_field, True))
    await state.clear()
    await callback.message.edit_text(
        f"💳 <b>{config['title']}</b>\n\n"
        f"Статус: <b>{'включена' if enabled else 'отключена'}</b>\n\n"
        "Отключённая система не показывается покупателям и не создаёт новые счета.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Отключить" if enabled else "🟢 Включить", callback_data=f"admin_payment_toggle_{provider}")],
            [InlineKeyboardButton(text="✏️ Реквизиты", callback_data=f"admin_payment_setup_{provider}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=PAYMENT_SECTIONS[section]["back_callback"])],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


def payment_credentials_text(provider: str) -> str:
    """Подготовить безопасное для HTML представление текущих реквизитов."""
    config = PAYMENT_PROVIDERS[provider]
    lines = []
    for field, label in config["fields"]:
        value = str(getattr(settings, field, "") or "").strip()
        if not value:
            shown = "<i>не указано</i>"
        elif any(marker in field for marker in ("SECRET", "TOKEN", "API_KEY")):
            shown = f"<code>{escape(value[:4] + '…' + value[-4:])}</code>"
        else:
            shown = f"<code>{escape(value)}</code>"
        lines.append(f"• <b>{escape(label)}:</b> {shown}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("admin_payment_provider_"))
async def admin_payment_provider_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Открыть карточку выбранной платёжной системы."""
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    provider = callback.data.removeprefix("admin_payment_provider_")
    await show_payment_provider(callback, state, provider)


@router.callback_query(F.data.startswith("admin_payment_toggle_"))
async def admin_payment_toggle(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    provider = callback.data.removeprefix("admin_payment_toggle_")
    if provider not in PAYMENT_PROVIDERS:
        await callback.answer("Неизвестная система", show_alert=True)
        return
    field = f"PAYMENT_{provider.upper()}_ENABLED"
    enabled = not bool(getattr(settings, field, False))
    try:
        save_payment_enabled_to_env(field, enabled)
    except (OSError, RuntimeError, ValueError):
        logger.warning("Could not update %s in .env", field, exc_info=True)
    await save_payment_enabled_setting(session, field, enabled)
    await session.commit()
    await show_payment_provider(callback, state, provider)


@router.callback_query(F.data.startswith("admin_payment_setup_"))
async def admin_payment_provider_setup(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Показать сохранённые в .env или БД реквизиты выбранной системы."""
    provider = callback.data.removeprefix("admin_payment_setup_")
    config = PAYMENT_PROVIDERS.get(provider)
    if config is None or not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    section = "cards" if provider in PAYMENT_SECTIONS["cards"]["providers"] else "crypto"
    await state.clear()
    await callback.message.edit_text(
        f"💳 <b>{config['title']}: реквизиты</b>\n\n"
        f"{payment_credentials_text(provider)}\n\n"
        "Секретные значения показаны частично для безопасности.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить реквизиты", callback_data=f"admin_payment_edit_{provider}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_payment_provider_{provider}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_payment_edit_"))
async def admin_payment_provider_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать пошаговое изменение реквизитов выбранного провайдера."""
    provider = callback.data.removeprefix("admin_payment_edit_")
    config = PAYMENT_PROVIDERS.get(provider)
    if config is None or not await is_developer_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    section = "cards" if provider in PAYMENT_SECTIONS["cards"]["providers"] else "crypto"
    _, label = config["fields"][0]
    await state.clear()
    await state.update_data(
        payment_provider=provider,
        payment_field_index=0,
        payment_back_callback=PAYMENT_SECTIONS[section]["back_callback"],
        _screen_chat_id=callback.message.chat.id,
        _screen_message_id=callback.message.message_id,
    )
    await state.set_state(AdminStates.waiting_payment_setting_value)
    await callback.message.edit_text(
        f"💳 <b>{config['title']}</b>\n\n"
        f"Введите: <b>{label}</b>\n"
        f"Поле 1 из {len(config['fields'])}.",
        reply_markup=_input_cancel_keyboard(PAYMENT_SECTIONS[section]["back_callback"]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_payment_setting_value)
async def admin_payment_setting_value(message: Message, state: FSMContext, session: AsyncSession):
    """Сохранить очередной реквизит платёжной системы."""
    if not await is_developer_async(message.from_user.id, session):
        await state.clear()
        await message.answer("❌ Доступ запрещен")
        return

    value = (message.text or "").strip()
    data = await state.get_data()
    provider = data.get("payment_provider")
    index = data.get("payment_field_index")
    back_callback = data.get("payment_back_callback", "admin_payment_settings")
    config = PAYMENT_PROVIDERS.get(provider)
    if not value or config is None or not isinstance(index, int) or index >= len(config["fields"]):
        await state.clear()
        await message.answer("❌ Не удалось сохранить реквизит. Откройте раздел заново.")
        return

    field, _ = config["fields"][index]
    await save_payment_setting(session, field, value)
    await session.commit()

    next_index = index + 1
    if next_index < len(config["fields"]):
        _, next_label = config["fields"][next_index]
        await state.update_data(payment_field_index=next_index)
        await edit_input_screen(
            message,
            state,
            f"✅ Сохранено. Введите: <b>{next_label}</b>\n"
            f"Поле {next_index + 1} из {len(config['fields'])}.",
            reply_markup=_input_cancel_keyboard(back_callback),
            parse_mode="HTML",
            state_data=data,
        )
        return

    await edit_input_screen(
        message,
        state,
        f"✅ Реквизиты {config['title']} сохранены. Система станет доступна сразу, если все поля заполнены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К разделу", callback_data=back_callback)]
        ]),
        state_data=data,
    )
    await state.clear()


# ========== ГЛОБАЛЬНАЯ СКИДКА ==========

GLOBAL_DISCOUNT_DRAFT_DEFAULTS = {
    "enabled": False,
    "scope": "ALL",
    "mode": "MAXIMUM",
    "discount_type": "PERCENT",
    "value": ZERO,
    "target_ids": [],
}

GLOBAL_DISCOUNT_SCOPE_LABELS = {
    "ALL": "Всё",
    "CATEGORY": "Категории",
    "SUBCATEGORY": "Подкатегории",
    "GROUP": "Группы",
    "TYPE": "Типы",
    "PRODUCT": "Товары",
}


