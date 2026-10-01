"""Handlers shared by public, admin and support messages."""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from utils.close_button import CLOSE_NOTIFICATION_CALLBACK


router = Router()


@router.callback_query(F.data == CLOSE_NOTIFICATION_CALLBACK)
async def close_notification(callback: CallbackQuery) -> None:
    """Delete a standalone notification on explicit user request."""
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        await callback.answer("Сообщение уже удалено")
        return
    await callback.answer()
