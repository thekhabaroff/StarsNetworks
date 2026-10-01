"""Доставка уже оплаченного цифрового товара пользователю."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

from database.db import async_session_maker
from database.models import Order, User, Product
from utils.service import create_accounts_file, get_accounts_for_order
from utils.checkout import CompletedOrder
from utils.notifications import notify_admins_about_purchase, send_notification_to_admins
from utils.stars import FragmentError, send_stars, send_premium
from utils.stars_catalog import is_premium_product, is_virtual_product
from datetime import datetime, timedelta
from sqlalchemy import or_, select, update
from html import escape

logger = logging.getLogger(__name__)
FRAGMENT_ATTEMPT_TIMEOUT = timedelta(minutes=10)


async def deliver_completed_order(bot: Bot, completed: CompletedOrder) -> bool:
    """Отправить файл после commit.

    При сетевой ошибке заказ и связанные аккаунты остаются в БД. Клиент сможет
    повторно запросить файл кнопкой «Скачать товар», а оператор — увидеть
    завершенный заказ и помочь без потери секретных данных.
    """
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
                # A timed-out worker may have completed the remote purchase
                # just before it stopped. Expire only the local sending lease
                # into UNKNOWN; never turn it into a retryable failure.
                now = datetime.now()
                started_at = order.fulfillment_started_at
                if started_at is None or now - started_at >= FRAGMENT_ATTEMPT_TIMEOUT:
                    stale = await session.execute(
                        update(Order)
                        .where(
                            Order.id == order.id,
                            Order.fulfillment_status == "SENDING",
                            or_(
                                Order.fulfillment_started_at.is_(None),
                                Order.fulfillment_started_at <= now - FRAGMENT_ATTEMPT_TIMEOUT,
                            ),
                        )
                        .values(
                            fulfillment_status="UNKNOWN",
                            fulfillment_error=(
                                "Результат запроса Fragment не подтверждён; требуется ручная сверка"
                            ),
                        )
                    )
                    if stale.rowcount:
                        await session.commit()
                        logger.error(
                            "Fragment delivery outcome for order %s is unknown after stale attempt",
                            order.id,
                        )
                        await send_notification_to_admins(
                            bot,
                            "⚠️ <b>Нужна сверка доставки Fragment</b>\n\n"
                            f"Заказ: #{order.id}\n"
                            f"Получатель: @{escape(order.target_username or 'не указан')}\n"
                            f"Товар: {escape(product.name)}\n"
                            "Попытка отправки зависла. Повтор заблокирован до проверки истории Fragment.",
                        )
                logger.warning("Virtual delivery for order %s is not safe to retry while SENDING", order.id)
                return False
            if order.fulfillment_status == "UNKNOWN":
                logger.warning("Fragment delivery for order %s has an unknown outcome; retry blocked", order.id)
                return False
            if order.fulfillment_status not in {"PENDING", "FAILED"}:
                logger.warning(
                    "Virtual delivery for order %s has unsupported state %s",
                    order.id,
                    order.fulfillment_status,
                )
                return False
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

            # Atomic compare-and-set prevents simultaneous payment callbacks
            # and user retries from issuing two paid Fragment requests.
            # ``completed_at`` is payment time, so a distinct attempt time is
            # persisted and used only for the stale-attempt safety check.
            started_at = datetime.now()
            claim = await session.execute(
                update(Order)
                .where(
                    Order.id == order.id,
                    Order.fulfillment_status.in_(("PENDING", "FAILED")),
                )
                .values(
                    fulfillment_status="SENDING",
                    fulfillment_started_at=started_at,
                    fulfillment_attempts=Order.fulfillment_attempts + 1,
                    fulfillment_error=None,
                )
            )
            await session.commit()
            if not claim.rowcount:
                await session.refresh(order)
                return order.fulfillment_status == "SENT"
            await session.refresh(order)
            try:
                if is_premium_product(product):
                    await send_premium(order.target_username, premium_months)
                else:
                    await send_stars(order.target_username, order.quantity)
            except Exception as exc:
                # Timeouts, network failures, 5xx responses, malformed success
                # responses, and unexpected exceptions can all happen after
                # Fragment accepted the purchase. Keep those non-retryable.
                outcome_unknown = not isinstance(exc, FragmentError) or exc.outcome_unknown
                fulfillment_status = "UNKNOWN" if outcome_unknown else "FAILED"
                async with async_session_maker() as error_session:
                    await error_session.execute(
                        update(Order)
                        .where(Order.id == order.id, Order.fulfillment_status == "SENDING")
                        .values(
                            fulfillment_status=fulfillment_status,
                            fulfillment_error=str(exc)[:1000],
                        )
                    )
                    await error_session.commit()
                if outcome_unknown:
                    logger.error(
                        "Fragment delivery outcome for order %s is unknown; automatic retry blocked: %s",
                        order.id,
                        exc,
                    )
                else:
                    logger.warning("Fragment rejected delivery for order %s: %s", order.id, exc)
                try:
                    if outcome_unknown:
                        message = (
                            f"✅ Оплата заказа #{order.id} принята, но Fragment не подтвердил результат отправки.\n"
                            "Чтобы избежать повторного списания, повторная отправка заблокирована. "
                            "Мы проверим статус заказа — пожалуйста, не создавайте повторный заказ."
                        )
                    else:
                        message = (
                            f"✅ Оплата заказа #{order.id} принята, но Fragment отклонил отправку.\n"
                            "Повторить её безопасно: откройте заказ и нажмите «Повторить отправку»."
                        )
                    await bot.send_message(
                        user.telegram_id,
                        message,
                    )
                except Exception:
                    logger.exception("Could not notify buyer about failed virtual delivery %s", order.id)
                if outcome_unknown:
                    try:
                        await send_notification_to_admins(
                            bot,
                            "⚠️ <b>Нужна сверка доставки Fragment</b>\n\n"
                            f"Заказ: #{order.id}\n"
                            f"Получатель: @{escape(order.target_username or 'не указан')}\n"
                            f"Товар: {escape(product.name)}\n"
                            "Результат запроса не подтверждён. Не повторяйте отправку, пока не проверите историю Fragment.",
                        )
                    except Exception:
                        logger.exception("Could not alert admins about unknown Fragment order %s", order.id)
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
