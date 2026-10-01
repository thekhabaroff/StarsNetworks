"""Административные обработчики: ledger_users."""
from .admin_context import *
from .admin_stats import admin_stats, sale_time_expression
from aiogram.exceptions import TelegramAPIError

def _ledger_trim(value: object | None, limit: int = 80) -> str:
    """Сократить текст для Telegram, не добавляя в HTML непроверенные данные."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"


def _ledger_user_name(user: object) -> str:
    """Показать безопасное и короткое имя пользователя в админском журнале."""
    username = _ledger_trim(getattr(user, "username", None), 40)
    if username:
        return f"@{username}"
    first_name = _ledger_trim(getattr(user, "first_name", None), 40)
    if first_name:
        return first_name
    return "Без имени"


def _ledger_user_button_text(user: object) -> str:
    """Подпись кнопки: ID | имя (@username) - баланс."""
    telegram_id = int(getattr(user, "telegram_id"))
    balance = format_rubles(getattr(user, "balance", ZERO))
    prefix = f"{telegram_id} | "
    balance_suffix = f" - {balance} ₽"
    username = str(getattr(user, "username", None) or "").strip().lstrip("@")
    if username:
        username_limit = max(1, 64 - len(prefix) - len(balance_suffix) - len(" (@)") - 1)
        username = _ledger_trim(username, username_limit)
        username_suffix = f" (@{username})"
    else:
        username_suffix = ""
    name = str(getattr(user, "first_name", None) or "").strip() or "Без имени"
    name_limit = max(1, 64 - len(prefix) - len(username_suffix) - len(balance_suffix))
    name = _ledger_trim(name, name_limit)
    return f"{prefix}{name}{username_suffix}{balance_suffix}"[:64]


def _ledger_amount(amount: Decimal) -> str:
    sign = "+" if amount > ZERO else "−"
    return f"{sign}{format_rubles(abs(amount))} ₽"


def _balance_adjustment_actor_id(entry: BalanceLedger) -> int | None:
    """Извлечь Telegram ID администратора из старых и новых корректировок."""
    if entry.reference_type != "balance_adjustment" and entry.reason not in {
        "manual_adjustment",
        "self_topup",
    }:
        return None
    reference = str(entry.reference_id or "")
    match = re.fullmatch(r"telegram:(\d+):\d+", reference)
    if match:
        return int(match.group(1))
    # Ранние записи могли содержать ID только в текстовом описании.
    match = re.search(r"(?:ID[\s,:]*)?(\d{5,})\s*$", str(entry.description or ""))
    return int(match.group(1)) if match else None


async def _balance_adjustment_type(
    session: AsyncSession,
    entry: BalanceLedger,
    amount: Decimal,
) -> str | None:
    actor_id = _balance_adjustment_actor_id(entry)
    if actor_id is None:
        return None
    actor = await session.scalar(select(User).where(User.telegram_id == actor_id))
    name = escape(_ledger_trim(actor.first_name if actor else None, 64) or "Без имени")
    username = ""
    if actor and actor.username:
        username = f" (@{escape(_ledger_trim(actor.username, 64))})"
    action = "Начисление администратором" if amount > ZERO else "Корректировка администратором"
    return f"<b>{action}</b> <code>{actor_id}</code> {name}{username}"


def _ledger_page_buttons(page, previous_callback: str, next_callback: str) -> list[InlineKeyboardButton]:
    """Построить компактную строку навигации по странице журнала."""
    buttons = []
    if page.has_previous:
        buttons.append(InlineKeyboardButton(text="◀️", callback_data=previous_callback))
    buttons.append(
        InlineKeyboardButton(
            text=f"{page.page + 1}/{page.total_pages}", callback_data="ledger_noop"
        )
    )
    if page.has_next:
        buttons.append(InlineKeyboardButton(text="▶️", callback_data=next_callback))
    return buttons


def _ledger_history_body(history, *, show_users: bool) -> str:
    """Сформировать тело страницы журнала; поля из БД экранируются."""
    text = (
        f"Записей: <b>{history.total}</b> · Страница "
        f"{history.page + 1}/{history.total_pages}\n"
    )
    if not history.items:
        return f"{text}\nДвижений пока нет."

    for entry in history.items:
        created_at = entry.created_at.strftime("%d.%m.%Y %H:%M") if entry.created_at else "—"
        text += (
            f"\n• <b>{created_at}</b> · <b>{_ledger_amount(entry.amount)}</b>\n"
            f"{escape(ledger_reason_label(entry.reason))}"
        )
        if show_users:
            text += (
                f"\n👤 {escape(_ledger_user_name(entry))} · "
                f"<code>{entry.telegram_id}</code>"
            )
        if entry.balance_after is not None:
            text += f"\nОстаток после операции: {format_rubles(entry.balance_after)} ₽"
        # 80 исходных символов оставляют запас до лимита Telegram даже если
        # все они будут развёрнуты HTML-экранированием (например, ``&``).
        description = _ledger_trim(entry.description, 80)
        if description:
            text += f"\n{escape(description)}"
        text += "\n"
    return text


async def _latest_ledger_movements_text(session: AsyncSession, items) -> str:
    """Показать последние движения отдельными компактными карточками."""
    if not items:
        return "Движений пока нет."

    actor_ids = {
        actor_id
        for entry in items
        if (actor_id := _balance_adjustment_actor_id(entry)) is not None
    }
    actors = {}
    if actor_ids:
        actor_rows = (
            await session.execute(select(User).where(User.telegram_id.in_(actor_ids)))
        ).scalars().all()
        actors = {actor.telegram_id: actor for actor in actor_rows}

    groups: dict[str, list[str]] = {}
    for entry in items:
        if entry.created_at is not None:
            date_text = entry.created_at.strftime("%d.%m.%Y")
            time_text = entry.created_at.strftime("%H:%M")
        else:
            date_text, time_text = "Дата неизвестна", "—"
        name = escape(_ledger_trim(entry.first_name, 32) or "Без имени")
        if entry.username:
            safe_username = escape(_ledger_trim(entry.username, 32))
            display_name = (
                f'{name} (<a href="https://t.me/{safe_username}">@{safe_username}</a>)'
            )
        else:
            display_name = name
        telegram_id = int(entry.telegram_id)
        actor_id = _balance_adjustment_actor_id(entry)
        if actor_id == telegram_id:
            action = (
                "Самостоятельное пополнение"
                if entry.amount > ZERO
                else "Самостоятельное списание"
            )
        elif actor_id is not None:
            action = (
                "Начисление администратором"
                if entry.amount > ZERO
                else "Корректировка администратором"
            )
        else:
            action = ledger_reason_label(entry.reason)

        movement_text = (
            f"{time_text} · <b>{_ledger_amount(entry.amount)}</b>\n"
            f"👤 {display_name} · <code>{telegram_id}</code>\n"
            f"{escape(action)}"
        )
        if actor_id is not None and actor_id != telegram_id:
            actor = actors.get(actor_id)
            actor_name = escape(_ledger_trim(actor.first_name if actor else None, 32) or "Без имени")
            actor_username = ""
            if actor and actor.username:
                safe_actor_username = escape(_ledger_trim(actor.username, 32))
                actor_username = f" (@{safe_actor_username})"
            movement_text += (
                f"\nВыполнил: {actor_name}{actor_username} · <code>{actor_id}</code>"
            )
        description = _ledger_trim(entry.description, 80)
        if description and actor_id is None:
            movement_text += f"\n💬 {escape(description)}"
        groups.setdefault(date_text, []).append(movement_text)
    return "\n\n".join(
        f"📅 <b>{date_text}</b>\n"
        + "\n".join(f"<blockquote>{line}</blockquote>" for line in lines)
        for date_text, lines in groups.items()
    )


async def _show_ledger_unavailable(callback: CallbackQuery, session: AsyncSession) -> None:
    """Сообщить админу, что схема журнала ещё не применена, не раскрывая БД."""
    logger.warning("Balance ledger is unavailable; database migration may be pending", exc_info=True)
    try:
        await session.rollback()
    except SQLAlchemyError:
        logger.exception("Could not rollback database session after ledger error")
    await callback.message.edit_text(
        "👥 <b>Балансы пользователей пока недоступны</b>\n\n"
        "Для него ещё не применена миграция базы данных. После её применения "
        "журнал начнёт показывать новые операции.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_ledger")
async def admin_ledger_menu(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Совместимость со старыми сообщениями: журнал заменён статистикой."""
    await state.clear()
    await admin_stats(callback, session)


