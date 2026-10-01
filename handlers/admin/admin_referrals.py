"""Административные обработчики: referrals."""
from .admin_context import *


def referral_settings_keyboard(config) -> InlineKeyboardMarkup:
    cashback_status = (
        f"включён — {config.cashback_percent:g}% от награды"
        if config.cashback_enabled
        else "выключен"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Условия: {config.condition_label}", callback_data="admin_referral_condition")],
        [
            InlineKeyboardButton(text=f"🧬 Уровень: {config.levels}", callback_data="admin_referral_levels"),
            InlineKeyboardButton(text=f"🎁 Награда: {config.reward_percent:g}%", callback_data="admin_referral_reward"),
        ],
        [InlineKeyboardButton(text=f"💸 Кешбек: {cashback_status}", callback_data="admin_referral_cashback")],
        [InlineKeyboardButton(text="✉️ Настройка приглашения", callback_data="admin_referral_invitation")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
    ])


async def show_referral_settings(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    config = await get_referral_program_config(session)
    cashback_text = (
        f"включён, {config.cashback_percent:g}% от награды реферера"
        if config.cashback_enabled
        else "выключен"
    )
    await callback.message.edit_text(
        "👥 <b>Реферальная система</b>\n\n"
        f"<b>Уровней:</b> {config.levels}\n"
        f"<b>Начисление:</b> {config.condition_label}\n"
        f"<b>Награда рефереру:</b> {config.reward_percent:g}% от суммы заказа\n"
        f"<b>Кешбек рефералу:</b> {cashback_text}\n\n"
        "При двух уровнях пользователь получает награду как за своих друзей, "
        "так и за друзей, которых пригласили эти друзья.",
        reply_markup=referral_settings_keyboard(config),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_referral_settings")
async def admin_referral_settings(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await show_referral_settings(callback, state, session)
    await callback.answer()


@router.callback_query(F.data == "admin_referral_levels")
async def admin_referral_levels(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    await callback.message.edit_text(
        "🧬 <b>Количество уровней</b>\n\n"
        "<b>1 уровень</b> — вы получаете награду только за покупки друзей, "
        "которых пригласили сами.\n"
        "<b>2 уровня</b> — вы получаете награду за покупки своих друзей "
        "и за покупки пользователей, которых пригласили ваши друзья.\n\n"
        f"Сейчас: <b>{config.levels}</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ 1 уровень" if config.levels == 1 else "1 уровень", callback_data="admin_referral_set_levels_1")],
            [InlineKeyboardButton(text="✅ 2 уровня" if config.levels == 2 else "2 уровня", callback_data="admin_referral_set_levels_2")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral_settings")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_referral_set_levels_"))
async def admin_referral_set_levels(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    value = callback.data.rsplit("_", 1)[-1]
    if value not in {"1", "2"}:
        await callback.answer("Некорректный уровень", show_alert=True)
        return
    await save_referral_setting(session, REFERRAL_LEVELS_KEY, value)
    await session.commit()
    await show_referral_settings(callback, state, session)
    await callback.answer("Сохранено")


@router.callback_query(F.data == "admin_referral_condition")
async def admin_referral_condition(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    await callback.message.edit_text(
        "💳 <b>Условия начисления</b>\n\n"
        "<b>Первый платёж</b> — награда только за первый оплаченный заказ реферала.\n"
        "<b>Каждый платёж</b> — награда за каждый оплаченный заказ реферала.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Первый платёж" if config.condition == "first" else "Первый платёж", callback_data="admin_referral_set_condition_first")],
            [InlineKeyboardButton(text="✅ Каждый платёж" if config.condition == "every" else "Каждый платёж", callback_data="admin_referral_set_condition_every")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral_settings")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_referral_set_condition_"))
async def admin_referral_set_condition(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    value = callback.data.removeprefix("admin_referral_set_condition_")
    if value not in {"first", "every"}:
        await callback.answer("Некорректное условие", show_alert=True)
        return
    await save_referral_setting(session, REFERRAL_CONDITION_KEY, value)
    await session.commit()
    await show_referral_settings(callback, state, session)
    await callback.answer("Сохранено")


async def start_referral_number_input(
    callback: CallbackQuery, state: FSMContext, state_name: State, title: str, text: str
) -> None:
    await state.clear()
    await state.update_data(
        referral_settings_chat_id=callback.message.chat.id,
        referral_settings_message_id=callback.message.message_id,
    )
    await state.set_state(state_name)
    await callback.message.edit_text(
        f"{title}\n\n{text}\n\nОтправьте число от 0 до 100.",
        reply_markup=_input_cancel_keyboard("admin_referral_settings"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_referral_reward")
async def admin_referral_reward(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    await start_referral_number_input(
        callback, state, AdminStates.waiting_referral_reward, "🎁 <b>Награда рефереру</b>",
        f"Сейчас: <b>{config.reward_percent:g}%</b> от суммы оплаченного заказа.",
    )
    await callback.answer()


async def save_referral_number_input(
    message: Message, state: FSMContext, session: AsyncSession, key: str, label: str
) -> None:
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    try:
        value = to_money((message.text or "").strip(), minimum=ZERO, maximum=Decimal("100"))
    except ValueError:
        value = Decimal("-1")
    if not 0 <= value <= Decimal("100"):
        data = await state.get_data()
        await message.bot.edit_message_text(
            chat_id=data.get("referral_settings_chat_id", message.chat.id),
            message_id=data.get("referral_settings_message_id", message.message_id),
            text=f"❌ <b>{label}</b>: введите число от 0 до 100.",
            reply_markup=_input_cancel_keyboard("admin_referral_settings"),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            logger.debug("Could not delete invalid referral number", exc_info=True)
        return
    data = await state.get_data()
    await save_referral_setting(session, key, format(value, "f"))
    await session.commit()
    await state.clear()
    config = await get_referral_program_config(session)
    await message.bot.edit_message_text(
        chat_id=data.get("referral_settings_chat_id", message.chat.id),
        message_id=data.get("referral_settings_message_id", message.message_id),
        text=f"✅ <b>{label}</b> сохранена.\n\n"
        f"Текущее значение: <b>{value:g}%</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К реферальной системе", callback_data="admin_referral_settings")]
        ]),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete referral setting input", exc_info=True)


@router.message(AdminStates.waiting_referral_reward)
async def admin_referral_reward_save(message: Message, state: FSMContext, session: AsyncSession):
    await save_referral_number_input(message, state, session, REFERRAL_REWARD_PERCENT_KEY, "Награда")


@router.callback_query(F.data == "admin_referral_cashback")
async def admin_referral_cashback(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    status = "включён" if config.cashback_enabled else "выключен"
    await callback.message.edit_text(
        "💸 <b>Кешбек рефералу</b>\n\n"
        "Кешбек получает приглашённый пользователь после покупки или продления. "
        "Он считается от награды реферера: при награде 10% и кешбеке 50% "
        "реферал получит 5% от суммы заказа.\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Размер: <b>{config.cashback_percent:g}%</b> от награды реферера.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Отключить" if config.cashback_enabled else "🟢 Включить", callback_data="admin_referral_cashback_toggle")],
            [InlineKeyboardButton(text="✏️ Изменить процент", callback_data="admin_referral_cashback_percent")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral_settings")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_referral_cashback_toggle")
async def admin_referral_cashback_toggle(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    await save_referral_setting(
        session, REFERRAL_CASHBACK_ENABLED_KEY, "false" if config.cashback_enabled else "true"
    )
    await session.commit()
    await admin_referral_cashback(callback, session)


@router.callback_query(F.data == "admin_referral_cashback_percent")
async def admin_referral_cashback_percent(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    await start_referral_number_input(
        callback, state, AdminStates.waiting_referral_cashback_percent,
        "💸 <b>Размер кешбека</b>",
        f"Сейчас: <b>{config.cashback_percent:g}%</b> от награды реферера.",
    )
    await callback.answer()


@router.message(AdminStates.waiting_referral_cashback_percent)
async def admin_referral_cashback_percent_save(message: Message, state: FSMContext, session: AsyncSession):
    await save_referral_number_input(message, state, session, REFERRAL_CASHBACK_PERCENT_KEY, "Размер кешбека")


@router.callback_query(F.data == "admin_referral_invitation")
async def admin_referral_invitation(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    config = await get_referral_program_config(session)
    preview = escape(config.invitation_text)
    if len(preview) > 800:
        preview = preview[:800] + "…"
    await callback.message.edit_text(
        "✉️ <b>Настройка приглашения</b>\n\n"
        "Этот текст будет подставлен в кнопку «Пригласить». Персональная ссылка "
        "добавляется Telegram автоматически, поэтому писать её в тексте не нужно.\n\n"
        f"<b>Текущий текст:</b>\n{preview}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактировать приглашение", callback_data="admin_referral_invitation_edit")],
            [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="admin_referral_invitation_preview")],
            [InlineKeyboardButton(text="↩️ Сбросить по умолчанию", callback_data="admin_referral_invitation_reset")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral_settings")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_referral_invitation_edit")
async def admin_referral_invitation_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        referral_settings_chat_id=callback.message.chat.id,
        referral_settings_message_id=callback.message.message_id,
    )
    await state.set_state(AdminStates.waiting_referral_invitation_text)
    await callback.message.edit_text(
        "✏️ <b>Текст приглашения</b>\n\n"
        "Отправьте новый текст. Ссылку добавлять не нужно: Telegram прикрепит "
        "персональную ссылку сам. Максимум 3500 символов.",
        reply_markup=_input_cancel_keyboard("admin_referral_invitation"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_referral_invitation_text)
async def admin_referral_invitation_save(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value or len(value) > 3500:
        data = await state.get_data()
        await message.bot.edit_message_text(
            chat_id=data.get("referral_settings_chat_id", message.chat.id),
            message_id=data.get("referral_settings_message_id", message.message_id),
            text="❌ <b>Текст приглашения</b> должен содержать от 1 до 3500 символов.",
            reply_markup=_input_cancel_keyboard("admin_referral_invitation"),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            logger.debug("Could not delete invalid invitation input", exc_info=True)
        return
    data = await state.get_data()
    await save_referral_setting(session, REFERRAL_INVITATION_TEXT_KEY, value)
    await session.commit()
    await state.clear()
    await message.bot.edit_message_text(
        chat_id=data.get("referral_settings_chat_id", message.chat.id),
        message_id=data.get("referral_settings_message_id", message.message_id),
        text="✅ <b>Текст приглашения сохранён.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К настройке приглашения", callback_data="admin_referral_invitation")]
        ]),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete invitation input", exc_info=True)


@router.callback_query(F.data == "admin_referral_invitation_preview")
async def admin_referral_invitation_preview(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    config = await get_referral_program_config(session)
    user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
    user = user_result.scalar_one_or_none()
    code = user.referral_code if user and user.referral_code else "example_code"
    link = f"https://t.me/{settings.BOT_USERNAME}?start={code}"
    text = escape(render_invitation_text(config.invitation_text, link))
    await callback.message.edit_text(
        f"👁 <b>Предпросмотр приглашения</b>\n\n{text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral_invitation")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_referral_invitation_reset")
async def admin_referral_invitation_reset(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    await save_referral_setting(session, REFERRAL_INVITATION_TEXT_KEY, DEFAULT_INVITATION_TEXT)
    await session.commit()
    await admin_referral_invitation(callback, state, session)


@router.callback_query(F.data.startswith("setting_key_"))
async def admin_setting_edit_key(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Выбор ключа настройки"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    key = callback.data.replace("setting_key_", "")
    if key not in {"welcome_text", "support_chat", "faq_text", "rules_text"}:
        await callback.answer("Некорректная настройка", show_alert=True)
        return
    result = await session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    current_value = setting.value if setting and setting.value else "не установлено"
    current_preview = escape(current_value)
    if len(current_preview) > 3000:
        current_preview = current_preview[:3000] + "…"
    await state.update_data(
        setting_key=key,
        setting_chat_id=callback.message.chat.id,
        setting_message_id=callback.message.message_id,
    )
    await state.set_state(AdminStates.waiting_setting_edit_value)
    await callback.message.edit_text(
        f"⚙️ <b>{BOT_SETTING_LABELS[key]}</b>\n\n"
        f"<b>Текущее значение:</b>\n{current_preview}\n\n"
        "Отправьте новое значение следующим сообщением. После сохранения этот экран обновится.",
        reply_markup=_input_cancel_keyboard({
            "welcome_text": "admin_interaction",
            "support_chat": "admin_interaction_support",
            "faq_text": "admin_interaction_faq",
            "rules_text": "admin_interaction_rules",
        }[key]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_setting_edit_value)
async def admin_setting_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    """Сохранение настройки"""
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        await message.answer("❌ Доступ запрещен")
        return
    data = await state.get_data()
    key = data.get("setting_key")

    if key not in {"welcome_text", "support_chat", "faq_text", "rules_text"} or not message.text:
        await message.answer("Ошибка. Начните редактирование заново.")
        await state.clear()
        return

    # Ищем существующую настройку
    stmt = select(Setting).where(Setting.key == key)
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = message.text
    else:
        setting = Setting(key=key, value=message.text)
        session.add(setting)

    await session.commit()
    current_value = escape(message.text)
    if len(current_value) > 3000:
        current_value = current_value[:3000] + "…"
    chat_id = data.get("setting_chat_id")
    message_id = data.get("setting_message_id")
    await message.bot.edit_message_text(
        chat_id=chat_id if isinstance(chat_id, int) else message.chat.id,
        message_id=message_id if isinstance(message_id, int) else message.message_id,
        text=(
            f"✅ <b>{BOT_SETTING_LABELS[key]}</b> обновлено.\n\n"
            f"<b>Текущее значение:</b>\n{current_value}\n\n"
            "Отправьте новое значение, чтобы изменить его ещё раз."
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data={
                "welcome_text": "admin_interaction",
                "support_chat": "admin_interaction_support",
                "faq_text": "admin_interaction_faq",
                "rules_text": "admin_interaction_rules",
            }[key])],
        ]),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete setting input message", exc_info=True)


# ========== УПРАВЛЕНИЕ АККАУНТАМИ ==========

