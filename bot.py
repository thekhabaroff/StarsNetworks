"""Главный файл бота"""
from __future__ import annotations

import asyncio
import logging
import sys
from decimal import Decimal
from typing import List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# При запуске `python bot.py` другие модули импортируют настройки по имени
# `bot`. Связываем это имя с уже исполняемым модулем, чтобы не запускать файл
# второй раз и не создать циклический импорт.
if __name__ == "__main__":
    sys.modules.setdefault("bot", sys.modules[__name__])

load_dotenv()


class Settings(BaseSettings):
    """Настройки приложения из файла .env и переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    BOT_TOKEN: str
    # Username is used only for referral links; Telegram remains the source
    # of truth and fills it at startup when the variable is omitted.
    BOT_USERNAME: str = ""
    DATABASE_URL: str = "postgresql+asyncpg://starsnetworks_app:starsnetworks_app@db:5432/starsnetworks"
    ADMIN_IDS: str = ""
    DEVELOPER_IDS: str = ""

    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""
    HELEKET_MERCHANT_ID: str = ""
    HELEKET_API_KEY: str = ""
    LAVA_ID: str = ""
    LAVA_SECRET_KEY: str = ""
    LAVA_ADDITIONAL_KEY: str = ""
    LAVA_WEBHOOK_SECRET: str = ""
    CRYPTOBOT_API: str = ""
    # Alias used by Crypto Pay documentation; CRYPTOBOT_API remains supported
    # for compatibility with the existing admin panel and deployments.
    CRYPTOBOT_API_TOKEN: str = ""
    YOOMONEY_WALLET: str = ""
    YOOMONEY_NOTIFICATION_SECRET: str = ""
    PAYMENT_YOOMONEY_ENABLED: bool = False
    PAYMENT_YOOKASSA_ENABLED: bool = False
    PAYMENT_HELEKET_ENABLED: bool = False
    PAYMENT_LAVA_ENABLED: bool = False
    PAYMENT_CRYPTOBOT_ENABLED: bool = False
    PAYMENT_STARS_ENABLED: bool = True
    # Курсы и порядок методов оплаты хранятся в таблице settings и могут
    # меняться из пункта управления без перезапуска контейнера.
    PAYMENT_RATE_SYNC_ENABLED: bool = True
    # Курсы участвуют в расчёте суммы Telegram Stars. Храним их как Decimal,
    # чтобы ручная настройка и синхронизация ЦБ не возвращали float в
    # финансовую логику.
    PAYMENT_USD_RATE: Decimal = Decimal("0")
    PAYMENT_EUR_RATE: Decimal = Decimal("0")
    PAYMENT_STARS_RATE: Decimal = Decimal("2.3")
    PAYMENT_METHOD_ORDER: str = "stars,yookassa,yoomoney,lava,heleket,cryptobot"

    # Fragment delivery settings.  The API is intentionally optional at
    # startup: payments and the catalog remain available, while a paid order
    # is marked for retry until valid credentials are supplied.
    FRAGMENT_API_KEY: str = ""
    FRAGMENT_PHONE: str = ""
    FRAGMENT_PHONE_NUMBER: str = ""
    FRAGMENT_MNEMONICS: str = ""
    # Token of a Fragment connection created in the dashboard.  The current
    # API requires this token for purchases; the seed phrase is only used once
    # while creating the connection and is not needed by the bot afterwards.
    FRAGMENT_CONNECTION_TOKEN: str = ""
    FRAGMENT_WALLET_VERSION: str = "W5"
    FRAGMENT_API_URL: str = "https://api.fragment-api.com/v1"
    FRAGMENT_TOKEN_FILE: str = "/data/auth_token.json"
    # Automatic retail pricing. Fragment's public displayed amount is the
    # base cost (including the network amount shown by Fragment); the API fee
    # is added before the configured store markup.
    FRAGMENT_API_FEE_PERCENT: Decimal = Decimal("0.5")
    FRAGMENT_PRICING_CACHE_SECONDS: int = 300
    STARS_PRODUCT_PRICE_RUBLES: Decimal = Decimal("1.50")
    PREMIUM_3_MONTHS_PRICE_RUBLES: Decimal = Decimal("1200.00")
    PREMIUM_6_MONTHS_PRICE_RUBLES: Decimal = Decimal("1600.00")
    PREMIUM_12_MONTHS_PRICE_RUBLES: Decimal = Decimal("2900.00")
    STARS_MAX_QUANTITY: int = 1_000_000

    SUPPORT_ID: str = ""
    COMMUNITY_ID: str = ""
    TERMS_OF_SERVICE: str = ""
    PRIVACY_POLICY: str = ""

    WEBHOOK_URL: str = ""
    PAYMENT_PUBLIC_BASE_URL: str = ""
    TELEGRAM_WEBHOOK_SECRET_TOKEN: str = ""
    PAYMENT_WEBHOOK_PORT: int = 18743

    ORDER_RESERVATION_MINUTES: int = 15
    BROADCAST_THROTTLE: int = 25

    @property
    def admin_ids_list(self) -> List[int]:
        """Список ID администраторов."""
        if not self.ADMIN_IDS:
            return []
        return [int(uid.strip()) for uid in self.ADMIN_IDS.split(",") if uid.strip().isdigit()]

    @property
    def developer_ids_list(self) -> List[int]:
        """Список ID разработчиков."""
        if not self.DEVELOPER_IDS:
            return []
        return [int(uid.strip()) for uid in self.DEVELOPER_IDS.split(",") if uid.strip().isdigit()]

settings = Settings()

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

# Эти модули подключаются только при запуске приложения. Это позволяет
# миграциям и моделям импортировать `settings` без циклического импорта.
if __name__ == "__main__":
    from database.db import database_is_ready
    from handlers import (
        common, start, cart, catalog, orders, balance, referral, info, payment, admin, broadcast,
    )
    from handlers.webhook import create_webhook_app
from utils.logger import logger

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    # Docker collects stdout/stderr and rotates it according to Compose.
    # Writing a second file would conflict with the read-only application
    # filesystem used by the production container.
    handlers=[logging.StreamHandler(sys.stdout)]
)


async def cancel_expired_orders(bot: Bot):
    """Автоматически снять истекшие резервы одной транзакцией на заказ."""
    from database.db import async_session_maker
    from database.models import Order, OrderBatch, User
    from utils.checkout import CheckoutError, cancel_pending_batch, cancel_pending_order
    from sqlalchemy import select
    from datetime import datetime

    while True:
        try:
            async with async_session_maker() as session:
                # ID читаются без доверия к состоянию: каждый из них затем
                # повторно блокируется в cancel_pending_order.
                now = datetime.now()
                batch_stmt = select(OrderBatch.id).where(
                    OrderBatch.status == "PENDING_PAYMENT",
                    OrderBatch.reserved_until < now,
                )
                expired_batch_ids = (await session.execute(batch_stmt)).scalars().all()
                batch_notifications: list[tuple[int, int]] = []
                cancelled_count = 0
                for batch_id in expired_batch_ids:
                    try:
                        batch = await cancel_pending_batch(session, batch_id)
                    except CheckoutError:
                        continue
                    cancelled_count += 1
                    user = await session.get(User, batch.user_id)
                    if user:
                        batch_notifications.append((user.telegram_id, batch.id))

                stmt = select(Order.id).where(
                    Order.status == "ОЖИДАЕТ ОПЛАТЫ",
                    Order.reserved_until < now
                )
                result = await session.execute(stmt)
                expired_order_ids = result.scalars().all()
                notifications: list[tuple[int, int]] = []

                for order_id in expired_order_ids:
                    try:
                        order = await cancel_pending_order(session, order_id)
                    except CheckoutError:
                        # Платеж или ручная отмена успели изменить заказ после
                        # первичной выборки; это штатная гонка, не ошибка.
                        continue
                    cancelled_count += 1
                    user = await session.get(User, order.user_id)
                    if user:
                        notifications.append((user.telegram_id, order.id))

                if cancelled_count:
                    await session.commit()
                    logger.info("Cancelled %s expired orders", cancelled_count)

                for telegram_id, batch_id in batch_notifications:
                    try:
                        await bot.send_message(
                            telegram_id,
                            "⏰ <b>Корзина отменена</b>\n\n"
                            f"Оформление #{batch_id} отменено из-за истечения срока ожидания оплаты "
                            f"({settings.ORDER_RESERVATION_MINUTES} минут).\n\n"
                            "✅ Зарезервированные товары возвращены в каталог.",
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        logger.warning("Could not notify about expired batch %s: %s", batch_id, exc)

                # Сеть не удерживает транзакцию БД: при ошибке доставки товар
                # уже корректно возвращен, а пользователь увидит это в заказах.
                for telegram_id, order_id in notifications:
                    try:
                        await bot.send_message(
                            telegram_id,
                            "⏰ <b>Заказ отменен</b>\n\n"
                            f"Заказ #{order_id} был отменен из-за истечения "
                            f"срока ожидания оплаты ({settings.ORDER_RESERVATION_MINUTES} минут).\n\n"
                            "✅ Товар возвращен в каталог.\n\n"
                            "Вы можете создать новый заказ.",
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        logger.warning("Could not notify about expired order %s: %s", order_id, exc)

        except Exception as e:
            logger.error(f"Error in cancel_expired_orders: {e}")

        # Проверяем каждые 5 минут
        await asyncio.sleep(300)


async def sync_fragment_prices_periodically() -> None:
    """Keep automatic Stars/Premium retail prices aligned with Fragment."""
    from database.db import async_session_maker
    from utils.pricing import sync_cost_plus_prices

    while True:
        try:
            async with async_session_maker() as session:
                changed = await sync_cost_plus_prices(session)
            if changed:
                logger.info("Refreshed %s automatic Fragment product prices", changed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not refresh automatic Fragment product prices", exc_info=True)
        await asyncio.sleep(300)


async def reconcile_pending_payments(bot: Bot):
    """Сверять внешние счета и истёкшие пополнения без надежды на webhook.

    Вебхуки остаются самым быстрым путём, но поставщик или сеть могут потерять
    callback. Периодическая сверка получает финальный статус из API поставщика
    и завершает ту же идемпотентную транзакцию, что и webhook.
    """
    from database.db import async_session_maker
    from utils.checkout import (
        expire_pending_stars_topups,
        reconcile_pending_external_payments,
    )
    from utils.fulfillment import deliver_completed_order
    from utils.notifications import notify_balance_topup

    while True:
        try:
            async with async_session_maker() as session:
                reconciled = await reconcile_pending_external_payments(session)
                expired_stars_topups = await expire_pending_stars_topups(session)

            if expired_stars_topups:
                logger.info("Expired %s unconfirmed Telegram Stars topups", expired_stars_topups)
            for item in reconciled:
                completed_items = []
                if item.completed_order is not None:
                    completed_items.append(item.completed_order)
                if item.completed_orders:
                    completed_items.extend(item.completed_orders)
                if completed_items:
                    for completed in completed_items:
                        if completed.already_completed:
                            continue
                        try:
                            await deliver_completed_order(bot, completed)
                        except Exception:
                            logger.exception(
                                "Could not deliver reconciled order %s",
                                completed.order.id,
                            )
                elif item.topup_user is not None and item.topup_amount is not None:
                    try:
                        await notify_balance_topup(
                            None,
                            item.topup_user,
                            item.topup_amount,
                            bot,
                        )
                    except Exception:
                        logger.exception(
                            "Could not notify reconciled topup %s", item.payment_id
                        )
            if reconciled:
                logger.info("Reconciled %s external payment(s)", len(reconciled))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error while reconciling pending payments")

        # This bounds recovery from a lost webhook to one minute without
        # retaining database locks during an external provider request.
        await asyncio.sleep(60)


async def sync_roles_from_env(bot: Bot):
    """Синхронизация ролей пользователей из .env в БД"""
    from database.db import async_session_maker
    from database.models import User
    from sqlalchemy import select

    async with async_session_maker() as session:
        # Обновляем роли для всех пользователей из .env
        all_admin_ids = set(settings.admin_ids_list + settings.developer_ids_list)

        for user_id in all_admin_ids:
            try:
                stmt = select(User).where(User.telegram_id == user_id)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()

                if user:
                    # Устанавливаем роль на основе .env
                    if user_id in settings.developer_ids_list:
                        if user.role != "developer":
                            user.role = "developer"
                            logger.info(f"Updated role to 'developer' for user {user_id}")
                    elif user_id in settings.admin_ids_list:
                        if user.role != "admin":
                            user.role = "admin"
                            logger.info(f"Updated role to 'admin' for user {user_id}")
                else:
                    # Пользователь еще не зарегистрирован - роль будет установлена при регистрации
                    logger.debug(f"User {user_id} from .env not yet registered")

            except Exception as e:
                logger.error(f"Error syncing role for user {user_id}: {e}")

        await session.commit()


async def setup_support_chat(bot: Bot):
    """Настройка чата поддержки"""
    from database.db import async_session_maker
    from database.models import Setting
    from sqlalchemy import select

    async with async_session_maker() as session:
        # Проверяем, есть ли уже настройка для support_chat_id
        stmt = select(Setting).where(Setting.key == "support_chat_id")
        result = await session.execute(stmt)
        setting = result.scalar_one_or_none()

        support_chat_id = None
        if setting and setting.value:
            try:
                support_chat_id = int(setting.value)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Ignoring invalid support_chat_id setting %r: %s",
                    setting.value,
                    exc,
                )

        # Если ID чата не указан, отправляем инструкцию администратору
        if not support_chat_id and settings.admin_ids_list:
            instruction_text = """📋 <b>Настройка чата поддержки</b>