@router.callback_query(F.data.startswith("ledger_accounts_"))
async def admin_ledger_accounts(callback: CallbackQuery, session: AsyncSession):
    """Разбивка текущего баланса по пользователям."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    try:
        requested_page = int(callback.data.rsplit("_", 1)[1])
        page = await list_user_balances(
            session,
            page=requested_page,
            page_size=LEDGER_USERS_PAGE_SIZE,
            nonzero_only=True,
        )
        overview = await get_ledger_overview(session)
        recent_history = (
            await list_ledger_history(session, page=0, page_size=10)
            if page.page == 0
            else None
        )
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    except SQLAlchemyError:
        await _show_ledger_unavailable(callback, session)
        return

    text = (
        f"Всего на балансах: <b>{format_rubles(overview.total_balance)} ₽</b>\n"
        f"Пользователей с ненулевым балансом: <b>{page.total}</b>\n"
    )
    if recent_history is not None:
        movements_text = await _latest_ledger_movements_text(session, recent_history.items)
        text += f"\n{movements_text}\n"
    if not page.items:
        text += "\nПользователей с ненулевым балансом пока нет."

    buttons = []
    for user in page.items:
        buttons.append([
            InlineKeyboardButton(
                text=_ledger_user_button_text(user),
                callback_data=f"user_action_{user.telegram_id}_ledger_{page.page}",
            )
        ])

    if page.total_pages > 1:
        buttons.append(_ledger_page_buttons(
            page,
            f"ledger_accounts_{page.page - 1}",
            f"ledger_accounts_{page.page + 1}",
        ))
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ledger_account_"))
async def admin_ledger_account(callback: CallbackQuery, session: AsyncSession):
    """Баланс и последние операции одного пользователя."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    try:
        user_id_text, source_page_text = callback.data.removeprefix("ledger_account_").rsplit("_", 1)
        user_id, source_page = int(user_id_text), int(source_page_text)
        if user_id <= 0 or source_page < 0:
            raise ValueError
        user = await get_user_balance(session, user_id)
        if not user:
            await callback.answer("Пользователь больше не найден", show_alert=True)
            return
        history = await list_ledger_history(
            session,
            user_id=user.user_id,
            page=0,
            page_size=LEDGER_HISTORY_PAGE_SIZE,
        )
    except ValueError:
        await callback.answer("Некорректный пользователь", show_alert=True)
        return
    except SQLAlchemyError:
        await _show_ledger_unavailable(callback, session)
        return

    text = (
        "👤 <b>Баланс пользователя</b>\n\n"
        f"{escape(_ledger_user_name(user))}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Текущий баланс: <b>{format_rubles(user.balance)} ₽</b>\n\n"
    )
    text += "<b>Последние движения</b>\n\n"
    text += _ledger_history_body(history, show_users=False)

    buttons = []
    if history.total_pages > 1:
        buttons.append(_ledger_page_buttons(
            history,
            f"ledger_entries_{user.user_id}_{source_page}_{history.page - 1}",
            f"ledger_entries_{user.user_id}_{source_page}_{history.page + 1}",
        ))
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"ledger_accounts_{source_page}")])
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ledger_entries_"))
async def admin_ledger_account_history(callback: CallbackQuery, session: AsyncSession):
    """Следующая страница операций выбранного пользователя."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    try:
        data = callback.data.removeprefix("ledger_entries_").split("_")
        if len(data) != 3:
            raise ValueError
        user_id, source_page, requested_page = map(int, data)
        if user_id <= 0 or source_page < 0 or requested_page < 0:
            raise ValueError
        user = await get_user_balance(session, user_id)
        if not user:
            await callback.answer("Пользователь больше не найден", show_alert=True)
            return
        history = await list_ledger_history(
            session,
            user_id=user_id,
            page=requested_page,
            page_size=LEDGER_HISTORY_PAGE_SIZE,
        )
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    except SQLAlchemyError:
        await _show_ledger_unavailable(callback, session)
        return

    text = (
        "👤 <b>Баланс пользователя</b>\n\n"
        f"{escape(_ledger_user_name(user))}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Текущий баланс: <b>{format_rubles(user.balance)} ₽</b>\n\n"
    )
    text += "<b>Последние движения</b>\n\n"
    text += _ledger_history_body(history, show_users=False)
    buttons = []
    if history.total_pages > 1:
        buttons.append(_ledger_page_buttons(
            history,
            f"ledger_entries_{user_id}_{source_page}_{history.page - 1}",
            f"ledger_entries_{user_id}_{source_page}_{history.page + 1}",
        ))
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"ledger_accounts_{source_page}")])
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "ledger_noop")
async def admin_ledger_noop(callback: CallbackQuery):
    await callback.answer()


# Логи
@router.callback_query(F.data == "admin_logs")
async def admin_logs(callback: CallbackQuery, session: AsyncSession):
    """Просмотр логов ошибок"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    stmt = select(Log).where(Log.level == "ERROR").order_by(Log.created_at.desc()).limit(10)
    result = await session.execute(stmt)
    logs = result.scalars().all()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")]
    ])

    if not logs:
        await callback.message.edit_text("Логов ошибок нет", reply_markup=keyboard)
        await callback.answer()
        return

    text = "📝 <b>Последние 10 ошибок:</b>\n\n"
    for log in logs:
        text += f"[{log.created_at.strftime('%d.%m %H:%M')}] {escape(log.message[:100])}\n"

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# Управление пользователями
@router.callback_query(F.data == "admin_users")
async def admin_users_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Меню управления пользователями."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "👥 <b>Пользователи</b>\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Все", callback_data="admin_users_all"),
                InlineKeyboardButton(text="Поиск", callback_data="admin_users_search"),
            ],
            [
                InlineKeyboardButton(text="Администраторы", callback_data="admin_users_admins"),
                InlineKeyboardButton(text="Заблокированные", callback_data="admin_users_blocked"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


def _user_list_button_text(user: User) -> str:
    """Сформировать компактную подпись пользователя для inline-кнопки."""
    prefix = f"{user.telegram_id} | "
    username = (user.username or "").strip().lstrip("@")
    suffix = f" (@{username})" if username else ""
    name = (user.first_name or "").strip() or "Без имени"
    max_name_length = max(1, 64 - len(prefix) - len(suffix))
    return f"{prefix}{name[:max_name_length]}{suffix}"[:64]


async def show_users_list(
    callback: CallbackQuery, session: AsyncSession, title: str, statement
) -> None:
    """Показать компактный список пользователей по выбранному фильтру."""
    result = await session.execute(statement.limit(20))
    users = result.scalars().all()
    if not users:
        await callback.message.edit_text(
            f"👥 <b>{escape(title)}</b>\n\nПользователи не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_users")]
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    buttons = []
    for user in users:
        buttons.append([InlineKeyboardButton(
            text=_user_list_button_text(user),
            callback_data=f"user_action_{user.telegram_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_users")])
    await callback.message.edit_text(
        f"👥 <b>{escape(title)}</b>\n\nВыберите пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_users_all")
async def admin_users_all(callback: CallbackQuery, session: AsyncSession):
    await show_users_list(callback, session, "Все пользователи", select(User).order_by(User.created_at.desc()))


@router.callback_query(F.data == "admin_users_admins")
async def admin_users_admins(callback: CallbackQuery, session: AsyncSession):
    await show_users_list(
        callback, session, "Администраторы",
        select(User).where(User.role.in_(("admin", "developer"))).order_by(User.created_at.desc()),
    )


@router.callback_query(F.data == "admin_users_blocked")
async def admin_users_blocked(callback: CallbackQuery, session: AsyncSession):
    await show_users_list(
        callback, session, "Заблокированные пользователи",
        select(User).where(User.is_blocked.is_(True)).order_by(User.created_at.desc()),
    )


@router.callback_query(F.data == "admin_users_search")
async def admin_users_search_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_user_search)
    await callback.message.edit_text(
        "🔎 <b>Поиск пользователя</b>\n\nВведите Telegram ID, @username или имя:",
        reply_markup=_input_cancel_keyboard("admin_users"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_user_search)
async def admin_users_search_process(message: Message, state: FSMContext, session: AsyncSession):
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        return
    query = (message.text or "").strip().lstrip("@")
    if not query:
        await edit_input_screen(
            message,
            state,
            "🔎 <b>Поиск пользователя</b>\n\nВведите Telegram ID, @username или имя.",
            reply_markup=_input_cancel_keyboard("admin_users"),
        )
        return
    filters = [User.username.ilike(f"%{query}%"), User.first_name.ilike(f"%{query}%")]
    if query.isdigit():
        filters.append(User.telegram_id == int(query))
    result = await session.execute(select(User).where(or_(*filters)).order_by(User.created_at.desc()).limit(20))
    users = result.scalars().all()
    state_data = await state.get_data()
    if not users:
        await edit_input_screen(
            message,
            state,
            "🔎 <b>Результаты поиска</b>\n\nПользователи не найдены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_users")]
            ]),
            state_data=state_data,
        )
        await state.clear()
        return
    buttons = [[InlineKeyboardButton(
        text=_user_list_button_text(user),
        callback_data=f"user_action_{user.telegram_id}",
    )] for user in users]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_users")])
    await edit_input_screen(
        message,
        state,
        "🔎 <b>Результаты поиска</b>\n\nВыберите пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        state_data=state_data,
    )
    await state.clear()


