"""Single-screen navigation helpers for private FSM input flows."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


logger = logging.getLogger(__name__)

SCREEN_CHAT_ID_KEY = "_screen_chat_id"
SCREEN_MESSAGE_ID_KEY = "_screen_message_id"


class RememberScreenMiddleware(BaseMiddleware):
    """Remember the callback message when its handler opens an FSM input."""

    async def __call__(self, handler, event: CallbackQuery, data: dict[str, Any]):
        result = await handler(event, data)
        state: FSMContext | None = data.get("state")
        message = getattr(event, "message", None)
        if state is not None and message is not None and await state.get_state() is not None:
            await state.update_data(
                **{
                    SCREEN_CHAT_ID_KEY: message.chat.id,
                    SCREEN_MESSAGE_ID_KEY: message.message_id,
                }
            )
        return result


class DeleteProcessedInputMiddleware(BaseMiddleware):
    """Delete a user's FSM input after its selected handler has consumed it."""

    async def __call__(self, handler, event: Message, data: dict[str, Any]):
        state: FSMContext | None = data.get("state")
        state_before = await state.get_state() if state is not None else None
        try:
            return await handler(event, data)
        finally:
            if state_before is not None and event.chat.type == "private":
                try:
                    await event.delete()
                except TelegramBadRequest:
                    # The handler may already have deleted the input explicitly.
                    pass
                except Exception:
                    logger.debug("Could not delete processed FSM input", exc_info=True)


def apply_single_message_workflow(router: Router) -> None:
    """Attach screen tracking and automatic input cleanup to a router."""
    # Inner middleware runs only after this router has selected a handler.
    router.callback_query.middleware(RememberScreenMiddleware())
    router.message.middleware(DeleteProcessedInputMiddleware())


async def edit_input_screen(
    message: Message,
    state: FSMContext,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
    state_data: dict[str, Any] | None = None,
) -> None:
    """Replace the original FSM screen instead of sending another message."""
    data = state_data if state_data is not None else await state.get_data()
    chat_id = data.get(SCREEN_CHAT_ID_KEY)
    message_id = data.get(SCREEN_MESSAGE_ID_KEY)
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        raise RuntimeError("Исходное сообщение экрана не найдено. Откройте раздел заново.")
    try:
        await message.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
