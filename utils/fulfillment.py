"""Доставка уже оплаченного цифрового товара пользователю."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

from database.db import async_session_maker
from database.models import Order, User, Product
from utils.service import create_accounts_file, get_accounts_for_order
from utils.checkout import CompletedOrder
from utils.notifications import notify_admins_about_purchase
from utils.stars import send_stars, send_premium
from utils.stars_catalog import is_premium_product, is_virtual_product
from datetime import datetime, timedelta
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def deliver_completed_order(bot: Bot, completed: CompletedOrder) -> bool:
    """Отправить файл после commit.

    При сетевой ошибке заказ и связанные аккаунты остаются в БД. Клиент сможет
    повторно запросить файл кнопкой «Скачать товар», а оператор — увидеть
    завершенный заказ и помочь без потери секретных данных.
    """
    # A repeated Telegram update is normally a no-op.  For Stars, however, a
    # previous Fragment call may have failed after payment was committed; in
    # that case the same idempotent callback is also the safe retry path.
    if completed.already_completed:
        async with async_session_maker() as check_session:
            product = await check_session.scalar(
                select(Product).join(Order, Order.product_id == Product.id).where(Order.id == completed.order.id)
            )
            order = await check_session.get(Order, completed.order.id)
            if not product or not order or not is_virtual_product(product) or order.fulfillment_status == "SENT":
                return True
            if order.fulfillment_status == "SENDING":
                if not order.completed_at or datetime.now() - order.completed_at < timedelta(minutes=10):
                    return False

    async with async_session_maker() as session:
        order = await session.get(Order, completed.order.id)
        if not order:
            logger.error("Completed order %s disappeared before delivery", completed.order.id)
            return False
        user = await session.get(User, order.user_id)
        if not user:
            logger.error("Buyer for completed order %s was not found", order.id)
            return False

        product = await session.get(Product, order.product_id)
        if product and is_virtual_product(product):
            if order.fulfillment_status == "SENT":
                return True
            if order.fulfillment_status == "SENDING":
                if not order.completed_at or datetime.now() - order.completed_at < timedelta(minutes=10):
                    logger.warning("Virtual delivery for order %s is already in progress", order.id)
                    return False
                order.fulfillment_status = "FAILED"
                order.fulfillment_error = "Предыдущая попытка доставки превысила таймаут; разрешена повторная отправка"
            if not order.target_username:
                order.fulfillment_status = "FAILED"
                order.fulfillment_error = "Получатель виртуального товара не указан"
                await session.commit()
                return False

            premium_months = getattr(product, "premium_months", None)
            if is_premium_product(product) and premium_months not in {3, 6, 12}:
                order.fulfillment_status = "FAILED"
                order.fulfillment_error = "Для тарифа Premium не указан корректный срок (3, 6 или 12 месяцев)"
                await session.commit()
                return False

            # Claim delivery before leaving the database.  This prevents two
            # webhook/reconciliation tasks from sending the same order at the
            # same time.  A crashed SENDING attempt is intentionally left for
            # an explicit admin/user retry rather than risking an accidental
            # double send.
            order.fulfillment_status = "SENDING"
            order.fulfillment_attempts = (order.fulfillment_attempts or 0) + 1
            order.fulfillment_error = None
            await session.commit()
            try:
                if is_premium_product(product):
                    await send_premium(order.target_username, premium_months)
                else:
                    await send_stars(order.target_username, order.quantity)
            except Exception as exc:
                async with async_session_maker() as error_session:
                    failed = await error_session.get(Order, order.id)
                    if failed:
                        failed.fulfillment_status = "FAILED"
                        failed.fulfillment_error = str(exc)[:1000]
                        await error_session.commit()
                logger.error("Fragment delivery failed for order %s: %s", order.id, exc)
                try:
                    await bot.send_message(
                        user.telegram_id,
                        f"✅ Оплата заказа #{order.id} принята, но отправка товара временно не завершилась.\n"
                        "Мы повторим отправку после восстановления Fragment. Если сообщение не придёт, нажмите повторную отправку в заказе.",
                    )
                except Exception:
                    logger.exception("Could not notify buyer about failed virtual delivery %s", order.id)
                return False

            async with async_session_maker() as success_session:
                delivered = await success_session.get(Order, order.id)
                if delivered:
                    delivered.fulfillment_status = "SENT"
                    delivered.delivered_at = datetime.now()
                    delivered.fulfillment_error = None
                    await success_session.commit()
            try:
                await bot.send_message(
                    user.telegram_id,
                    (
                        f"✅ Заказ #{order.id} выполнен!\n"
                        + (
                            f"💎 Telegram Premium на {premium_months} мес. отправлен на @{order.target_username}."
                            if is_premium_product(product)
                            else f"⭐ {order.quantity} Stars отправлены на @{order.target_username}."
                        )
                    ),
                )
            except Exception:
                logger.exception("Could not notify buyer about virtual order %s", order.id)
            try:
                await notify_admins_about_purchase(session, order, bot)
            except Exception:
                logger.exception("Could not notify admins about order %s", order.id)
            return True

        try:
            # Выдаем только то, что прямо сейчас связано с заказом в БД. Это
            # исключает выдачу устаревшего in-memory списка после commit и
            # сохраняет повторное скачивание заказа работоспособным.
            accounts = await get_accounts_for_order(session, order.id)
            if len(accounts) != order.quantity:
                logger.error(
                    "Completed order %s has %s accounts instead of %s",
                    order.id,
                    len(accounts),
                    order.quantity,
                )
                return False
            file_obj = await create_accounts_file(accounts)
            await bot.send_document(
                user.telegram_id,
                BufferedInputFile(file_obj.read(), filename=file_obj.name),
                caption=f"✅ Заказ #{order.id} оплачен и выполнен!\n\n📦 Ваш товар:",
            )
        except Exception:
            logger.exception("Could not deliver completed order %s", order.id)
            return False

        try:
            await notify_admins_about_purchase(session, order, bot)
        except Exception:
            logger.exception("Could not notify admins about order %s", order.id)
        return True