@router.callback_query(F.data.startswith("user_action_"))
async def admin_user_action(callback: CallbackQuery, session: AsyncSession):
    """Полная административная карточка пользователя."""
    try:
        if not await is_admin_async(callback.from_user.id, session):
            await callback.answer("Доступ запрещен", show_alert=True)
            return
        callback_parts = callback.data.split("_")
        user_id = int(callback_parts[2])
        back_callback = "admin_users"
        if len(callback_parts) == 5 and callback_parts[3] == "ledger":
            source_page = int(callback_parts[4])
            if source_page < 0:
                raise ValueError("Некорректная страница")
            back_callback = f"ledger_accounts_{source_page}"
        user = (await session.execute(
            select(User).where(User.telegram_id == user_id)
        )).scalar_one_or_none()
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        actor_is_developer = await is_developer_async(callback.from_user.id, session)
        target_is_developer = (
            user.role == "developer"
            or user.telegram_id in settings.developer_ids_list
        )
        is_self = user.telegram_id == callback.from_user.id
        role_text = {
            "user": "Пользователь",
            "admin": "Администратор",
            "developer": "Разработчик",
        }.get(user.role or "user", user.role or "Пользователь")
        username = f" (@{escape(user.username)})" if user.username else ""
        now = datetime.now()
        discount = Decimal(str(user.personal_discount or 0))
        if not user.personal_discount_enabled or discount <= 0 or (
            user.personal_discount_until is not None
            and user.personal_discount_until <= now
        ):
            discount_text = "• Скидка не назначена"
        else:
            discount_value = (
                f"{discount:g}%"
                if user.personal_discount_type == "PERCENT"
                else f"{format_rubles(discount)} ₽"
            )
            term = (
                "Постоянная"
                if user.personal_discount_until is None
                else f"До {user.personal_discount_until:%d.%m.%Y %H:%M}"
            )
            discount_text = f"• {term}: <b>{discount_value}</b>"
        keyboard_buttons = [
            [InlineKeyboardButton(text="💳 Финансы", callback_data=f"admin_user_finance_{user.id}")],
        ]
        management_buttons = []
        if actor_is_developer and user.telegram_id not in settings.developer_ids_list:
            management_buttons.append(
                InlineKeyboardButton(text="👑 Изменить роль", callback_data=f"admin_user_role_{user.id}")
            )
        if not target_is_developer and not is_self:
            management_buttons.append(
                InlineKeyboardButton(
                    text="🔓 Разблокировать" if user.is_blocked else "🔒 Заблокировать",
                    callback_data=f"admin_user_block_{user.id}",
                )
            )
        if management_buttons:
            keyboard_buttons.append([
                *management_buttons,
            ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
        ])
        await callback.message.edit_text(
            "📝 <b>Информация о пользователе</b>\n\n"
            "👤 <b>Профиль:</b>\n"
            f"• ID: <code>{user.telegram_id}</code>\n"
            f"• Имя: <b>{escape(user.first_name or 'Без имени')}</b>{username}\n"
            f"• Роль: <b>{escape(role_text)}</b>\n"
            f"• Реферальный код: <code>{escape(user.referral_code or 'не назначен')}</code>\n"
            f"• Баланс: <b>{format_rubles(user.balance)} ₽</b>\n"
            f"• Статус: <b>{'Заблокирован' if user.is_blocked else 'Активен'}</b>\n\n"
            "🏷 <b>Скидка:</b>\n"
            f"{discount_text}"
            + ("\n\n🛡 <i>Разработчик защищён от блокировки</i>" if target_is_developer else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in admin_user_action: {e}", exc_info=True)
        await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)


async def _admin_finance_user(
    callback: CallbackQuery, session: AsyncSession, user_id: int
) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
    return user


@router.callback_query(F.data.startswith("admin_user_finance_"))
async def admin_user_finance(callback: CallbackQuery, session: AsyncSession):
    user_id = int(callback.data.rsplit("_", 1)[1])
    user = await _admin_finance_user(callback, session, user_id)
    if user is None:
        return
    await callback.message.edit_text(
        "💳 <b>Финансы пользователя</b>\n\n"
        f"Пользователь: <b>{escape(user.first_name or user.username or str(user.telegram_id))}</b>\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Текущий баланс: <b>{format_rubles(user.balance)} ₽</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Баланс", callback_data=f"admin_user_balance_manage_{user.id}")],
            [
                InlineKeyboardButton(text="🏷 Скидка", callback_data=f"admin_user_discount_{user.id}"),
                InlineKeyboardButton(text="🧾 Транзакции", callback_data=f"admin_user_payment_history_{user.id}_0"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"user_action_{user.telegram_id}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user_balance_manage_"))
