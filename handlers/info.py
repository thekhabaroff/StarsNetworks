"""Обработчик информации и поддержки"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from html import escape
import re
from database.models import Setting, User
from utils.text import FAQ_TEXT, RULES_TEXT
from utils.single_message import apply_single_message_workflow, edit_input_screen
from utils.interactions import get_interaction_config
from utils.private_chat import apply_private_chat_guard
from bot import settings
import logging

logger = logging.getLogger(__name__)

router = Router()
apply_single_message_workflow(router)
# FAQ/support menu screens belong to a user and must not be rendered in a
# shared group.  Group messages themselves remain available exclusively for
# the configured support-chat reply workflow below.
apply_private_chat_guard(router, protect_messages=False)

SUPPORT_TARGET_PREFIX = "support_user_id:"


class SupportStates(StatesGroup):
    """Явное состояние диалога с поддержкой для заблокированных пользователей."""

    waiting_message = State()


def get_menu_back_keyboard() -> InlineKeyboardMarkup:
    """Стандартная inline-кнопка возврата в главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])


def get_support_cancel_keyboard() -> InlineKeyboardMarkup:
    """Отменить ожидание сообщения для поддержки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Отмена", callback_data="back_to_menu")]
    ])


def get_information_keyboard(
    faq_enabled: bool = True,
    rules_enabled: bool = True,
    rules_source: str = "",
    privacy_enabled: bool = False,
    privacy_source: str = "",
) -> InlineKeyboardMarkup:
    """Разделы справочной информации."""
    buttons = []
    if faq_enabled:
        buttons.append([InlineKeyboardButton(text="❓ Часто задаваемые вопросы", callback_data="info_faq")])
    if rules_enabled:
        if rules_source:
            buttons.append([InlineKeyboardButton(text="📜 Пользовательское соглашение", url=rules_source)])
        else:
            buttons.append([InlineKeyboardButton(text="📜 Пользовательское соглашение", callback_data="info_rules")])
    if privacy_enabled:
        if privacy_source:
            buttons.append([InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=privacy_source)])
        else:
            buttons.append([InlineKeyboardButton(text="🔒 Политика конфиденциальности", callback_data="info_privacy")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_information_back_keyboard() -> InlineKeyboardMarkup:
    """Возврат из справочного текста к выбору раздела."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К информации", callback_data="menu_info")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
    ])


async def get_setting_value(session: AsyncSession, key: str, default: str = "") -> str:
    """Получить текстовую настройку, сохранив заданное значение по умолчанию."""
    stmt = select(Setting).where(Setting.key == key)
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    return setting.value if setting and setting.value else default


async def get_support_chat_id(session: AsyncSession):
    """Вернуть ID единственного разрешённого чата поддержки."""
    raw_chat_id = await get_setting_value(session, "support_chat_id")
    if not raw_chat_id:
        return None

    try:
        chat_id = int(raw_chat_id)
        if chat_id == 0:
            raise ValueError("support_chat_id cannot be zero")
        return chat_id
    except (TypeError, ValueError):
        logger.warning("Invalid support_chat_id setting")
        return None


async def is_support_agent(user_id: int, session: AsyncSession) -> bool:
    """Разрешить ответы поддержки только администраторам и разработчикам."""
    if user_id in settings.admin_ids_list or user_id in settings.developer_ids_list:
        return True

    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    return bool(user and user.role in ("admin", "developer"))


def is_support_setup_command(message: Message) -> bool:
    """Проверить безопасную команду первоначальной настройки чата поддержки."""
    if not message.text:
        return False

    command = message.text.split(maxsplit=1)[0].lower()
    return command == "/set_support_chat" or command.startswith("/set_support_chat@")


async def get_support_screen_text(session: AsyncSession) -> str:
    """Собрать HTML-безопасный текст экрана поддержки."""
    support_chat_id = await get_support_chat_id(session)
    support_contact = await get_setting_value(session, "support_chat")

    if support_chat_id:
        return (
            "💬 <b>Поддержка</b>\n\n"
            "Напишите ваше сообщение, и администратор обязательно вам ответит.\n\n"
            "Вы можете отправить текст, фото или файл."
        )
    if support_contact:
        return f"💬 Для связи с поддержкой перейдите в чат: {escape(support_contact)}"

    return "💬 <b>Поддержка временно недоступна.</b>\n\nПопробуйте позднее."