Для настройки системы поддержки выполните следующие шаги:

1. Создайте группу в Telegram (или используйте существующую)
2. Добавьте бота в группу как администратора
3. Администратор или разработчик из .env отправляет в этой группе команду:
   <code>/set_support_chat</code>

После настройки, сообщения от пользователей будут автоматически пересылаться в этот чат."""

            # Отправляем инструкцию всем администраторам
            for admin_id in settings.admin_ids_list:
                try:
                    await bot.send_message(
                        admin_id,
                        instruction_text,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent support chat setup instruction to admin {admin_id}")
                except Exception as e:
                    # Игнорируем ошибку, если пользователь не начал диалог с ботом
                    error_str = str(e).lower()
                    if "unauthorized" in error_str or "chat not found" in error_str or "bot was blocked" in error_str:
                        logger.warning(f"Admin {admin_id} has not started a conversation with the bot or blocked it. Skipping instruction.")
                    else:
                        logger.error(f"Failed to send instruction to admin {admin_id}: {e}")
        else:
            # Проверяем доступность чата
            if support_chat_id:
                try:
                    chat = await bot.get_chat(support_chat_id)
                    logger.info(f"Support chat configured: {chat.title} (ID: {support_chat_id})")
                except Exception as e:
                    logger.warning(f"Support chat ID {support_chat_id} is not accessible: {e}")
                    # Сбрасываем неверный ID
                    if setting:
                        setting.value = ""
                        await session.commit()


async def start_payment_webhook_server(bot: Bot, dispatcher: Dispatcher = None):
    """Запустить внутренний HTTP-сервер; TLS всегда завершает Nginx на хосте."""
    from aiohttp import web

    runner = None
    try:
        app = create_webhook_app(bot, dispatcher)
        runner = web.AppRunner(app)
        await runner.setup()

        # Контейнер слушает внутренний HTTP. Публичный HTTPS, сертификаты и
        # перенаправление HTTP→HTTPS принадлежат только Nginx на хосте.
        site = web.TCPSite(runner, "0.0.0.0", settings.PAYMENT_WEBHOOK_PORT)
        await site.start()
        # HTTP уже принимает соединения, но readiness остаётся отрицательной
        # до завершения запуска. В webhook-режиме это также гарантирует, что
        # Telegram webhook зарегистрирован до того, как Docker объявит
        # приложение готовым.
        bot._webhook_app = app

        base_url = f"http://0.0.0.0:{settings.PAYMENT_WEBHOOK_PORT}"
        logger.info("Internal webhook server started on %s", base_url)
        if dispatcher:
            logger.info("  - Telegram webhook: %s/webhook/telegram", base_url)
        logger.info("  - YooKassa webhook: %s/webhook/yookassa", base_url)
        logger.info("  - ЮMoney webhook: %s/webhook/yoomoney", base_url)
        logger.info("  - Heleket webhook: %s/webhook/heleket", base_url)
        logger.info("  - Lava webhook: %s/webhook/lava", base_url)
        logger.info("  - CryptoBot webhook: %s/webhook/cryptobot", base_url)
        logger.info("  - Health check: %s/health", base_url)
        return runner
    except Exception as exc:
        if runner is not None:
            await runner.cleanup()
        logger.exception("Failed to start payment webhook server")
        raise RuntimeError("Payment webhook server did not start") from exc


async def on_startup(bot: Bot, dispatcher: Dispatcher | None = None):
    """Действия при запуске бота"""
    logger.info("Bot starting up...")

    # Проверяем токен бота через get_me()
    try:
        bot_info = await bot.get_me()
        logger.info(f"Bot token verified. Bot: @{bot_info.username} (ID: {bot_info.id})")
        if not settings.BOT_USERNAME and bot_info.username:
            settings.BOT_USERNAME = bot_info.username
    except Exception as e:
        error_str = str(e).lower()
        if "unauthorized" in error_str:
            logger.error("Bot token is invalid or expired! Please check your BOT_TOKEN in .env file.")
            raise Exception(f"Invalid bot token: {e}")
        else:
            logger.error(f"Failed to verify bot token: {e}")
            raise

    # Стандартное меню Telegram, которое открывается при вводе «/».
    # Пользовательские сценарии бота доступны только в личном чате, поэтому
    # команды публикуются в соответствующей области видимости.
    await bot.set_my_commands(
        commands=[
            BotCommand(command="start", description="перезапустить бота"),
            BotCommand(command="clear", description="чистить историю сообщений"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    logger.info("Telegram private-chat command menu configured")

    # Схема уже подготовлена отдельным сервисом Alembic. До запуска webhook
    # убеждаемся, что ограниченная роль приложения действительно подключается.
    if not await database_is_ready():
        raise RuntimeError("Database is not ready for the application role")
    logger.info("Database readiness confirmed")

    # Реквизиты внешних оплат, сохранённые разработчиком из панели, должны
    # применяться до расчёта каталога: курс USD/RUB участвует в себестоимости.
    from database.db import async_session_maker
    from utils.payments import load_payment_settings
    async with async_session_maker() as session:
        await load_payment_settings(session)
    logger.info("Payment settings loaded")

    # The migration seeds the item for a fresh database; this idempotent
    # repair also handles upgrades and guarantees no legacy product leaks into
    # the customer-facing catalog.
    from database.db import async_session_maker
    from utils.pricing import sync_cost_plus_prices
    from utils.stars_catalog import ensure_premium_catalog, ensure_stars_catalog
    async with async_session_maker() as session:
        await ensure_stars_catalog(session)
        await ensure_premium_catalog(session)
        corrected_prices = await sync_cost_plus_prices(session)
    if corrected_prices:
        logger.info("Synchronized %s cost-plus product prices", corrected_prices)
    logger.info("Telegram Stars and Premium catalog ensured")

    # До появления этих строк в .env источники сообщества и поддержки были
    # значениями по умолчанию в коде. Сохраняем их в .env один раз, чтобы
    # дальнейшее редактирование из пункта управления было прозрачным.
    from utils.envfile import ensure_default_interaction_sources
    try:
        ensure_default_interaction_sources()
    except (OSError, RuntimeError) as exc:
        logger.warning("Could not initialize interaction sources in .env: %s", exc)

    # Синхронизация ролей из .env в БД
    await sync_roles_from_env(bot)
    logger.info("Roles synchronized from .env")

    # Запускаем задачу автоматической отмены заказов
    asyncio.create_task(cancel_expired_orders(bot))
    logger.info("Expired orders cancellation task started")

    asyncio.create_task(reconcile_pending_payments(bot))
    logger.info("Pending payment reconciliation task started")

    # ЦБ публикует USD/EUR раз в рабочий день. Синхронизация выполняется в
    # фоне и не задерживает запуск бота или обработку Telegram webhook.
    from utils.payment_rates import sync_payment_rates_periodically
    asyncio.create_task(sync_payment_rates_periodically())
    logger.info("Payment rates synchronization task started")

    asyncio.create_task(sync_fragment_prices_periodically())
    logger.info("Automatic Fragment pricing task started")

    # Запускаем HTTP сервер для платежных систем и, в webhook-режиме, Telegram.
    webhook_runner = await start_payment_webhook_server(bot, dispatcher)
    # Сохраняем runner в bot для доступа при завершении.
    bot._webhook_runner = webhook_runner

    # Удаляем webhook, если используется polling режим (webhook будет установлен в main() для webhook режима)
    if not settings.WEBHOOK_URL:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Polling mode: webhook deleted")
        except Exception as e:
            # Игнорируем ошибку, если webhook не был установлен или токен неверный
            error_str = str(e).lower()
            if "unauthorized" in error_str:
                logger.error(f"Bot token is invalid! Cannot delete webhook. Error: {e}")
                raise
            else:
                logger.warning(f"Could not delete webhook (non-critical): {e}")

    # В polling-режиме Telegram webhook не регистрируется, но внутренний
    # HTTP-сервер и dispatcher уже готовы обслуживать платёжные callbacks.
    if not settings.WEBHOOK_URL and hasattr(bot, "_webhook_app"):
        bot._webhook_app["runtime_state"]["ready"] = True


async def on_shutdown(bot: Bot):
    """Действия при остановке бота"""
    logger.info("Bot shutting down...")

    # Останавливаем webhook сервер для платежных систем
    if hasattr(bot, '_webhook_runner'):
        try:
            await bot._webhook_runner.cleanup()
            logger.info("Payment webhook server stopped")
        except Exception as e:
            logger.warning(f"Error stopping payment webhook server: {e}")

    # Не удаляем Telegram webhook при обычном рестарте контейнера. Иначе
    # короткое обновление образа создаёт окно, когда Telegram не доставляет
    # обновления до следующего успешного setWebhook. В polling-режиме webhook
    # уже удалён явно на startup.
    try:
        await bot.session.close()
    except Exception as e:
        logger.warning(f"Error closing bot session: {e}")


async def main():
    """Главная функция"""
    # Проверка токена
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN not set in environment variables!")
        sys.exit(1)
    if settings.WEBHOOK_URL and not settings.TELEGRAM_WEBHOOK_SECRET_TOKEN:
        raise RuntimeError(
            "TELEGRAM_WEBHOOK_SECRET_TOKEN is required when WEBHOOK_URL is configured"
        )

    # Создание бота и диспетчера
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    from utils.close_button import CloseNotificationMiddleware
    bot.session.middleware(CloseNotificationMiddleware())

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Регистрация роутеров (порядок важен!)
    # Сначала регистрируем специфичные обработчики (кнопки меню, команды)
    dp.include_router(common.router)
    dp.include_router(start.router)
    dp.include_router(admin.router)  # Админ-панель раньше общего обработчика сообщений
    dp.include_router(broadcast.router)  # Рассылка раньше общего обработчика сообщений
    dp.include_router(cart.router)
    dp.include_router(catalog.router)
    dp.include_router(orders.router)
    dp.include_router(balance.router)
    dp.include_router(referral.router)
    dp.include_router(payment.router)
    # Общий обработчик сообщений (поддержка) должен быть последним
    dp.include_router(info.router)

    # Регистрация middleware
    from database.db import DatabaseMiddleware
    from database.block import BlockedUserMiddleware

    # Middleware для получения сессии БД
    dp.message.middleware(DatabaseMiddleware())
    dp.callback_query.middleware(DatabaseMiddleware())
    dp.pre_checkout_query.middleware(DatabaseMiddleware())

    # Middleware для проверки блокировки (после DatabaseMiddleware, чтобы session был доступен)
    dp.message.middleware(BlockedUserMiddleware())
    dp.callback_query.middleware(BlockedUserMiddleware())
    dp.pre_checkout_query.middleware(BlockedUserMiddleware())

    # Обработчик ошибок через декоратор (резервный)
    # В aiogram 3.x обработчик получает ErrorEvent
    @dp.errors()
    async def error_handler(event):
        """Обработчик ошибок для aiogram 3.x (резервный)"""
        import traceback
        from aiogram.types import ErrorEvent

        # В aiogram 3.x event может быть ErrorEvent или просто exception
        if isinstance(event, ErrorEvent):
            exception = event.exception
            update = event.update
        elif hasattr(event, 'exception'):
            exception = event.exception
            update = getattr(event, 'update', None)
        else:
            # Если это просто exception
            exception = event
            update = None

        # Игнорируем некритичные сетевые ошибки
        error_str = str(exception).lower()
        if any(phrase in error_str for phrase in [
            "timeout", "таймаут", "семафора", "semaphore",
            "connection", "соединение", "network"
        ]):
            # Сетевые ошибки - не критичны, просто логируем
            logger.warning(f"Network error (non-critical): {exception}")
            return

        # Игнорируем ошибку "message is not modified"
        if "message is not modified" in error_str:
            return

        logger.error(f"Error handler called: {type(exception).__name__}: {exception}", exc_info=exception)

        try:
            from utils.logger import log_error_to_db
            from database.db import async_session_maker

            async with async_session_maker() as session:
                user_id = None

                # Получаем user_id из update
                if update:
                    if update.message and update.message.from_user:
                        user_id = update.message.from_user.id
                    elif update.callback_query and update.callback_query.from_user:
                        user_id = update.callback_query.from_user.id
                    elif update.edited_message and update.edited_message.from_user:
                        user_id = update.edited_message.from_user.id
                    elif update.channel_post and update.channel_post.sender_chat:
                        user_id = update.channel_post.sender_chat.id

                tb_str = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
                await log_error_to_db(
                    session,
                    "ERROR",
                    str(exception),
                    user_id=user_id,
                    traceback=tb_str
                )
        except Exception as e:
            logger.error(f"Error logging to DB: {e}")

    # Запуск бота
    # Автоматический выбор режима: webhook (если WEBHOOK_URL установлен) или polling

    if settings.WEBHOOK_URL:
        # ========== WEBHOOK РЕЖИМ (для production) ==========
        logger.info("Starting bot in WEBHOOK mode")

        try:
            # Выполняем startup действия и сразу запускаем сервер с dispatcher.
            await on_startup(bot, dp)

            # Устанавливаем webhook URL в Telegram
            try:
                await bot.set_webhook(
                    url=settings.WEBHOOK_URL,
                    allowed_updates=dp.resolve_used_update_types(),
                    secret_token=settings.TELEGRAM_WEBHOOK_SECRET_TOKEN,
                )
                if hasattr(bot, "_webhook_app"):
                    bot._webhook_app["runtime_state"]["ready"] = True
                logger.info(f"Webhook set to {settings.WEBHOOK_URL}")
            except Exception as e:
                logger.error(f"Failed to set webhook: {e}")
                raise

            # Ожидаем бесконечно (сервер работает в фоне)
            logger.info("Bot is running in webhook mode. Press Ctrl+C to stop.")
            try:
                await asyncio.Event().wait()  # Ожидаем бесконечно
            except KeyboardInterrupt:
                logger.info("Received shutdown signal")
            finally:
                await on_shutdown(bot)

        except Exception as e:
            logger.error(f"Error in webhook mode: {e}", exc_info=True)
            await on_shutdown(bot)
            raise
    else:
        # ========== POLLING РЕЖИМ (для разработки) ==========
        logger.info("Starting bot in POLLING mode")
        logger.warning("⚠️  Polling mode is for development only. For production, set WEBHOOK_URL in .env")

        # Pass the active dispatcher to the internal health endpoint in
        # polling mode too. A healthy HTTP server without a dispatcher is not
        # a healthy bot.
        await on_startup(bot, dp)

        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            await on_shutdown(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
