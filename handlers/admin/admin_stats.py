"""Административные обработчики: stats."""
from .admin_context import *


def sale_time_expression():
    """Единая дата продажи: оплата, выполнение или создание заказа."""
    return func.coalesce(Order.paid_at, Order.completed_at, Order.created_at)


def sales_query(start: datetime | None = None, end: datetime | None = None):
    """Запрос оплаченных заказов с покупателем и товаром."""
    sale_time = sale_time_expression()
    statement = (
        select(Order, User, Product, sale_time.label("sale_time"))
        .join(User, Order.user_id == User.id)
        .join(Product, Order.product_id == Product.id)
        .where(Order.status.in_(SALES_STATUSES))
    )
    if start is not None:
        statement = statement.where(sale_time >= start)
    if end is not None:
        statement = statement.where(sale_time < end)
    return statement


def format_period_label(kind: str, value: str) -> tuple[str, datetime, datetime]:
    """Преобразовать callback периода в заголовок и границы времени."""
    if kind == "day":
        start = datetime.strptime(value, "%Y%m%d")
        return start.strftime("%d.%m.%Y"), start, start + timedelta(days=1)
    if kind == "month":
        start = datetime.strptime(value, "%Y%m")
        end = datetime(start.year + 1, 1, 1) if start.month == 12 else datetime(start.year, start.month + 1, 1)
        return f"{RUSSIAN_MONTHS[start.month - 1]} {start.year}", start, end
    if kind == "year":
        start = datetime.strptime(value, "%Y")
        return f"{value} год", start, datetime(start.year + 1, 1, 1)
    if kind == "week":
        year, week = int(value[:4]), int(value[4:])
        start = datetime.fromisocalendar(year, week, 1)
        end = start + timedelta(days=6)
        return f"неделя {week} ({start.day} {RUSSIAN_MONTHS[start.month - 1]} — {end.day} {RUSSIAN_MONTHS[end.month - 1]})", start, end + timedelta(days=1)
    raise ValueError("Unknown statistics period")


async def show_sales_report(
    callback: CallbackQuery,
    session: AsyncSession,
    title: str,
    statement,
    *,
    page: int = 0,
    back_callback: str = "admin_stats",
    page_callback_prefix: str | None = None,
    next_buttons: list | None = None,
) -> None:
    """Показать агрегаты и детальные покупки за выбранный период."""
    sale_time = sale_time_expression()
    count_result = await session.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0), func.coalesce(func.sum(Order.quantity), 0))
        .where(Order.status.in_(SALES_STATUSES))
        .where(*statement._where_criteria)
    )
    count, revenue, quantity = count_result.one()
    total_pages = max(1, (count + SALES_PER_PAGE - 1) // SALES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    result = await session.execute(
        statement.order_by(sale_time.desc()).offset(page * SALES_PER_PAGE).limit(SALES_PER_PAGE)
    )
    rows = result.all()

    text = (
        f"📊 <b>{escape(title)}</b>\n\n"
        f"💰 Выручка: <b>{format_rubles(revenue)} ₽</b>\n"
        f"📦 Покупок: <b>{count}</b>\n"
        f"🔢 Товаров продано: <b>{quantity}</b>\n"
    )
    if not rows:
        text += "\nЗа этот период покупок не было."
    else:
        text += f"\n{_grouped_sales_text(rows)}"

    buttons = list(next_buttons or [])
    if page_callback_prefix and total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"{page_callback_prefix}_{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="stats_noop"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"{page_callback_prefix}_{page + 1}"))
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery, session: AsyncSession):
    """Главное меню статистики."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    # Общее количество пользователей
    stmt_users = select(func.count(User.id))
    result_users = await session.execute(stmt_users)
    total_users = result_users.scalar()

    # Общее количество заказов
    stmt_orders = select(func.count(Order.id))
    result_orders = await session.execute(stmt_orders)
    total_orders = result_orders.scalar()

    # Заказы по статусам
    stmt_pending = select(func.count(Order.id)).where(Order.status == "ОЖИДАЕТ ОПЛАТЫ")
    result_pending = await session.execute(stmt_pending)
    pending_orders = result_pending.scalar()

    stmt_completed = select(func.count(Order.id)).where(Order.status == "ВЫПОЛНЕНО")
    result_completed = await session.execute(stmt_completed)
    completed_orders = result_completed.scalar()

    # Общая сумма продаж
    stmt_revenue = select(func.sum(Order.total_amount)).where(Order.status.in_(SALES_STATUSES))
    result_revenue = await session.execute(stmt_revenue)
    total_revenue = result_revenue.scalar() or 0
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    sale_time = sale_time_expression()
    today_revenue = (await session.execute(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(Order.status.in_(SALES_STATUSES), sale_time >= today_start)
    )).scalar() or 0
    month_revenue = (await session.execute(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(Order.status.in_(SALES_STATUSES), sale_time >= month_start)
    )).scalar() or 0
    text = f"""📊 <b>Статистика</b>

