"""Обработчик команды /start"""
import asyncio
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import User
from utils.keyboards import get_main_menu_keyboard
from utils.cart import get_cart_summary
from utils.interactions import get_interaction_config
from utils.private_chat import apply_private_chat_guard
from utils.text import WELCOME_MESSAGE
from bot import settings
from database.models import Setting
import secrets
import string

router = Router()
apply_private_chat_guard(router)
logger = logging.getLogger(__name__)

# Telegram удаляет только сообщения моложе 48 часов. Ограничение диапазона
# защищает Bot API от тысяч запросов, если пользователь долго спамил чат или
# его message_id уже очень большой. Для обычного диалога 10 000 позиций с
# большим запасом покрывают всё доступное Telegram окно удаления.
CLEAR_HISTORY_MESSAGE_LIMIT = 10_000
CLEAR_HISTORY_BATCH_SIZE = 100


def generate_referral_code() -> str:
    """Генерация реферального кода"""
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))


def _clear_history_batches(last_message_id: int) -> list[list[int]]:
    """Build newest-first batches accepted by Telegram ``deleteMessages``."""
    first_message_id = max(1, last_message_id - CLEAR_HISTORY_MESSAGE_LIMIT + 1)
    batches: list[list[int]] = []
    batch_end = last_message_id
    while batch_end >= first_message_id:
        batch_start = max(first_message_id, batch_end - CLEAR_HISTORY_BATCH_SIZE + 1)
        batches.append(list(range(batch_start, batch_end + 1)))
        batch_end = batch_start - 1
    return batches


async def _delete_message_ids(message: Message, message_ids: list[int]) -> bool | None:
    """Delete one Telegram batch.

    ``False`` means Telegram rejected these particular message IDs and the
    caller may split the batch. ``None`` means an API/network failure where
    issuing more cleanup requests would only make the situation worse.
    """
    while True:
        try:
            await message.bot.delete_messages(
                chat_id=message.chat.id,
                message_ids=message_ids,
            )
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(float(exc.retry_after), 0.1))
        except TelegramBadRequest:
            return False
        except TelegramAPIError as exc:
            logger.warning("Chat-history cleanup request failed: %s", exc)
            return None


async def _delete_history_range(message: Message, message_ids: list[int]) -> bool | None:
    """Delete a newest-first time range with logarithmic boundary lookup.

    Returns ``True`` when Telegram's non-deletable boundary was reached,
    ``False`` when the whole range was deleted, and ``None`` on an API error.
    Message identifiers grow with time inside a private chat, so a rejected
    mixed batch can be split in half and its newer half handled first.
    """
    result = await _delete_message_ids(message, message_ids)
    if result is None:
        return None
    if result:
        return False
    if len(message_ids) == 1:
        return True

    midpoint = len(message_ids) // 2
    older_ids = message_ids[:midpoint]
    newer_ids = message_ids[midpoint:]

    newer_boundary = await _delete_history_range(message, newer_ids)
    if newer_boundary is None or newer_boundary:
        return newer_boundary
    return await _delete_history_range(message, older_ids)


async def _delete_available_chat_history(message: Message) -> None:
    """Delete every recent chat message Telegram still permits the bot to remove."""
    for message_ids in _clear_history_batches(message.message_id):
        boundary_reached = await _delete_history_range(message, message_ids)
        if boundary_reached is None:
            return
        if boundary_reached:
            logger.info(
                "Chat history cleared up to Telegram's deletion boundary for chat %s",
                message.chat.id,
            )
            return