@router.callback_query(F.data == "menu_info")
async def show_info_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать меню справочной информации."""
    await state.clear()
    interaction = await get_interaction_config(session)
    await callback.message.edit_text(
        "ℹ️ <b>Информация</b>\n\nВыберите нужный раздел:",
        reply_markup=get_information_keyboard(
            faq_enabled=interaction.faq_enabled,
            rules_enabled=interaction.rules_enabled,
            rules_source=interaction.rules_source if interaction.rules_enabled else "",
            privacy_enabled=interaction.privacy_enabled,
            privacy_source=interaction.privacy_source if interaction.privacy_enabled else "",
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "info_faq")
async def show_faq_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать FAQ из раздела информации."""
    await state.clear()
    if not (await get_interaction_config(session)).faq_enabled:
        await callback.message.edit_text(
            "❓ <b>FAQ временно недоступен.</b>",
            reply_markup=get_menu_back_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    faq_text = await get_setting_value(session, "faq_text", FAQ_TEXT)
    await callback.message.edit_text(faq_text, reply_markup=get_information_back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "info_rules")
async def show_rules_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать пользовательское соглашение из раздела информации."""
    await state.clear()
    interaction = await get_interaction_config(session)
    if not interaction.rules_enabled:
        await callback.message.edit_text(
            "📜 <b>Пользовательское соглашение временно недоступно.</b>",
            reply_markup=get_menu_back_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    rules_text = await get_setting_value(session, "rules_text", RULES_TEXT)
    await callback.message.edit_text(rules_text, reply_markup=get_information_back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "info_privacy")
async def show_privacy_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать сообщение вместо политики, если источник ещё не указан."""
    await state.clear()
    interaction = await get_interaction_config(session)
    if not interaction.privacy_enabled:
        await callback.message.edit_text(
            "🔒 <b>Политика конфиденциальности временно недоступна.</b>",
            reply_markup=get_menu_back_keyboard(),
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            "🔒 <b>Политика конфиденциальности</b>\n\n"
            "Источник ещё не указан администратором.",
            reply_markup=get_information_back_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "menu_rules")
async def legacy_rules_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Сохранить работу старых кнопок с уже отправленных меню."""
    await show_info_callback(callback, session, state)


@router.callback_query(F.data == "menu_support")
async def show_support_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать поддержку из inline-главного меню."""
    if not (await get_interaction_config(session)).support_enabled:
        await state.clear()
        await callback.message.edit_text(
            "💬 <b>Поддержка временно недоступна.</b>",
            reply_markup=get_menu_back_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    support_text = await get_support_screen_text(session)
    support_chat_id = await get_support_chat_id(session)
    if support_chat_id:
        await state.set_state(SupportStates.waiting_message)
    else:
        await state.clear()
    await callback.message.edit_text(
        support_text,
        reply_markup=(get_support_cancel_keyboard() if support_chat_id else get_menu_back_keyboard()),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(F.chat.type.in_(["group", "supergroup"]))
async def handle_group_message(message: Message, session: AsyncSession):
    """Обрабатывать ответы только из единственного настроенного чата поддержки."""
    if not message.from_user:
        return

    support_chat_id = await get_support_chat_id(session)

    # Первичная настройка выполняется явной командой сотрудника. После настройки
    # никакая другая группа не может изменить ID чата поддержки.
    if support_chat_id is None:
        if not is_support_setup_command(message):
            return
        if not await is_support_agent(message.from_user.id, session):
            return

        stmt = select(Setting).where(Setting.key == "support_chat_id")
        result = await session.execute(stmt)
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = str(message.chat.id)
        else:
            session.add(Setting(key="support_chat_id", value=str(message.chat.id)))
        await session.commit()
        logger.info("Support chat configured")
        await message.answer(
            "✅ Этот чат назначен чатом поддержки. "
            "Отвечайте пользователю только ответом на сообщение бота с заявкой."
        )
        return

    # Сообщения из любых других групп полностью игнорируются.
    if message.chat.id != support_chat_id or not message.reply_to_message:
        return

    if not await is_support_agent(message.from_user.id, session):
        logger.warning("Ignored support reply from an unauthorized group member")
        return

    await handle_support_reply(message, session)


async def handle_support_reply(message: Message, session: AsyncSession):
    """Обработка ответов от поддержки пользователям"""
    # Проверки повторяются здесь, чтобы функцию нельзя было небезопасно вызвать
    # из нового обработчика в будущем.
    if not message.from_user:
        return
    support_chat_id = await get_support_chat_id(session)
    if message.chat.id != support_chat_id or not await is_support_agent(message.from_user.id, session):
        return

    original_message = message.reply_to_message
    if not original_message or not original_message.from_user or original_message.from_user.id != message.bot.id:
        await message.reply("❌ Ответьте на сообщение бота с заявкой пользователя.")
        return

    # ID находится в первой строке метаданных, сформированных самим ботом.
    # Якорь начала строки не позволяет подменить адресата текстом клиента.
    original_text = original_message.text or original_message.caption or ""
    user_id_match = re.match(rf"^{re.escape(SUPPORT_TARGET_PREFIX)}\s*(\d+)\b", original_text)
    if not user_id_match:
        await message.reply("❌ Не удалось определить адресата заявки.")
        return

    user_id = int(user_id_match.group(1))

    # Получаем пользователя из БД
    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        await message.reply("❌ Пользователь заявки не найден в базе данных.")
        return

    # Отправляем ответ пользователю
    try:
        response_content = message.text or message.caption or "[Медиа файл]"
        response_text = f"💬 <b>Ответ от поддержки:</b>\n\n{escape(response_content)}"

        if message.photo:
            # Если в ответе есть фото
            photo = message.photo[-1]  # Берем самое качественное фото
            await message.bot.send_photo(
                user_id,
                photo=photo.file_id,
                caption=response_text,
                parse_mode="HTML"
            )
        elif message.document:
            # Если в ответе есть документ
            await message.bot.send_document(
                user_id,
                document=message.document.file_id,
                caption=response_text,
                parse_mode="HTML"
            )
        elif message.video:
            # Если в ответе есть видео
            await message.bot.send_video(
                user_id,
                video=message.video.file_id,
                caption=response_text,
                parse_mode="HTML"
            )
        elif message.voice:
            # Если в ответе голосовое сообщение
            try:
                await message.bot.send_voice(
                    user_id,
                    voice=message.voice.file_id,
                    caption=response_text,
                    parse_mode="HTML"
                )
            except Exception as voice_error:
                # Если голосовые сообщения запрещены пользователем, отправляем текстовое сообщение
                if "VOICE_MESSAGES_FORBIDDEN" in str(voice_error):
                    fallback_text = "💬 <b>Ответ от поддержки:</b>\n\n"
                    if message.caption:
                        fallback_text += escape(message.caption)
                    else:
                        fallback_text += "Вам отправлено голосовое сообщение, но у вас отключены голосовые сообщения в настройках приватности Telegram.\n\nПожалуйста, включите голосовые сообщения в настройках приватности или обратитесь в поддержку другим способом."
                    await message.bot.send_message(
                        user_id,
                        fallback_text,
                        parse_mode="HTML"
                    )
                else:
                    # Если другая ошибка, пробрасываем её дальше
                    raise
        elif message.text:
            # Если это просто текст
            await message.bot.send_message(
                user_id,
                response_text,
                parse_mode="HTML"
            )
        else:
            await message.reply("❌ Неподдерживаемый тип сообщения.")
            return

        # Подтверждаем отправку
        user_name = escape(user.first_name or "N/A")
        username = escape(user.username or "N/A")
        await message.reply(f"✅ Ответ отправлен пользователю {user_name} (@{username})")

    except Exception as e:
        logger.error(f"Failed to send reply to user {user_id}: {e}")
        await message.reply("❌ Не удалось отправить ответ пользователю.")



async def forward_to_support_chat(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
):
    """Пересылка сообщения пользователя в чат поддержки"""
    if not message.from_user:
        return False

    user_id = message.from_user.id

    # Получаем пользователя из БД
    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        await edit_input_screen(
            message,
            state,
            "❌ Вы не зарегистрированы. Используйте /start.",
            reply_markup=get_menu_back_keyboard(),
        )
        return False

    support_chat_id = await get_support_chat_id(session)

    # Не пересылаем обращения в личные сообщения сотрудников: ответить можно
    # только из явно настроенного чата поддержки.
    if not support_chat_id:
        await edit_input_screen(
            message,
            state,
            "❌ Поддержка временно недоступна. Попробуйте позднее.",
            reply_markup=get_menu_back_keyboard(),
        )
        return False

    try:
        message_content = message.text or message.caption or "[Медиа файл]"
        first_name = escape(user.first_name or "N/A")
        username = escape(user.username or "не указан")
        admin_text = (
            f"<code>{SUPPORT_TARGET_PREFIX}{user.telegram_id}</code>\n"
            "💬 <b>Сообщение от пользователя</b>\n\n"
            f"👤 Пользователь: {first_name}\n"
            f"Username: @{username}\n"
            f"ID: {user.telegram_id}\n\n"
            f"📝 Сообщение:\n{escape(message_content)}\n\n"
            "↩️ <i>Ответьте на это сообщение, чтобы отправить ответ пользователю.</i>"
        )

        # Медиа пересылается отдельно, а на служебное сообщение бота сотрудник
        # отвечает reply — так адресат не определяется из пользовательского текста.
        if not message.text:
            await message.forward(support_chat_id)
        await message.bot.send_message(support_chat_id, admin_text, parse_mode="HTML")

        await edit_input_screen(
            message,
            state,
            "✅ Ваше сообщение отправлено в поддержку. Ожидайте ответа.",
            reply_markup=get_menu_back_keyboard(),
        )
        await state.clear()
        return True
    except Exception as e:
        logger.error(f"Failed to send message to support chat {support_chat_id}: {e}")
        await edit_input_screen(
            message,
            state,
            "❌ Не удалось отправить сообщение в поддержку. Попробуйте позже.",
            reply_markup=get_menu_back_keyboard(),
        )
        return False


@router.message(SupportStates.waiting_message)
async def handle_user_message(message: Message, session: AsyncSession, state: FSMContext):
    """Обработка сообщений от пользователей для поддержки"""
    # Проверяем, что это не команда
    if message.text and message.text.startswith('/'):
        return  # Пропускаем команды

    # Сотрудники поддержки не создают обращения через личный чат бота.
    if await is_support_agent(message.from_user.id, session):
        return

    # Пересылаем в поддержку
    await forward_to_support_chat(message, session, state)
