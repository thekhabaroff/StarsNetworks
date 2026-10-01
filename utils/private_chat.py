"""Ограничение личных сценариев ботa личным чатом с пользователем.

Цифровой товар, баланс, заказы и реферальные данные нельзя безопасно
показывать в группе: Telegram доставляет callback в общий чат, а не в
приватный диалог пользователя. Этот middleware используется только у
пользовательских router-ов. Отдельный router поддержки в группе намеренно
не подключается к нему.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramAPIError
import logging

logger = logging.getLogger(__name__)


PRIVATE_CHAT_NOTICE = (
    "Для защиты ваших данных откройте личный чат с ботом и продолжите там."
)


def _event_chat_type(event: Any) -> ChatType | str | None:
    """Вернуть тип чата сообщения или callback, не доверяя отсутствующему чату."""
    if isinstance(event, CallbackQuery):
        chat = getattr(getattr(event, "message", None), "chat", None)
    elif isinstance(event, Message):
        chat = event.chat
    else:
        return None
    return getattr(chat, "type", None)


class PrivateChatMiddleware(BaseMiddleware):
    """Fail closed for menu callbacks and user input outside a private chat.

    ``successful_payment`` is the only exception. Telegram may deliver an
    already issued Stars invoice after this protection is deployed; refusing
    it would leave a real payment unfinished. Its handlers must send any
    confirmation directly to ``from_user.id`` rather than back to the source
    group.
    """

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)

        if _event_chat_type(event) == ChatType.PRIVATE:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer(PRIVATE_CHAT_NOTICE, show_alert=True)
        elif isinstance(event, Message):
            try:
                await event.answer(PRIVATE_CHAT_NOTICE)
            except TelegramAPIError as exc:
                # The bot may not be allowed to write in a group. The event is
                # still denied, and a send failure must not bypass the guard.
                logger.debug("Could not send private-chat guard notice: %s", exc, exc_info=True)
        return None


def apply_private_chat_guard(router: Router, *, protect_messages: bool = True) -> None:
    """Attach the same private-chat guard to a consumer router explicitly.

    Payment routers also receive Telegram ``successful_payment`` messages.
    They may safely pass the guard's narrowly scoped compatibility exception;
    their confirmations are delivered directly to the buyer's private chat.
    """
    # Use inner middleware deliberately. An outer observer middleware runs
    # before handler filters and would consume every group message while this
    # router is being traversed, including the separate group-support router.
    # Inner middleware runs only after one of this router's handlers matched.
    router.callback_query.middleware(PrivateChatMiddleware())
    if protect_messages:
        router.message.middleware(PrivateChatMiddleware())