@router.message(Command("clear"))
async def cmd_clear(message: Message, session: AsyncSession, state: FSMContext):
    """Удалить доступную Telegram историю и показать одно главное меню."""
    await state.clear()

    user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    if not user:
        await message.answer("Используйте /start для регистрации.")
        return

    setting = await session.scalar(select(Setting).where(Setting.key == "welcome_text"))
    welcome_text = setting.value if setting and setting.value else WELCOME_MESSAGE
    is_admin = (
        message.from_user.id in settings.admin_ids_list
        or message.from_user.id in settings.developer_ids_list
        or user.role in ("admin", "developer")
    )

    await _delete_available_chat_history(message)

    interaction = await get_interaction_config(session)
    cart_summary = await get_cart_summary(session, user.id)
    await message.answer(
        welcome_text,
        reply_markup=get_main_menu_keyboard(
            is_admin=is_admin,
            balance=user.balance,
            cart_count=cart_summary.items_count,
            cart_total=cart_summary.total,
            community_url=interaction.community_source if interaction.community_enabled else None,
            support_url=interaction.support_source if interaction.support_enabled else None,
        ),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    """Обработка команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    # Проверяем реферальный код
    referral_code = None
    if len(message.text.split()) > 1:
        referral_code = message.text.split()[1]

    # Получаем или создаем пользователя
    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        # Создаем нового пользователя
        referred_by = None
        if referral_code:
            # Ищем реферера
            stmt_ref = select(User).where(User.referral_code == referral_code)
            result_ref = await session.execute(stmt_ref)
            referrer = result_ref.scalar_one_or_none()
            if referrer:
                referred_by = referrer.id

        user_code = generate_referral_code()
        # Проверяем уникальность кода
        while True:
            stmt_check = select(User).where(User.referral_code == user_code)
            result_check = await session.execute(stmt_check)
            if result_check.scalar_one_or_none() is None:
                break
            user_code = generate_referral_code()

        # Определяем роль пользователя на основе .env
        user_role = "user"
        if user_id in settings.developer_ids_list:
            user_role = "developer"
        elif user_id in settings.admin_ids_list:
            user_role = "admin"

        user = User(
            telegram_id=user_id,
            username=username,
            first_name=first_name,
            referral_code=user_code,
            referred_by=referred_by,
            role=user_role
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # Уведомляем администраторов о новой регистрации
        try:
            from utils.notifications import notify_user_registration
            await notify_user_registration(session, user, message.bot)
        except Exception as e:
            logger.error("Error notifying about registration: %s", e)

        # Получаем приветствие из настроек или используем по умолчанию
        stmt_setting = select(Setting).where(Setting.key == "welcome_text")
        result_setting = await session.execute(stmt_setting)
        setting = result_setting.scalar_one_or_none()
        welcome_text_db = setting.value if setting and setting.value else WELCOME_MESSAGE

        # Пользовательское соглашение доступно отдельным пунктом в разделе
        # «Информация». Не дублируем его целиком на первом экране.
        welcome_text = f"{welcome_text_db}\n\n✅ Вы успешно зарегистрированы!"
    else:
        # Обновляем данные пользователя
        user.username = username
        user.first_name = first_name

        # Синхронизируем роль с .env (если пользователь в .env, но роль не совпадает)
        if user_id in settings.developer_ids_list and user.role != "developer":
            user.role = "developer"
        elif user_id in settings.admin_ids_list and user.role != "admin":
            user.role = "admin"
        elif user_id not in settings.admin_ids_list and user_id not in settings.developer_ids_list and user.role in ("admin", "developer"):
            # Если пользователь удален из .env, но имеет роль админа - не меняем (может быть назначен через бота)
            pass

        await session.commit()
        # Получаем приветствие из настроек или используем по умолчанию
        stmt_setting = select(Setting).where(Setting.key == "welcome_text")
        result_setting = await session.execute(stmt_setting)
        setting = result_setting.scalar_one_or_none()
        welcome_text = setting.value if setting and setting.value else WELCOME_MESSAGE

    # Проверяем права администратора (гибридная проверка: .env + БД)
    is_admin = user_id in settings.admin_ids_list or user_id in settings.developer_ids_list
    if not is_admin and user.role in ("admin", "developer"):
        is_admin = True

    interaction = await get_interaction_config(session)
    cart_summary = await get_cart_summary(session, user.id)
    await message.answer(
        welcome_text,
        reply_markup=get_main_menu_keyboard(
            is_admin=is_admin,
            balance=user.balance,
            cart_count=cart_summary.items_count,
            cart_total=cart_summary.total,
            community_url=interaction.community_source if interaction.community_enabled else None,
            support_url=interaction.support_source if interaction.support_enabled else None,
        ),
    )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Возврат в главное меню"""
    # Очищаем FSM состояние
    await state.clear()

    user_id = callback.from_user.id

    # Получаем пользователя из БД для проверки роли
    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    # Проверяем права администратора (гибридная проверка: .env + БД)
    is_admin = user_id in settings.admin_ids_list or user_id in settings.developer_ids_list
    if not is_admin and user and user.role in ("admin", "developer"):
        is_admin = True

    # Получаем приветствие из настроек или используем по умолчанию
    stmt_setting = select(Setting).where(Setting.key == "welcome_text")
    result_setting = await session.execute(stmt_setting)
    setting = result_setting.scalar_one_or_none()
    welcome_text = setting.value if setting and setting.value else WELCOME_MESSAGE

    interaction = await get_interaction_config(session)
    cart_summary = await get_cart_summary(session, user.id)
    main_keyboard = get_main_menu_keyboard(
        is_admin=is_admin,
        balance=user.balance,
        cart_count=cart_summary.items_count,
        cart_total=cart_summary.total,
        community_url=interaction.community_source if interaction.community_enabled else None,
        support_url=interaction.support_source if interaction.support_enabled else None,
    )

    # Меняем текущее сообщение: навигация работает через inline-кнопки.
    try:
        await callback.message.edit_text(
            welcome_text,
            reply_markup=main_keyboard,
            parse_mode="HTML"
        )
    except TelegramBadRequest as exc:
        # Сообщение может быть медиа-сообщением или уже содержать тот же текст.
        if "message is not modified" not in str(exc).lower():
            await callback.message.answer(
                welcome_text,
                reply_markup=main_keyboard,
                parse_mode="HTML"
            )
    await callback.answer()
