"""Обработчик реферальной системы"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message, CopyTextButton
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from html import escape
from database.models import User, ReferralTransaction
from bot import settings
from utils.referral_settings import (
    get_referral_program_config,
    render_invitation_text,
)
from utils.private_chat import apply_private_chat_guard
from utils.single_message import apply_single_message_workflow, edit_input_screen
from urllib.parse import quote
import re
import logging

logger = logging.getLogger(__name__)

router = Router()
apply_private_chat_guard(router)
apply_single_message_workflow(router)


class ReferralStates(StatesGroup):
    waiting_new_code = State()


def referral_link(code: str) -> str:
    return f"https://t.me/{settings.BOT_USERNAME}?start={code}"


async def get_referral_overview(session: AsyncSession, telegram_id: int):
    """Собрать данные для главного экрана реферальной программы."""
    stmt = select(User).where(User.telegram_id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user or not user.referral_code:
        return None, None, None

    stmt_referrals = select(User).where(User.referred_by == user.id)
    result_referrals = await session.execute(stmt_referrals)
    referrals = result_referrals.scalars().all()

    stmt_stats = select(
        func.count(ReferralTransaction.id).label('total_transactions'),
        func.sum(ReferralTransaction.commission).label('total_commission'),
        func.sum(ReferralTransaction.amount).label('total_amount')
    ).where(ReferralTransaction.referrer_id == user.id)
    result_stats = await session.execute(stmt_stats)
    stats = result_stats.first()

    config = await get_referral_program_config(session)
    total_transactions = stats.total_transactions or 0
    total_commission = stats.total_commission or 0.0
    total_amount = stats.total_amount or 0.0
    link = referral_link(user.referral_code)
    referral_text = (
        f"👥 <b>Реферальная программа</b>\n\nВаша ссылка:\n<code>{escape(link)}</code>\n\n"
        f"Награда: {config.reward_percent:g}% за {config.condition_label}."
    )
    if config.cashback_enabled:
        referral_text += f"\nКешбек для приглашённого: {config.cashback_percent:g}% от вашей награды."
    stats_text = f"""

📊 <b>Статистика рефералов:</b>
👥 Всего рефералов: {len(referrals)}
💰 Заработано комиссий: {total_commission:.2f} ₽
📦 Всего транзакций: {total_transactions}
💵 Покупки рефералов: {total_amount:.2f} ₽
"""
    return user, referral_text + stats_text, config


def get_referral_keyboard(code: str, invitation_text: str) -> InlineKeyboardMarkup:
    """Кнопки экрана реферальной программы."""
    link = referral_link(code)
    share_text = quote(render_invitation_text(invitation_text, link))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Пригласить", url=f"https://t.me/share/url?url={quote(link, safe='')}&text={share_text}")],
        [InlineKeyboardButton(text="📋 Копировать ссылку", copy_text=CopyTextButton(text=link))],
        [
            InlineKeyboardButton(text="🔳 QR-код", callback_data="referral_qr"),
            InlineKeyboardButton(text="✏️ Сменить код", callback_data="referral_change_code"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])


@router.callback_query(F.data == "menu_referral")
async def show_referral_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать реферальную программу из inline-главного меню."""
    await state.clear()
    user, text, config = await get_referral_overview(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден. Используйте /start", show_alert=True)
        return

    await callback.message.edit_text(
        text,
        reply_markup=get_referral_keyboard(user.referral_code, config.invitation_text),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "referral_qr")
async def referral_qr(callback: CallbackQuery, session: AsyncSession):
    user, _, _ = await get_referral_overview(session, callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    link = referral_link(user.referral_code)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=512x512&data={quote(link, safe='')}"
    await callback.message.answer_photo(
        qr_url,
        caption="🔳 <b>Ваш реферальный QR-код</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="referral_qr_close")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "referral_qr_close")
async def referral_qr_close(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "referral_change_code")
async def referral_change_code(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReferralStates.waiting_new_code)
    await state.update_data(
        referral_chat_id=callback.message.chat.id,
        referral_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "✏️ <b>Смена реферального кода</b>\n\nОтправьте новый код: 4–50 символов, только латинские буквы, цифры, <code>_</code> и <code>-</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_referral")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ReferralStates.waiting_new_code)
async def referral_change_code_save(message: Message, state: FSMContext, session: AsyncSession):
    code = (message.text or "").strip()
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="menu_referral")]
    ])
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,50}", code):
        await edit_input_screen(
            message,
            state,
            "✏️ <b>Смена реферального кода</b>\n\n"
            "❌ Код должен содержать 4–50 латинских букв, цифр, <code>_</code> или <code>-</code>.\n\n"
            "Отправьте новый код.",
            reply_markup=back_keyboard,
        )
        return
    result = await session.execute(select(User).where(User.referral_code == code))
    owner = result.scalar_one_or_none()
    if owner and owner.telegram_id != message.from_user.id:
        await edit_input_screen(
            message,
            state,
            "✏️ <b>Смена реферального кода</b>\n\n"
            "❌ Этот код уже занят. Отправьте другой.",
            reply_markup=back_keyboard,
        )
        return
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id).with_for_update())
    user = result.scalar_one_or_none()
    if not user:
        await edit_input_screen(
            message,
            state,
            "❌ Пользователь не найден.",
            reply_markup=back_keyboard,
        )
        await state.clear()
        return
    user.referral_code = code
    await session.commit()
    data = await state.get_data()
    await state.clear()
    _, overview, config = await get_referral_overview(session, message.from_user.id)
    await message.bot.edit_message_text(
        chat_id=data.get("referral_chat_id", message.chat.id),
        message_id=data.get("referral_message_id", message.message_id),
        text=f"✅ Код изменён.\n\n{overview}",
        reply_markup=get_referral_keyboard(code, config.invitation_text),
        parse_mode="HTML",
    )
    try:
        await message.delete()
    except TelegramAPIError as exc:
        logger.debug("Could not delete referral code input: %s", exc, exc_info=True)