async def admin_user_balance_manage(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.clear()
    user_id = int(callback.data.rsplit("_", 1)[1])
    user = await _admin_finance_user(callback, session, user_id)
    if user is None:
        return
    await callback.message.edit_text(
        "💰 <b>Управление балансом</b>\n\n"
        f"Пользователь: <b>{escape(user.first_name or user.username or str(user.telegram_id))}</b>\n"
        f"Текущий баланс: <b>{format_rubles(user.balance)} ₽</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data=f"admin_user_balance_add_{user.id}"),
                InlineKeyboardButton(text="➖ Убавить", callback_data=f"admin_user_balance_subtract_{user.id}"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user_finance_{user.id}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


PERSONAL_DISCOUNT_DEFAULTS = {
    "enabled": False,
    "scope": "ALL",
    "mode": "MAXIMUM",
    "discount_type": "PERCENT",
    "value": ZERO,
    "target_ids": [],
    "valid_until": None,
}
PERSONAL_FIXED_DISCOUNT_MAX = Decimal("99999.99")


def _personal_discount_draft(data: dict) -> dict:
    draft = {**PERSONAL_DISCOUNT_DEFAULTS, **(data.get("personal_discount_draft") or {})}
    draft["value"] = Decimal(str(draft["value"]))
    draft["target_ids"] = sorted({int(item) for item in draft["target_ids"] if str(item).isdigit()})
    return draft


def _personal_discount_config_from_draft(draft: dict) -> PersonalDiscountConfig:
    return PersonalDiscountConfig(
        enabled=bool(draft["enabled"]),
        scope=draft["scope"],
        mode=draft["mode"],
        discount_type=draft["discount_type"],
        value=Decimal(str(draft["value"])),
        target_ids=frozenset(draft["target_ids"]),
        valid_until=draft["valid_until"],
    )


def _personal_discount_text(draft: dict) -> str:
    status = "🟢 Включена" if draft["enabled"] else "🔴 Выключена"
    kind = "Процентная" if draft["discount_type"] == "PERCENT" else "Фиксированная"
    value = f"{draft['value']:g}%" if draft["discount_type"] == "PERCENT" else f"{format_rubles(draft['value'])} ₽"
    mode = "Суммируется" if draft["mode"] == "STACK" else "Максимальная"
    scope = GLOBAL_DISCOUNT_SCOPE_LABELS[draft["scope"]]
    targets = "все товары" if draft["scope"] == "ALL" else f"выбрано: {len(draft['target_ids'])}"
    term = "Постоянная" if draft["valid_until"] is None else f"до {draft['valid_until']:%d.%m.%Y %H:%M}"
    return (
        "🏷 <b>Скидка пользователя</b>\n\n"
        f"<blockquote>• <b>Статус:</b> {status}\n"
        f"• <b>Тип:</b> {kind}\n"
        f"• <b>Скидка:</b> {value}\n"
        f"• <b>Режим:</b> {mode}\n"
        f"• <b>Влияние:</b> {scope} ({targets})\n"
        f"• <b>Срок:</b> {term}</blockquote>\n"
        "Изменения сохранятся после нажатия «Принять»."
    )


def _personal_discount_keyboard(draft: dict) -> InlineKeyboardMarkup:
    value = f"{draft['value']:g}%" if draft["discount_type"] == "PERCENT" else f"{format_rubles(draft['value'])} ₽"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 Выключить" if draft["enabled"] else "🟢 Включить",
            callback_data="admin_personal_discount_toggle",
        )],
        [
            InlineKeyboardButton(text="📌 Влияние", callback_data="admin_personal_discount_scope"),
            InlineKeyboardButton(text="⚙️ Режим", callback_data="admin_personal_discount_combine"),
        ],
        [
            InlineKeyboardButton(
                text=("🔘 " if draft["discount_type"] == "PERCENT" else "⚪ ") + "Процентная",
                callback_data="admin_personal_discount_type_PERCENT",
            ),
            InlineKeyboardButton(
                text=("🔘 " if draft["discount_type"] == "FIXED" else "⚪ ") + "Фиксированная",
                callback_data="admin_personal_discount_type_FIXED",
            ),
        ],
        [InlineKeyboardButton(text=f"🏷 Скидка: {value}", callback_data="admin_personal_discount_value")],
        [InlineKeyboardButton(text="⏳ Срок действия", callback_data="admin_personal_discount_term")],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_personal_discount_cancel"),
            InlineKeyboardButton(text="✅ Принять", callback_data="admin_personal_discount_accept"),
        ],
    ])


def _personal_discount_percent_keyboard(draft: dict) -> InlineKeyboardMarkup:
    current = Decimal(str(draft["value"]))
    current_label = "🚫 Нет скидки" if current == ZERO else f"🏷 {current:g}%"
    rows = [[InlineKeyboardButton(
        text=f"{percent}%",
        callback_data=f"admin_personal_discount_percent_{percent}",
    ) for percent in range(start, start + 25, 5)] for start in range(5, 81, 25)]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"[{current_label}]", callback_data="admin_personal_discount_percent_zero")],
        *rows,
        [InlineKeyboardButton(text="✏️ Ручной ввод", callback_data="admin_personal_discount_percent_manual")],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_personal_discount_percent_cancel"),
            InlineKeyboardButton(text="✅ Принять", callback_data="admin_personal_discount_percent_accept"),
        ],
    ])


async def _show_personal_discount(callback: CallbackQuery, state: FSMContext) -> None:
    draft = _personal_discount_draft(await state.get_data())
    await callback.message.edit_text(
        _personal_discount_text(draft),
        reply_markup=_personal_discount_keyboard(draft),
        parse_mode="HTML",
    )


