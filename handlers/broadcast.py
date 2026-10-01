"""Обработчик рассылки"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import User
from bot import settings
from utils.single_message import apply_single_message_workflow, edit_input_screen
from utils.keyboards import get_back_keyboard, get_cancel_keyboard
import asyncio
import logging

logger = logging.getLogger(__name__)

router = Router()
apply_single_message_workflow(router)


class BroadcastStates(StatesGroup):
    """Состояния для рассылки"""
    waiting_message = State()
    waiting_user_id = State()


def is_admin(user_id: int) -> bool:
    """Быстрая проверка суперадминистраторов из окружения."""
    return user_id in settings.admin_ids_list or user_id in settings.developer_ids_list


async def is_admin_async(user_id: int, session: AsyncSession) -> bool:
    """Проверить роль администратора из окружения или базы данных."""
    if is_admin(user_id):
        return True

    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    return bool(user and user.role in ("admin", "developer"))


async def require_private_admin_callback(
    callback: CallbackQuery, session: AsyncSession
) -> bool:
    """Permit broadcast controls only to an admin in a private bot chat."""
    if callback.message.chat.type != "private":
        await callback.answer(
            "Рассылка доступна только в личном чате с ботом.",
            show_alert=True,
        )
        return False
    if not await is_admin_async(callback.from_user.id, session):
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return False
    return True


async def require_private_admin_message(
    message: Message, state: FSMContext, session: AsyncSession
) -> bool:
    """Close a broadcast state if somebody continues it outside a private chat."""
    if message.chat.type != "private":
        await state.clear()
        await message.answer("❌ Рассылка доступна только в личном чате с ботом.")
        return False
    if not await is_admin_async(message.from_user.id, session):
        await state.clear()
        await message.answer("❌ Доступ запрещен")
        return False
    return True


def get_broadcast_keyboard():
    """Inline-клавиатура раздела рассылки."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Массовая рассылка", callback_data="broadcast_mass")],
        [InlineKeyboardButton(text="👤 Индивидуальная рассылка", callback_data="broadcast_individual")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")]
    ])


