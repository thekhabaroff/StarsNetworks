"""Automatically attach a close button to standalone bot notifications."""
from __future__ import annotations

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


CLOSE_NOTIFICATION_CALLBACK = "close_notification"


def close_notification_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Закрыть", callback_data=CLOSE_NOTIFICATION_CALLBACK)
    ]])


class CloseNotificationMiddleware(BaseRequestMiddleware):
    """Add Close to new text messages that do not already have navigation."""

    async def __call__(self, make_request, bot: Bot, method: TelegramMethod):
        if isinstance(method, SendMessage) and method.reply_markup is None:
            method.reply_markup = close_notification_keyboard()
        return await make_request(bot, method)