@router.callback_query(F.data == "referral_stats")
async def show_referral_stats(callback: CallbackQuery, session: AsyncSession):
    """Показать подробную статистику рефералов"""
    user_id = callback.from_user.id

    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    # Получаем список рефералов
    stmt_referrals = select(User).where(User.referred_by == user.id)
    result_referrals = await session.execute(stmt_referrals)
    referrals = result_referrals.scalars().all()

    # Получаем последние транзакции
    stmt_transactions = select(ReferralTransaction).where(
        ReferralTransaction.referrer_id == user.id
    ).order_by(ReferralTransaction.created_at.desc()).limit(20)
    result_transactions = await session.execute(stmt_transactions)
    transactions = result_transactions.scalars().all()

    # Получаем общую статистику
    stmt_stats = select(
        func.count(ReferralTransaction.id).label('total_transactions'),
        func.sum(ReferralTransaction.commission).label('total_commission'),
        func.sum(ReferralTransaction.amount).label('total_amount')
    ).where(ReferralTransaction.referrer_id == user.id)
    result_stats = await session.execute(stmt_stats)
    stats = result_stats.first()

    total_transactions = stats.total_transactions or 0
    total_commission = stats.total_commission or 0.0
    total_amount = stats.total_amount or 0.0

    # Формируем текст
    text = "📊 <b>Подробная статистика рефералов</b>\n\n"
    text += f"👥 Всего рефералов: {len(referrals)}\n"
    text += f"💰 Заработано комиссий: {total_commission:.2f} ₽\n"
    text += f"📦 Всего транзакций: {total_transactions}\n"
    text += f"💵 Общая сумма покупок рефералов: {total_amount:.2f} ₽\n\n"

    if referrals:
        text += "<b>Список рефералов:</b>\n"
        for i, ref in enumerate(referrals[:30], 1):  # Показываем первые 30
            username = f"@{escape(ref.username)}" if ref.username else f"ID: {ref.telegram_id}"
            name = escape(ref.first_name or "")
            text += f"{i}. {name} ({username})\n"

        if len(referrals) > 30:
            text += f"\n... и еще {len(referrals) - 30} рефералов\n"
    else:
        text += "📭 У вас пока нет рефералов\n"

    if transactions:
        text += "\n<b>Последние комиссии:</b>\n"
        for trans in transactions[:10]:  # Показываем последние 10
            text += f"• +{trans.commission:.2f} ₽ (заказ #{trans.order_id}, сумма: {trans.amount:.2f} ₽)\n"

        if len(transactions) > 10:
            text += f"\n... и еще {len(transactions) - 10} транзакций\n"
    else:
        text += "\n📭 Пока нет транзакций с рефералами"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()
