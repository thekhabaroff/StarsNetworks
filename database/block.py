"""Middleware для проверки блокировки пользователя"""
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy import select
from database.models import User
from bot import settings


class BlockedUserMiddleware(BaseMiddleware):
    """Middleware для проверки блокировки пользователя"""

    SUPPORT_STATE = "SupportStates:waiting_message"

    async def _is_allowed_for_blocked_user(
        self, event: Any, data: Dict[str, Any]
    ) -> bool:
        """Разрешить только явный inline-сценарий поддержки и финал Stars.

        Раньше любое текстовое или медиа-сообщение считалось обращением в
        поддержку. Из-за этого заблокированный пользователь мог завершить
        активный FSM-сценарий покупки. Теперь право на сообщение в поддержку
        появляется только после нажатия inline-кнопки «Поддержка».
        """
        if isinstance(event, CallbackQuery):
            return event.data == "menu_support"
        if not isinstance(event, Message):
            return False
        if event.successful_payment:
            # Платеж уже списан Telegram; завершить заказ необходимо даже если
            # блокировка была включена между pre-checkout и successful_payment.
            return True

        state = data.get("state")
        return bool(state and await state.get_state() == self.SUPPORT_STATE)

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        # Получаем user_id и проверяем тип события
        user_id = None
        if isinstance(event, Message):
            if event.from_user:
                user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            if event.from_user:
                user_id = event.from_user.id
        elif isinstance(event, PreCheckoutQuery):
            user_id = event.from_user.id

        if not user_id:
            return await handler(event, data)

        # Разработчиков нельзя заблокировать через бот. Администраторы, в том
        # числе перечисленные в ADMIN_IDS, подчиняются обычной блокировке.
        if user_id in settings.developer_ids_list:
            return await handler(event, data)

        # Проверяем блокировку в БД
        session = data.get("session")
        if session:
            stmt = select(User).where(User.telegram_id == user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and user.role == "developer":
                return await handler(event, data)

            if user and user.is_blocked:
                if await self._is_allowed_for_blocked_user(event, data):
                    return await handler(event, data)

                blocked_message = (
                    "❌ <b>Вы заблокированы</b>\n\n"
                    "Ваш доступ к боту ограничен администратором.\n"
                    "Если вы считаете, что это ошибка, обратитесь в поддержку."
                )

                if isinstance(event, PreCheckoutQuery):
                    await event.answer(
                        ok=False,
                        error_message="Оплата недоступна: ваш доступ к боту ограничен.",
                    )
                    return
                if isinstance(event, CallbackQuery):
                    try:
                        await event.message.edit_text(blocked_message, parse_mode="HTML")
                    except Exception:
                        await event.answer("Вы заблокированы", show_alert=True)
                    return
                else:
                    await event.answer(blocked_message, parse_mode="HTML")
                    return

        return await handler(event, data)