👥 Пользователей в боте: <b>{total_users}</b>
📦 Всего заказов: <b>{total_orders}</b>
⏳ Ожидают оплаты: <b>{pending_orders}</b>
✅ Выполнено: <b>{completed_orders}</b>

💰 Выручка за сегодня: <b>{format_rubles(today_revenue)} ₽</b>
📅 Выручка за месяц: <b>{format_rubles(month_revenue)} ₽</b>
🏦 Выручка за всё время: <b>{format_rubles(total_revenue)} ₽</b>

Выберите нужный раздел:"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Продажи", callback_data="admin_stats_years"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_stats_users_years"),
        ],
        [InlineKeyboardButton(text="👥 Балансы пользователей", callback_data="ledger_accounts_0")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_stats_days")
async def admin_stats_days(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    buttons = _stats_year_buttons("admin_stats_day_year_")
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")])
    await callback.message.edit_text(
        "📋 <b>Статистика по дням</b>\n\nВыберите год:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_stats_months")
async def admin_stats_months(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    buttons = _stats_year_buttons("admin_stats_month_year_")
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")])
    await callback.message.edit_text(
        "🗓 <b>Статистика по месяцам</b>\n\nВыберите год:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


def _stats_year_buttons(prefix: str):
    """Календарные годы: от 2020 до текущего, без ограничений по продажам."""
    years = range(datetime.now().year, 2025, -1)
    buttons, row = [], []
    for year in years:
        row.append(InlineKeyboardButton(text=str(year), callback_data=f"{prefix}{year}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


def _grouped_sales_text(rows: list | tuple) -> str:
    """Сгруппировать покупки по датам в компактные HTML-блоки."""
    groups: dict[str, list[str]] = {}
    totals: dict[str, Decimal] = {}
    for order, user, product, sale_time_value in rows:
        if sale_time_value is not None:
            date_text = sale_time_value.strftime("%d.%m.%Y")
            time_text = sale_time_value.strftime("%H:%M")
        else:
            date_text, time_text = "Дата неизвестна", "—"
        name = escape(user.first_name or "Без имени")
        if user.username:
            safe_username = escape(user.username)
            buyer = f'{name} (<a href="https://t.me/{safe_username}">@{safe_username}</a>)'
        else:
            buyer = name
        telegram_id = int(user.telegram_id)
        groups.setdefault(date_text, []).append(
            f"{time_text} | <b>#{order.id}</b> | {escape(product.name)} | "
            f"<b>{format_rubles(order.total_amount)} ₽</b>\n"
            f'<a href="tg://user?id={telegram_id}">{telegram_id}</a> | {buyer}'
        )
        totals[date_text] = totals.get(date_text, ZERO) + money(order.total_amount)
    return "\n\n".join(
        f"📅 <b>{date_text}</b> · 💰 <b>{format_rubles(totals[date_text])} ₽</b>\n"
        f"<blockquote>{'\n'.join(lines)}</blockquote>"
        for date_text, lines in groups.items()
    )


async def _latest_sales_text(session: AsyncSession, limit: int = 10) -> str:
    """Последние покупки для экрана выбора года."""
    result = await session.execute(
        sales_query().order_by(
            sale_time_expression().desc(), Order.id.desc()
        ).limit(limit)
    )
    rows = result.all()
    if not rows:
        return "Покупок пока нет."
    return _grouped_sales_text(rows)


def _grouped_users_text(users: list[User]) -> str:
    """Сгруппировать пользователей по дате в компактные HTML-блоки."""
    groups: dict[str, list[str]] = {}
    for user in users:
        if user.created_at is not None:
            date_text = user.created_at.strftime("%d.%m.%Y")
            time_text = user.created_at.strftime("%H:%M")
        else:
            date_text, time_text = "Дата неизвестна", "—"
        name = escape(user.first_name or "Без имени")
        if user.username:
            safe_username = escape(user.username)
            display_name = (
                f'{name} (<a href="https://t.me/{safe_username}">@{safe_username}</a>)'
            )
        else:
            display_name = name
        telegram_id = int(user.telegram_id)
        groups.setdefault(date_text, []).append(
            f'{time_text} | <a href="tg://user?id={telegram_id}">{telegram_id}</a> | '
            f"{display_name}"
        )
    return "\n\n".join(
        f"📅 <b>{date_text}</b> · 👥 <b>{len(lines)} чел.</b>\n"
        f"<blockquote>{'\n'.join(lines)}</blockquote>"
        for date_text, lines in groups.items()
    )


async def _latest_users_text(session: AsyncSession, limit: int = 10) -> str:
    """Компактный список последних регистраций для первого экрана аудитории."""
    users = (await session.execute(
        select(User).order_by(User.created_at.desc(), User.id.desc()).limit(limit)
    )).scalars().all()
    if not users:
        return "Пользователей пока нет."
    return _grouped_users_text(list(users))


@router.callback_query(F.data == "admin_stats_years")
async def admin_stats_years(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    buttons = _stats_year_buttons("admin_stats_period_year_")
    for row in buttons:
        for button in row:
            button.callback_data = f"{button.callback_data}_0"
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")])
    latest = await _latest_sales_text(session)
    await callback.message.edit_text(
        f"{latest}\n\nВыберите год:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_month_year_"))
async def admin_stats_month_year(callback: CallbackQuery, session: AsyncSession):
    year = int(callback.data.rsplit("_", 1)[1])
    buttons = []
    for month in range(1, 13):
        button = InlineKeyboardButton(text=RUSSIAN_MONTHS[month - 1].capitalize(), callback_data=f"admin_stats_period_month_{year}{month:02d}_0")
        if month % 2:
            buttons.append([button])
        else:
            buttons[-1].append(button)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats_months")])
    await callback.message.edit_text(f"🗓 <b>Статистика за {year} год</b>\n\nВыберите месяц:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_day_year_"))
async def admin_stats_day_year(callback: CallbackQuery, session: AsyncSession):
    year = int(callback.data.rsplit("_", 1)[1])
    buttons = []
    for month in range(1, 13):
        button = InlineKeyboardButton(text=f"{month:02d}.{year}", callback_data=f"admin_stats_day_month_{year}{month:02d}")
        if month % 2:
            buttons.append([button])
        else:
            buttons[-1].append(button)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats_days")])
    await callback.message.edit_text(f"📋 <b>Дни {year}</b>\n\nВыберите месяц:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_day_month_"))
async def admin_stats_day_month(callback: CallbackQuery, session: AsyncSession):
    value = callback.data.rsplit("_", 1)[1]
    start = datetime.strptime(value, "%Y%m")
    next_month = datetime(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    days = (next_month - start).days
    buttons, row = [], []
    for day in range(1, days + 1):
        row.append(InlineKeyboardButton(text=f"{day:02d}", callback_data=f"admin_stats_period_day_{value}{day:02d}_0"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_stats_day_year_{start.year}")])
    await callback.message.edit_text(f"📋 <b>Дни {start:%m.%Y}</b>\n\nВыберите день:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_stats_weeks")
async def admin_stats_weeks(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    buttons = _stats_year_buttons("admin_stats_week_year_")
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")])
    await callback.message.edit_text("📆 <b>Статистика по неделям</b>\n\nВыберите год:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_week_year_"))
async def admin_stats_week_year(callback: CallbackQuery, session: AsyncSession):
    year = int(callback.data.rsplit("_", 1)[1])
    max_week = datetime(year, 12, 28).isocalendar().week
    buttons, row = [], []
    for week in range(1, max_week + 1):
        row.append(InlineKeyboardButton(text=f"Нед. {week}", callback_data=f"admin_stats_period_week_{year}{week:02d}_0"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats_weeks")])
    await callback.message.edit_text(f"📆 <b>Недели {year}</b>\n\nВыберите неделю:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_period_"))
async def admin_stats_period(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    try:
        _, _, _, kind, value, page_text = callback.data.split("_", 5)
        title, start, end = format_period_label(kind, value)
        page = int(page_text)
    except (ValueError, AttributeError):
        await callback.answer("Некорректный период", show_alert=True)
        return
    back_callback = {"day": f"admin_stats_period_week_{start.isocalendar().year}{start.isocalendar().week:02d}_0", "week": f"admin_stats_period_month_{value[:4]}{start.month:02d}_0", "month": f"admin_stats_period_year_{value[:4]}_0", "year": "admin_stats_years"}[kind]
    next_buttons = []
    if kind == "year":
        row = []
        for month in range(1, 13):
            row.append(InlineKeyboardButton(text=RUSSIAN_MONTHS[month - 1].capitalize(), callback_data=f"admin_stats_period_month_{start.year}{month:02d}_0"))
            if len(row) == 2:
                next_buttons.append(row)
                row = []
        if row:
            next_buttons.append(row)
    elif kind == "month":
        weeks, day = [], start
        while day < end:
            iso = day.isocalendar()
            code = f"{iso.year}{iso.week:02d}"
            if code not in weeks:
                weeks.append(code)
            day += timedelta(days=1)
        for code in weeks:
            _, week_start, week_end = format_period_label("week", code)
            week = int(code[4:])
            next_buttons.append([InlineKeyboardButton(text=f"{week} ({week_start.day} {RUSSIAN_MONTHS[week_start.month - 1]} — {(week_end - timedelta(days=1)).day} {RUSSIAN_MONTHS[(week_end - timedelta(days=1)).month - 1]})", callback_data=f"admin_stats_period_week_{code}_0")])
    elif kind == "week":
        row = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            row.append(InlineKeyboardButton(text=f"{day.day} {RUSSIAN_MONTHS[day.month - 1][:3]}", callback_data=f"admin_stats_period_day_{day:%Y%m%d}_0"))
            if len(row) == 2:
                next_buttons.append(row)
                row = []
        if row:
            next_buttons.append(row)
    await show_sales_report(
        callback, session, f"Статистика за {title}", sales_query(start, end), page=page,
        back_callback=back_callback, page_callback_prefix=f"admin_stats_period_{kind}_{value}",
        next_buttons=next_buttons,
    )


def _user_stats_next_buttons(
    kind: str, start: datetime, end: datetime
) -> list[list[InlineKeyboardButton]]:
    """Навигация год → месяц → неделя → день для статистики пользователей."""
    buttons: list[list[InlineKeyboardButton]] = []
    if kind == "year":
        row = []
        for month in range(1, 13):
            row.append(InlineKeyboardButton(
                text=RUSSIAN_MONTHS[month - 1].capitalize(),
                callback_data=f"admin_stats_users_period_month_{start.year}{month:02d}_0",
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    elif kind == "month":
        weeks, day = [], start
        while day < end:
            iso = day.isocalendar()
            code = f"{iso.year}{iso.week:02d}"
            if code not in weeks:
                weeks.append(code)
            day += timedelta(days=1)
        for code in weeks:
            _, week_start, week_end = format_period_label("week", code)
            week_last_day = week_end - timedelta(days=1)
            buttons.append([InlineKeyboardButton(
                text=(
                    f"{int(code[4:])} ({week_start.day} {RUSSIAN_MONTHS[week_start.month - 1]} — "
                    f"{week_last_day.day} {RUSSIAN_MONTHS[week_last_day.month - 1]})"
                ),
                callback_data=f"admin_stats_users_period_week_{code}_0",
            )])
    elif kind == "week":
        row = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            row.append(InlineKeyboardButton(
                text=f"{day.day} {RUSSIAN_MONTHS[day.month - 1][:3]}",
                callback_data=f"admin_stats_users_period_day_{day:%Y%m%d}_0",
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    return buttons


@router.callback_query(F.data == "admin_stats_users_years")
async def admin_stats_users_years(callback: CallbackQuery, session: AsyncSession):
    """Первый экран отдельной статистики регистраций пользователей."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    buttons = _stats_year_buttons("admin_stats_users_period_year_")
    for row in buttons:
        for button in row:
            button.callback_data = f"{button.callback_data}_0"
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")])
    latest = await _latest_users_text(session)
    await callback.message.edit_text(
        f"{latest}\n\nВыберите год:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_users_period_"))
async def admin_stats_users_period(callback: CallbackQuery, session: AsyncSession):
    """Количество и список пользователей, впервые вошедших за период."""
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    try:
        kind, value, page_text = callback.data.removeprefix(
            "admin_stats_users_period_"
        ).split("_", 2)
        title, start, end = format_period_label(kind, value)
        page = max(0, int(page_text))
    except (ValueError, AttributeError):
        await callback.answer("Некорректный период", show_alert=True)
        return

    total = int((await session.scalar(
        select(func.count(User.id)).where(User.created_at >= start, User.created_at < end)
    )) or 0)
    total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(page, total_pages - 1)
    users = (await session.execute(
        select(User)
        .where(User.created_at >= start, User.created_at < end)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(page * USERS_PER_PAGE)
        .limit(USERS_PER_PAGE)
    )).scalars().all()

    text = f"👥 <b>Пользователи за {escape(title)}</b>\n\nЗашли в бот: <b>{total}</b>\n"
    if users:
        text += f"\n{_grouped_users_text(list(users))}"
    else:
        text += "\nЗа этот период новые пользователи не заходили."

    prefix = f"admin_stats_users_period_{kind}_{value}"
    buttons = _user_stats_next_buttons(kind, start, end)
    if total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}_{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="stats_noop"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}_{page + 1}"))
        buttons.append(navigation)
    back_callback = {
        "day": f"admin_stats_users_period_week_{start.isocalendar().year}{start.isocalendar().week:02d}_0",
        "week": f"admin_stats_users_period_month_{start.year}{start.month:02d}_0",
        "month": f"admin_stats_users_period_year_{start.year}_0",
        "year": "admin_stats_users_years",
    }[kind]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_stats_recent")
async def admin_stats_recent(callback: CallbackQuery, session: AsyncSession):
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await show_sales_report(
        callback, session, "Последние покупки", sales_query(), page=0,
        page_callback_prefix="admin_stats_recent_page",
    )


@router.callback_query(F.data.startswith("admin_stats_recent_page_"))
async def admin_stats_recent_page(callback: CallbackQuery, session: AsyncSession):
    try:
        page = int(callback.data.rsplit("_", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректная страница", show_alert=True)
        return
    await show_sales_report(
        callback, session, "Последние покупки", sales_query(), page=page,
        page_callback_prefix="admin_stats_recent_page",
    )


@router.callback_query(F.data == "stats_noop")
async def admin_stats_noop(callback: CallbackQuery):
    await callback.answer()


# Балансы пользователей и персональная история движений
LEDGER_USERS_PAGE_SIZE = 10
LEDGER_HISTORY_PAGE_SIZE = 8