@router.callback_query(F.data == "broadcast_menu")
async def broadcast_menu_callback(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Открыть рассылку из inline-главного меню."""
    if not await require_private_admin_callback(callback, session):
        return

    await state.clear()
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Выберите тип рассылки:",
        reply_markup=get_broadcast_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "broadcast_mass")
async def broadcast_mass_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать массовую рассылку"""
    if not await require_private_admin_callback(callback, session):
        return

    await state.update_data(broadcast_type="mass")
    await state.set_state(BroadcastStates.waiting_message)

    await callback.message.edit_text(
        "📢 <b>Массовая рассылка</b>\n\n"
        "Отправьте сообщение для рассылки всем пользователям:",
        reply_markup=get_cancel_keyboard("admin_menu"),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "broadcast_individual")
async def broadcast_individual_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать индивидуальную рассылку"""
    if not await require_private_admin_callback(callback, session):
        return

    await state.update_data(broadcast_type="individual")
    await state.set_state(BroadcastStates.waiting_user_id)

    await callback.message.edit_text(
        "👤 <b>Индивидуальная рассылка</b>\n\n"
        "Введите ID пользователя:",
        reply_markup=get_cancel_keyboard("broadcast_menu"),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_user_id)
async def process_user_id(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка ID пользователя для индивидуальной рассылки"""
    if not await require_private_admin_message(message, state, session):
        return

    try:
        user_id = int(message.text)
        await state.update_data(target_user_id=user_id)
        await edit_input_screen(
            message,
            state,
            "👤 <b>Индивидуальная рассылка</b>\n\n"
            "Отправьте сообщение для пользователя:",
            reply_markup=get_cancel_keyboard("broadcast_menu"),
        )
        await state.set_state(BroadcastStates.waiting_message)
    except ValueError:
        await edit_input_screen(
            message,
            state,
            "👤 <b>Индивидуальная рассылка</b>\n\n"
            "❌ Введите корректный числовой ID пользователя:",
            reply_markup=get_cancel_keyboard("broadcast_menu"),
        )


async def send_broadcast_message(
    bot,
    user_id: int,
    message_text: str,
    message_photo: str = None,
    message_document: str = None
):
    """Отправить сообщение пользователю"""
    try:
        if message_photo:
            await bot.send_photo(user_id, message_photo, caption=message_text)
        elif message_document:
            await bot.send_document(user_id, message_document, caption=message_text)
        else:
            await bot.send_message(user_id, message_text)
        return True
    except Exception as e:
        logger.error(f"Error sending message to user {user_id}: {e}")
        return False


@router.message(BroadcastStates.waiting_message)
async def process_broadcast_message(message: Message, state: FSMContext, session: AsyncSession):
    """Обработка сообщения для рассылки"""
    if not await require_private_admin_message(message, state, session):
        return

    data = await state.get_data()
    broadcast_type = data.get("broadcast_type")

    if not broadcast_type:
        await edit_input_screen(
            message,
            state,
            "❌ Данные рассылки не найдены. Начните заново.",
            reply_markup=get_back_keyboard("admin_menu"),
            state_data=data,
        )
        await state.clear()
        return

    if broadcast_type == "mass":
        # Массовая рассылка
        await edit_input_screen(
            message,
            state,
            "📢 <b>Массовая рассылка</b>\n\nРассылка выполняется…",
            reply_markup=get_back_keyboard("admin_menu"),
            state_data=data,
        )

        stmt = select(User.telegram_id).where(User.is_blocked.is_(False))
        users = list((await session.scalars(stmt)).all())
        # Sending may take minutes. Release the DB connection/transaction
        # before the network loop so broadcast cannot starve payments.
        await session.rollback()

        total = len(users)
        success = 0
        failed = 0

        # Throttling: не более 25 сообщений в секунду
        throttle = max(1, min(int(settings.BROADCAST_THROTTLE), 25))
        throttle_delay = 1.0 / throttle

        for telegram_id in users:
            try:
                # Определяем тип сообщения
                if message.photo:
                    sent = await send_broadcast_message(
                        message.bot,
                        telegram_id,
                        message.caption or "",
                        message_photo=message.photo[-1].file_id
                    )
                elif message.document:
                    sent = await send_broadcast_message(
                        message.bot,
                        telegram_id,
                        message.caption or "",
                        message_document=message.document.file_id
                    )
                else:
                    sent = await send_broadcast_message(
                        message.bot,
                        telegram_id,
                        message.text
                    )
                if sent:
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("Error sending broadcast to user %s: %s", telegram_id, exc)
                failed += 1

            # Throttling
            await asyncio.sleep(throttle_delay)

        await edit_input_screen(
            message,
            state,
            f"✅ Рассылка завершена!\n"
            f"Всего: {total}\n"
            f"Успешно: {success}\n"
            f"Ошибок: {failed}",
            reply_markup=get_back_keyboard("broadcast_menu"),
            state_data=data,
        )

    elif broadcast_type == "individual":
        # Индивидуальная рассылка
        target_user_id = data.get("target_user_id")

        try:
            if message.photo:
                sent = await send_broadcast_message(
                    message.bot,
                    target_user_id,
                    message.caption or "",
                    message_photo=message.photo[-1].file_id
                )
            elif message.document:
                sent = await send_broadcast_message(
                    message.bot,
                    target_user_id,
                    message.caption or "",
                    message_document=message.document.file_id
                )
            else:
                sent = await send_broadcast_message(
                    message.bot,
                    target_user_id,
                    message.text
                )

            if sent:
                result_text = f"✅ Сообщение отправлено пользователю {target_user_id}"
            else:
                result_text = "❌ Не удалось отправить сообщение пользователю"
        except Exception:
            logger.exception("Error sending individual broadcast to user %s", target_user_id)
            result_text = "❌ Не удалось отправить сообщение пользователю."

        await edit_input_screen(
            message,
            state,
            result_text,
            reply_markup=get_back_keyboard("broadcast_menu"),
            state_data=data,
        )

    await state.clear()