async def _show_personal_discount_percent(callback: CallbackQuery, state: FSMContext) -> None:
    draft = _personal_discount_draft(await state.get_data())
    await callback.message.edit_text(
        "🏷 <b>Размер скидки</b>\n\nВыберите процент скидки:",
        reply_markup=_personal_discount_percent_keyboard(draft), parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin_user_discount_\d+$"))
async def admin_user_discount(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.clear()
    user_id = int(callback.data.rsplit("_", 1)[1])
    user = await _admin_finance_user(callback, session, user_id)
    if user is None:
        return
    config = await get_personal_discount_config(session, user)
    # An expired term must not leave the editor looking enabled.  Start an
    # expired configuration as disabled/permanent so enabling it again really
    # makes it active; the administrator can choose a new temporary term.
    expired = (
        user.personal_discount_until is not None
        and user.personal_discount_until <= datetime.now()
    )
    await state.update_data(
        personal_discount_user_id=user.id,
        personal_discount_draft={
            "enabled": config.enabled,
            "scope": config.scope,
            "mode": config.mode,
            "discount_type": config.discount_type,
            "value": config.value,
            "target_ids": list(config.target_ids),
            "valid_until": None if expired else user.personal_discount_until,
        },
        personal_discount_chat_id=callback.message.chat.id,
        personal_discount_message_id=callback.message.message_id,
    )
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_toggle")
async def admin_personal_discount_toggle(callback: CallbackQuery, state: FSMContext):
    draft = _personal_discount_draft(await state.get_data())
    draft["enabled"] = not draft["enabled"]
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_scope")
async def admin_personal_discount_scope(callback: CallbackQuery, state: FSMContext):
    draft = _personal_discount_draft(await state.get_data())
    buttons = []
    for scope, icon, label in (
        ("ALL", "🌐", "Всё"), ("CATEGORY", "📂", "Категории"),
        ("SUBCATEGORY", "🗂", "Подкатегории"), ("GROUP", "📁", "Группы"),
        ("TYPE", "🏷", "Типы"), ("PRODUCT", "📦", "Товары"),
    ):
        marker = "✅ " if draft["scope"] == scope else ""
        buttons.append([InlineKeyboardButton(
            text=f"{marker}{icon} {label}",
            callback_data=f"admin_personal_discount_scope_{scope}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_personal_discount_editor")])
    await callback.message.edit_text(
        "📌 <b>Влияние скидки</b>\n\nВыберите, на что она распространяется.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_personal_discount_scope_"))
async def admin_personal_discount_scope_select(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    scope = callback.data.removeprefix("admin_personal_discount_scope_")
    if scope not in SCOPES:
        await callback.answer("Некорректное влияние", show_alert=True)
        return
    draft = _personal_discount_draft(await state.get_data())
    if draft["scope"] != scope:
        draft["target_ids"] = []
    draft["scope"] = scope
    await state.update_data(personal_discount_draft=draft)
    if scope == "ALL":
        await _show_personal_discount(callback, state)
    else:
        await _show_personal_discount_targets(callback, state, session, scope)
    await callback.answer()


async def _show_personal_discount_targets(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, scope: str
) -> None:
    draft = _personal_discount_draft(await state.get_data())
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
        items = (await session.execute(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
        )).scalars().all()
        title = "товары"
    buttons = [[InlineKeyboardButton(
        text=("✅ " if item.id in selected else "☑️ ") + item.name[:46],
        callback_data=f"admin_personal_discount_target_{scope}_{item.id}",
    )] for item in items[:80]]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_personal_discount_scope")])
    text = f"📌 <b>Выберите {title}</b>\n\nОтмеченные позиции получат скидку."
    if not items:
        text = f"📌 <b>Выбор: {title}</b>\n\nПодходящих элементов пока нет."
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_personal_discount_target_"))
async def admin_personal_discount_target(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    scope, target_text = callback.data.removeprefix("admin_personal_discount_target_").rsplit("_", 1)
    if scope not in SCOPES or not target_text.isdigit():
        await callback.answer("Некорректная позиция", show_alert=True)
        return
    draft = _personal_discount_draft(await state.get_data())
    if draft["scope"] != scope:
        await callback.answer("Откройте выбор заново", show_alert=True)
        return
    selected = set(draft["target_ids"])
    target_id = int(target_text)
    if target_id in selected:
        selected.remove(target_id)
    else:
        selected.add(target_id)
    draft["target_ids"] = sorted(selected)
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount_targets(callback, state, session, scope)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_combine")
async def admin_personal_discount_combine(callback: CallbackQuery, state: FSMContext):
    draft = _personal_discount_draft(await state.get_data())
    await callback.message.edit_text(
        "⚙️ <b>Режим скидки</b>\n\nСуммировать — складывает её с другими скидками.\n"
        "Максимальная — применяется только если она больше остальных.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("✅ " if draft["mode"] == "STACK" else "") + "Суммировать", callback_data="admin_personal_discount_combine_STACK")],
            [InlineKeyboardButton(text=("✅ " if draft["mode"] == "MAXIMUM" else "") + "Максимальная", callback_data="admin_personal_discount_combine_MAXIMUM")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_personal_discount_editor")],
        ]), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_personal_discount_combine_"))
async def admin_personal_discount_combine_select(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.removeprefix("admin_personal_discount_combine_")
    if mode not in {"STACK", "MAXIMUM"}:
        await callback.answer("Некорректный режим", show_alert=True)
        return
    draft = _personal_discount_draft(await state.get_data())
    draft["mode"] = mode
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_personal_discount_type_"))
async def admin_personal_discount_type(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.removeprefix("admin_personal_discount_type_")
    if kind not in {"PERCENT", "FIXED"}:
        await callback.answer("Некорректный тип", show_alert=True)
        return
    draft = _personal_discount_draft(await state.get_data())
    draft["discount_type"] = kind
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_value")
async def admin_personal_discount_value(callback: CallbackQuery, state: FSMContext):
    draft = _personal_discount_draft(await state.get_data())
    if draft["discount_type"] == "PERCENT":
        await state.update_data(personal_discount_value_original=str(draft["value"]))
        await _show_personal_discount_percent(callback, state)
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_personal_discount_percent)
    await callback.message.edit_text(
        "🏷 <b>Размер скидки</b>\n\nВведите сумму в рублях от 0 до 99 999,99.",
        reply_markup=_input_cancel_keyboard("admin_personal_discount_editor"), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_personal_discount_percent_\d+$"))
async def admin_personal_discount_percent_select(callback: CallbackQuery, state: FSMContext):
    percent = int(callback.data.rsplit("_", 1)[1])
    if percent == 100:
        await callback.answer("Скидка 100% недоступна: бесплатные заказы отключены.", show_alert=True)
        return
    if percent not in range(5, 101, 5):
        await callback.answer("Некорректный размер скидки", show_alert=True)
        return
    draft = _personal_discount_draft(await state.get_data())
    draft["value"] = Decimal(percent)
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount_percent(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_percent_zero")
async def admin_personal_discount_percent_zero(callback: CallbackQuery, state: FSMContext):
    draft = _personal_discount_draft(await state.get_data())
    draft["value"] = ZERO
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount_percent(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_percent_manual")
async def admin_personal_discount_percent_manual(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_personal_discount_percent)
    await callback.message.edit_text(
        "✏️ <b>Ручной ввод скидки</b>\n\nВведите процент от 0 до 99.99.",
        reply_markup=_input_cancel_keyboard("admin_personal_discount_percent_back"), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_percent_back")
async def admin_personal_discount_percent_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _show_personal_discount_percent(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_percent_cancel")
async def admin_personal_discount_percent_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    draft = _personal_discount_draft(data)
    draft["value"] = Decimal(str(data.get("personal_discount_value_original", draft["value"])))
    await state.update_data(personal_discount_draft=draft, personal_discount_value_original=None)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_percent_accept")
async def admin_personal_discount_percent_accept(callback: CallbackQuery, state: FSMContext):
    await state.update_data(personal_discount_value_original=None)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.message(AdminStates.waiting_personal_discount_percent)
async def admin_user_discount_value_input(message: Message, state: FSMContext):
    data = await state.get_data()
    draft = _personal_discount_draft(data)
    maximum = (
        Decimal("99.99")
        if draft["discount_type"] == "PERCENT"
        else PERSONAL_FIXED_DISCOUNT_MAX
    )
    try:
        value = to_money(message.text or "", minimum=ZERO, maximum=maximum)
    except ValueError:
        await edit_input_screen(
            message, state, "❌ <b>Некорректная скидка</b>\n\nВведите корректное значение.",
            reply_markup=_input_cancel_keyboard("admin_personal_discount_editor"), state_data=data,
        )
        return
    draft["value"] = value
    await state.update_data(personal_discount_draft=draft)
    await state.set_state(None)
    if draft["discount_type"] == "PERCENT":
        text = "🏷 <b>Размер скидки</b>\n\nВыберите процент скидки:"
        keyboard = _personal_discount_percent_keyboard(draft)
    else:
        text = _personal_discount_text(draft)
        keyboard = _personal_discount_keyboard(draft)
    await message.bot.edit_message_text(
        chat_id=data["personal_discount_chat_id"], message_id=data["personal_discount_message_id"],
        text=text, reply_markup=keyboard, parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete personal discount input", exc_info=True)


@router.callback_query(F.data == "admin_personal_discount_term")
async def admin_personal_discount_term(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    draft = _personal_discount_draft(await state.get_data())
    await callback.message.edit_text(
        "⏳ <b>Срок действия</b>\n\nВыберите постоянную или временную скидку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("✅ " if draft["valid_until"] is None else "") + "♾ Постоянная", callback_data="admin_personal_discount_term_permanent")],
            [InlineKeyboardButton(text="⏳ Временная", callback_data="admin_personal_discount_term_temporary")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_personal_discount_editor")],
        ]), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_term_permanent")
async def admin_personal_discount_term_permanent(callback: CallbackQuery, state: FSMContext):
    draft = _personal_discount_draft(await state.get_data())
    draft["valid_until"] = None
    await state.update_data(personal_discount_draft=draft)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_term_temporary")
async def admin_personal_discount_term_temporary(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_personal_discount_days)
    await callback.message.edit_text(
        "⏳ <b>Временная скидка</b>\n\nВведите количество дней от 1 до 36500.",
        reply_markup=_input_cancel_keyboard("admin_personal_discount_term"), parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_personal_discount_days)
async def admin_user_discount_days_input(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 36500:
            raise ValueError
    except ValueError:
        await edit_input_screen(
            message, state, "❌ <b>Некорректный срок</b>\n\nВведите число дней от 1 до 36500.",
            reply_markup=_input_cancel_keyboard("admin_personal_discount_term"), state_data=data,
        )
        return
    draft = _personal_discount_draft(data)
    draft["valid_until"] = datetime.now() + timedelta(days=days)
    await state.update_data(personal_discount_draft=draft)
    await state.set_state(None)
    await message.bot.edit_message_text(
        chat_id=data["personal_discount_chat_id"], message_id=data["personal_discount_message_id"],
        text=_personal_discount_text(draft), reply_markup=_personal_discount_keyboard(draft), parse_mode="HTML",
    )
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete personal discount term input", exc_info=True)


@router.callback_query(F.data == "admin_personal_discount_editor")
async def admin_personal_discount_editor(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _show_personal_discount(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_cancel")
async def admin_personal_discount_cancel(callback: CallbackQuery, state: FSMContext):
    user_id = (await state.get_data()).get("personal_discount_user_id")
    await state.clear()
    await callback.message.edit_text(
        "❌ Изменения скидки отменены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user_finance_{user_id}")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_personal_discount_accept")
async def admin_personal_discount_accept(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    user = await session.get(User, int(data.get("personal_discount_user_id", 0)))
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    draft = _personal_discount_draft(data)
    if draft["enabled"] and draft["value"] <= ZERO:
        await callback.answer("Укажите скидку больше 0", show_alert=True)
        return
    if draft["discount_type"] == "PERCENT" and draft["value"] >= Decimal("100"):
        await callback.answer("Процентная скидка должна быть меньше 100%", show_alert=True)
        return
    if draft["enabled"] and draft["scope"] != "ALL" and not draft["target_ids"]:
        await callback.answer("Выберите хотя бы одну позицию", show_alert=True)
        return
    await save_personal_discount_config(session, user, _personal_discount_config_from_draft(draft))
    await session.commit()
    await state.clear()
    await callback.message.edit_text(
        "✅ <b>Скидка пользователя сохранена.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К финансам", callback_data=f"admin_user_finance_{user.id}")]
        ]), parse_mode="HTML",
    )
    await callback.answer("Скидка сохранена")


@router.callback_query(F.data.startswith("admin_user_payment_history_"))
async def admin_user_payment_history(callback: CallbackQuery, session: AsyncSession):
    tail = callback.data.removeprefix("admin_user_payment_history_")
    user_id_text, page_text = tail.rsplit("_", 1)
    user_id, page = int(user_id_text), max(0, int(page_text))
    user = await _admin_finance_user(callback, session, user_id)
    if user is None:
        return
    sale_time = sale_time_expression()
    ledger_events = select(
        literal("ledger").label("source"),
        BalanceLedger.id.label("event_id"),
        literal("SUCCESS").label("status"),
        BalanceLedger.created_at.label("event_at"),
        BalanceLedger.amount.label("amount"),
        BalanceLedger.reason.label("kind"),
        BalanceLedger.description.label("detail"),
        BalanceLedger.reference_id.label("reference"),
    ).where(
        BalanceLedger.user_id == user.id,
        BalanceLedger.reason != "purchase",
    )
    purchase_events = select(
        literal("order").label("source"),
        Order.id.label("event_id"),
        literal("SUCCESS").label("status"),
        sale_time.label("event_at"),
        (-Order.total_amount).label("amount"),
        literal("purchase").label("kind"),
        Product.name.label("detail"),
        cast(Order.id, String).label("reference"),
    ).join(Product, Product.id == Order.product_id).where(
        Order.user_id == user.id,
        Order.status.in_(SALES_STATUSES),
    )
    payment_events = select(
        literal("payment").label("source"),
        Payment.id.label("event_id"),
        Payment.status.label("status"),
        Payment.created_at.label("event_at"),
        Payment.amount.label("amount"),
        literal("payment").label("kind"),
        Payment.payment_method.label("detail"),
        Payment.payment_id.label("reference"),
    ).where(
        Payment.user_id == user.id,
        Payment.status != "SUCCESS",
    )
    events = union_all(ledger_events, purchase_events, payment_events).subquery()
    total = int((await session.scalar(select(func.count()).select_from(events))) or 0)
    per_page = 10
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages - 1)
    rows = (await session.execute(
        select(events).order_by(events.c.event_at.desc()).offset(page * per_page).limit(per_page)
    )).all()
    text = "🧾 <b>Транзакции пользователя</b>"
    buttons = []
    if not rows:
        text += "\n\nТранзакций пока нет."
    else:
        for source, event_id, status, event_at, amount, kind, detail, reference in rows:
            date_text = event_at.strftime("%d.%m.%Y %H:%M:%S") if event_at else "Дата неизвестна"
            if status == "SUCCESS":
                status_icon = "➕" if Decimal(str(amount)) > ZERO else "➖"
            else:
                status_icon = "❌" if status == "FAILED" else "⏳"
            buttons.append([InlineKeyboardButton(
                text=f"{status_icon} {date_text}",
                callback_data=f"admin_user_transaction_{source}_{event_id}_{user.id}_{page}",
            )])
    if total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_user_payment_history_{user.id}_{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ledger_noop"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_user_payment_history_{user.id}_{page + 1}"))
        buttons.append(navigation)
    buttons.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user_finance_{user.id}"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
    ])
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user_transaction_"))
async def admin_user_transaction_detail(callback: CallbackQuery, session: AsyncSession):
    tail = callback.data.removeprefix("admin_user_transaction_")
    source, event_id_text, user_id_text, page_text = tail.rsplit("_", 3)
    event_id, user_id, page = int(event_id_text), int(user_id_text), int(page_text)
    user = await _admin_finance_user(callback, session, user_id)
    if user is None:
        return
    if source == "ledger":
        entry = await session.get(BalanceLedger, event_id)
        if entry is None or entry.user_id != user.id:
            await callback.answer("Транзакция не найдена", show_alert=True)
            return
        amount = Decimal(str(entry.amount))
        adjustment_type = await _balance_adjustment_type(session, entry, amount)
        type_text = adjustment_type or f"<b>{escape(ledger_reason_label(entry.reason))}</b>"
        date_text = entry.created_at.strftime("%d.%m.%Y %H:%M:%S") if entry.created_at else "—"
        text = (
            "🧾 <b>Транзакция</b>\n\n"
            f"Статус: <b>✅ Выполнена</b>\n"
            f"Тип: {type_text}\n"
            f"Сумма: <b>{_ledger_amount(amount)}</b>\n"
            f"Дата: <b>{date_text}</b>"
        )
        if entry.balance_after is not None:
            text += f"\nБаланс после операции: <b>{format_rubles(entry.balance_after)} ₽</b>"
        if entry.description and adjustment_type is None:
            text += f"\nОписание: {escape(_ledger_trim(entry.description, 160))}"
    elif source == "order":
        order = await session.get(Order, event_id)
        if order is None or order.user_id != user.id or order.status not in SALES_STATUSES:
            await callback.answer("Транзакция не найдена", show_alert=True)
            return
        product = await session.get(Product, order.product_id)
        event_at = order.paid_at or order.completed_at or order.created_at
        date_text = event_at.strftime("%d.%m.%Y %H:%M:%S") if event_at else "—"
        text = (
            "🧾 <b>Транзакция</b>\n\n"
            "Статус: <b>✅ Выполнена</b>\n"
            f"Тип: <b>Покупка #{order.id}</b>\n"
            f"Товар: <b>{escape(product.name if product else 'Товар удалён')}</b>\n"
            f"Количество: <b>{order.quantity}</b>\n"
            f"Сумма: <b>−{format_rubles(order.total_amount)} ₽</b>\n"
            f"Способ оплаты: <b>{escape(order.payment_method or 'не указан')}</b>\n"
            f"Дата: <b>{date_text}</b>"
        )
    elif source == "payment":
        payment = await session.get(Payment, event_id)
        if payment is None or payment.user_id != user.id or payment.status == "SUCCESS":
            await callback.answer("Транзакция не найдена", show_alert=True)
            return
        event_at = payment.created_at
        date_text = event_at.strftime("%d.%m.%Y %H:%M:%S") if event_at else "—"
        status_icon = "❌" if payment.status == "FAILED" else "⏳"
        status_text = "Не выполнена" if payment.status == "FAILED" else "Ожидает оплаты"
        text = (
            "🧾 <b>Транзакция</b>\n\n"
            f"Статус: <b>{status_icon} {status_text}</b>\n"
            f"Тип: <b>Платёж</b>\n"
            f"Сумма: <b>{format_rubles(payment.amount)} ₽</b>\n"
            f"Способ оплаты: <b>{escape(payment.payment_method)}</b>\n"
            f"Дата: <b>{date_text}</b>"
        )
    else:
        await callback.answer("Некорректная транзакция", show_alert=True)
        return
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user_payment_history_{user.id}_{page}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user_block_"))
async def admin_user_block(callback: CallbackQuery, session: AsyncSession):
    """Блокировка/разблокировка пользователя"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    user_id = int(callback.data.split("_")[3])

    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    if user.telegram_id == callback.from_user.id:
        await callback.answer("Нельзя заблокировать самого себя", show_alert=True)
        return

    if user.role == "developer" or user.telegram_id in settings.developer_ids_list:
        await callback.answer("Разработчиков нельзя блокировать", show_alert=True)
        return

    user.is_blocked = not user.is_blocked
    await session.commit()

    status = "заблокирован" if user.is_blocked else "разблокирован"

    # Отправляем уведомление пользователю
    try:
        if user.is_blocked:
            notification_text = (
                "❌ <b>Вы были заблокированы</b>\n\n"
                "Ваш доступ к боту ограничен администратором.\n"
                "Если вы считаете, что это ошибка, обратитесь в поддержку."
            )
        else:
            notification_text = (
                "✅ <b>Вы были разблокированы</b>\n\n"
                "Ваш доступ к боту восстановлен. Вы можете продолжать пользоваться всеми функциями."
            )

        await callback.bot.send_message(
            user.telegram_id,
            notification_text,
            parse_mode="HTML"
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Failed to send block/unblock notification to user %s: %s",
            user.telegram_id,
            exc,
            exc_info=True,
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_users")]
    ])
    await callback.message.edit_text(
        f"✅ Пользователь {status}",
        reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data == "admin_bulk_block_users")
async def admin_bulk_block_users_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать массовую блокировку пользователей"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_bulk_block_users)
    keyboard = _input_cancel_keyboard("admin_users")
    await callback.message.edit_text(
        "🔒 <b>Массовая блокировка пользователей</b>\n\n"
        "Введите ID пользователей через запятую (например: 123456, 789012, 345678):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminStates.waiting_bulk_block_users)
async def admin_bulk_block_users_process(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка массовой блокировки"""

    try:
        user_ids = [int(uid.strip()) for uid in message.text.split(",")]
        blocked = 0
        not_found = 0
        notified = 0
        skipped = 0

        for user_id in user_ids:
            stmt = select(User).where(User.telegram_id == user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and (
                user.telegram_id == message.from_user.id
                or user.role == "developer"
                or user.telegram_id in settings.developer_ids_list
            ):
                skipped += 1
            elif user:
                user.is_blocked = True
                blocked += 1

                # Отправляем уведомление пользователю
                try:
                    notification_text = (
                        "❌ <b>Вы были заблокированы</b>\n\n"
                        "Ваш доступ к боту ограничен администратором.\n"
                        "Если вы считаете, что это ошибка, обратитесь в поддержку."
                    )
                    await message.bot.send_message(
                        user.telegram_id,
                        notification_text,
                        parse_mode="HTML"
                    )
                    notified += 1
                except TelegramAPIError as exc:
                    logger.warning(
                        "Failed to send block notification to user %s: %s",
                        user.telegram_id,
                        exc,
                        exc_info=True,
                    )
            else:
                not_found += 1

        await session.commit()

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_users")]
        ])

        await message.answer(
            f"✅ Массовая блокировка завершена!\n\n"
            f"Заблокировано: {blocked}\n"
            f"Уведомлений отправлено: {notified}\n"
            f"Пропущено защищённых: {skipped}\n"
            f"Не найдено: {not_found}",
            reply_markup=keyboard
        )
        await state.clear()

    except ValueError:
        await message.answer(
            "Неверный формат. Введите ID через запятую (числа):",
            reply_markup=_input_cancel_keyboard("admin_users")
        )


@router.callback_query(F.data.regexp(r"^admin_user_balance_(?:\d+|(?:add|subtract)_\d+)$"))
async def admin_user_balance_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать увеличение или уменьшение баланса пользователя."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    tail = callback.data.removeprefix("admin_user_balance_")
    if "_" in tail:
        operation, user_id_text = tail.split("_", 1)
    else:
        operation, user_id_text = "add", tail
    user_id = int(user_id_text)
    user = await session.get(User, user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await state.update_data(
        user_id=user_id,
        is_admin_self=False,
        balance_operation=operation,
    )
    await state.set_state(AdminStates.waiting_balance_amount)

    await callback.message.edit_text(
        f"{'➕' if operation == 'add' else '➖'} <b>"
        f"{'Добавление средств' if operation == 'add' else 'Уменьшение баланса'}</b>\n\n"
        f"Текущий баланс: <b>{format_rubles(user.balance)} ₽</b>\n"
        f"Введите сумму, которую нужно {'добавить' if operation == 'add' else 'убавить'}:",
        reply_markup=_input_cancel_keyboard(f"admin_user_balance_manage_{user.id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_balance_amount)
async def admin_user_balance_finish(message: Message, state: FSMContext, session: AsyncSession):
    """Завершить пополнение баланса (для пользователя или администратора)"""
    data = await state.get_data()
    try:
        amount = to_money(message.text, minimum=Decimal("0.01"))
        is_admin_self = data.get("is_admin_self", False)
        operation = data.get("balance_operation", "add")

        if is_admin_self:
            # Пополнение баланса администратора
            user_id = message.from_user.id
            stmt = (
                select(User)
                .where(User.telegram_id == user_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                await edit_input_screen(message, state, "Пользователь не найден. Используйте /start", state_data=data)
                await state.clear()
                return
        else:
            # Пополнение баланса другого пользователя
            user_id = data.get("user_id")

            if not user_id:
                await edit_input_screen(message, state, "Ошибка: пользователь не указан.", state_data=data)
                await state.clear()
                return

            stmt = (
                select(User)
                .where(User.id == user_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                await edit_input_screen(message, state, "Пользователь не найден.", state_data=data)
                await state.clear()
                return

        # Ключ строится из входящего Telegram-сообщения. Если Telegram
        # доставит тот же update повторно, строка журнала уже существует и
        # баланс второй раз не изменится.
        current_balance = money(user.balance)
        if operation == "subtract" and amount > current_balance:
            await session.rollback()
            await edit_input_screen(
                message,
                state,
                "❌ Нельзя убавить больше текущего баланса.\n\n"
                f"Доступно: <b>{format_rubles(current_balance)} ₽</b>",
                reply_markup=_input_cancel_keyboard(f"admin_user_balance_manage_{user.id}"),
                state_data=data,
            )
            return
        signed_amount = amount if operation == "add" else -amount
        balance_after = current_balance + signed_amount
        actor_self = user.telegram_id == message.from_user.id
        action_description = (
            "Самостоятельное пополнение: "
            if actor_self and operation == "add"
            else "Самостоятельное уменьшение баланса: "
            if actor_self
            else "Корректировку выполнил: "
        )
        ledger_result = await record_manual_adjustment(
            session,
            user_id=user.id,
            amount=signed_amount,
            adjustment_id=f"telegram:{message.chat.id}:{message.message_id}",
            description=action_description + (
                f"{message.from_user.first_name or 'Без имени'}"
                + (f" (@{message.from_user.username})" if message.from_user.username else "")
                + f", ID {message.from_user.id}"
            ),
            balance_after=balance_after,
            is_self_topup=actor_self and operation == "add",
        )
        if not ledger_result.created:
            await session.rollback()
            await edit_input_screen(
                message, state,
                "ℹ️ Эта корректировка уже была обработана. Баланс повторно не изменён.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К финансам", callback_data=f"admin_user_finance_{user.id}")]
                ]),
                state_data=data,
            )
            await state.clear()
            return

        # The object is locked in this transaction. The immutable journal row
        # already contains the exact post-operation balance, then both changes
        # are committed together.
        user.balance = balance_after
        await session.commit()

        # Обновляем объект пользователя для получения нового баланса
        await session.refresh(user)

        # Уведомляем пользователя (если это не сам админ пополняет свой баланс)
        if not is_admin_self and not actor_self:
            try:
                await message.bot.send_message(
                    user.telegram_id,
                    f"💰 <b>Баланс изменён</b>\n\n"
                    f"{'Зачислено' if operation == 'add' else 'Списано'}: "
                    f"{format_rubles(amount)} ₽\n"
                    f"Текущий баланс: {format_rubles(user.balance)} ₽",
                    parse_mode="HTML"
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not notify user %s about balance adjustment: %s",
                    user.telegram_id,
                    exc,
                    exc_info=True,
                )

        # Уведомляем администраторов о пополнении баланса
        try:
            if operation == "add":
                from utils.notifications import notify_balance_topup
                await notify_balance_topup(session, user, amount, message.bot)
            else:
                from utils.notifications import send_notification_to_admins
                await send_notification_to_admins(
                    message.bot,
                    "➖ <b>Баланс пользователя уменьшен</b>\n\n"
                    f"Пользователь: {escape(user.first_name or user.username or 'Без имени')} "
                    f"(<code>{user.telegram_id}</code>)\n"
                    f"Списано: <b>{format_rubles(amount)} ₽</b>\n"
                    f"Новый баланс: <b>{format_rubles(user.balance)} ₽</b>",
                )
        except TelegramAPIError as exc:
            logger.warning("Error notifying about balance adjustment: %s", exc, exc_info=True)

        if is_admin_self:
            result_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")]
            ])
            result_text = (
                f"✅ Ваш баланс изменён!\n\nСумма: {_ledger_amount(signed_amount)}\n"
                f"Новый баланс: {format_rubles(user.balance)} ₽"
            )
        else:
            result_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К финансам", callback_data=f"admin_user_finance_{user.id}")]
            ])
            result_text = (
                f"✅ Баланс пользователя изменён!\n\n"
                f"Пользователь: {escape(user.first_name or user.username or 'Без имени')}\n"
                f"Изменение: {_ledger_amount(signed_amount)}\n"
                f"Новый баланс: {format_rubles(user.balance)} ₽"
            )
        await edit_input_screen(
            message, state, result_text, reply_markup=result_keyboard, state_data=data
        )
        await state.clear()

    except ValueError:
        back_callback = (
            "admin_menu" if data.get("is_admin_self")
            else f"admin_user_balance_manage_{data.get('user_id')}"
        )
        await edit_input_screen(
            message, state,
            "❌ Введите корректную положительную сумму:",
            reply_markup=_input_cancel_keyboard(back_callback),
            state_data=data,
        )


@router.callback_query(F.data == "admin_topup_self")
async def admin_topup_self_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать пополнение баланса администратора"""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_balance_amount)
    await state.update_data(
        is_admin_self=True,
        balance_operation="add",
    )  # Флаг, что это пополнение своего баланса

    keyboard = _input_cancel_keyboard("admin_menu")

    await callback.message.edit_text(
        "💰 <b>Пополнение своего баланса</b>\n\n"
        "Введите сумму пополнения (число, можно с точкой, например: 1000 или 1000.50):",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()





# ========== Р¤РР›Р¬РўР Р« Р—РђРљРђР—РћР’ ==========



