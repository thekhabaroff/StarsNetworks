"""Подключение к базе данных"""
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from bot import settings
import logging

logger = logging.getLogger(__name__)

# Создаем движок БД
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True
)

# Создаем фабрику сессий
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()


async def database_is_ready() -> bool:
    """Проверить БД и права прикладной роли без изменения схемы.

    Схемой управляет только Alembic в отдельном Compose-сервисе ``migrate``.
    Приложение имеет ограниченную роль и не должно выполнять DDL/create_all.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            # The latest migration creates the ledger. Checking it catches a
            # wrong DATABASE_URL, a missing migration, or an app role that
            # received no grants; a bare SELECT 1 would call all three cases
            # healthy.
            await connection.execute(text("SELECT 1 FROM balance_ledger LIMIT 1"))
            if connection.dialect.name == "postgresql":
                grants = await connection.execute(
                    text(
                        """
                        SELECT
                            has_table_privilege(current_user, 'public.users', 'INSERT')
                            AND has_table_privilege(current_user, 'public.users', 'UPDATE')
                            AND has_table_privilege(current_user, 'public.users', 'DELETE')
                            AND has_table_privilege(current_user, 'public.balance_ledger', 'SELECT')
                            AND has_table_privilege(current_user, 'public.balance_ledger', 'INSERT')
                            AND COALESCE(
                                has_sequence_privilege(
                                    current_user,
                                    pg_get_serial_sequence('public.balance_ledger', 'id'),
                                    'USAGE'
                                ),
                                false
                            )
                            AS app_dml_ready
                        """
                    )
                )
                if not grants.scalar_one():
                    logger.warning("Database role lacks required application DML grants")
                    return False
        return True
    except Exception as exc:
        logger.warning("Database readiness check failed: %s", exc)
        return False


async def get_session() -> AsyncSession:
    """Получить сессию БД"""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


class DatabaseMiddleware(BaseMiddleware):
    """Добавлять сессию БД в данные обработки Telegram-события."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        async with async_session_maker() as session:
            data["session"] = session
            return await handler(event, data)
