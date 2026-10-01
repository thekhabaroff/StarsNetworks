"""Сервис уведомлений"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import StockNotification, Product, User
from bot import settings
from html import escape
from utils.money import format_rubles
from utils.stars_catalog import is_premium_product, is_virtual_product
import logging

logger = logging.getLogger(__name__)


def format_user_for_html(user: User) -> str:
    """Безопасно отобразить имя пользователя в HTML-сообщении Telegram."""
    if user.username:
        return f"@{escape(user.username)}"
    return escape(user.first_name or "Без имени")


async def send_notification_to_admins(bot, message: str, parse_mode: str = "HTML"):
    """Отправить служебное уведомление администраторам в личный чат с ботом."""
    for admin_id in settings.admin_ids_list:
        try:
            await bot.send_message(admin_id, message, parse_mode=parse_mode)
        except Exception as e:
            logger.error("Error sending notification to admin %s: %s", admin_id, e)


async def notify_stock_available(session: AsyncSession, product_id: int, bot, check_stock_was_zero: bool = False):
    """Уведомить пользователей о поступлении товара

    Args:
        session: Сессия БД
        product_id: ID товара
        bot: Экземпляр бота
        check_stock_was_zero: Если True, уведомляет только если stock_count был 0 и стал >0
    """
    try:
        # Получаем товар для проверки текущего количества
        stmt_product = select(Product).where(Product.id == product_id)
        result_product = await session.execute(stmt_product)
        product = result_product.scalar_one_or_none()

        if not product:
            return

        # Если нужно проверить, что stock_count был 0 и стал >0
        if check_stock_was_zero:
            # Получаем количество аккаунтов на складе из таблицы Account
            from sqlalchemy import func
            from database.models import Account
            stmt_count = select(func.count(Account.id)).where(
                Account.product_id == product_id,
                Account.is_sold == False
            )
            result_count = await session.execute(stmt_count)
            actual_stock_count = result_count.scalar() or 0

            # Если stock_count в Product не совпадает с реальным количеством, обновляем
            if product.stock_count != actual_stock_count:
                from sqlalchemy import update
                await session.execute(
                    update(Product)
                    .where(Product.id == product_id)
                    .values(stock_count=actual_stock_count)
                )
                await session.commit()
                # Обновляем объект product
                result_product = await session.execute(stmt_product)
                product = result_product.scalar_one_or_none()

            # Уведомляем только если stock_count стал >0 (был 0 или меньше)
            if product.stock_count <= 0:
                return

        # Получаем все активные подписки
        stmt = select(StockNotification).where(
            StockNotification.product_id == product_id,
            StockNotification.is_notified == False
        )
        result = await session.execute(stmt)
        notifications = result.scalars().all()

        if not notifications:
            return

        # Отправляем уведомления
        for notification in notifications:
            try:
                stmt_user = select(User).where(User.id == notification.user_id)
                result_user = await session.execute(stmt_user)
                user = result_user.scalar_one_or_none()

                if user and not user.is_blocked:
                    await bot.send_message(
                        user.telegram_id,
                        f"🔔 <b>Товар поступил в продажу!</b>\n\n"
                        f"📦 {escape(product.name)}\n"
                        f"💰 Цена: {format_rubles(product.price)} ₽\n"
                        f"📊 В наличии: {product.stock_count} шт.\n\n"
                        f"Используйте меню 'Каталог' для покупки.",
                        parse_mode="HTML"
                    )

                    # Помечаем как уведомленное
                    notification.is_notified = True
            except Exception as e:
                logger.error(f"Error notifying user {notification.user_id}: {e}")

        await session.commit()

    except Exception as e:
        logger.error(f"Error in notify_stock_available: {e}")


async def notify_admins_about_purchase(session: AsyncSession, order, bot):
    """Уведомить администраторов о покупке"""
    try:
        from database.models import User, Product

        stmt_user = select(User).where(User.id == order.user_id)
        result_user = await session.execute(stmt_user)
        user = result_user.scalar_one_or_none()

        stmt_product = select(Product).where(Product.id == order.product_id)
        result_product = await session.execute(stmt_product)
        product = result_product.scalar_one_or_none()

        if not user or not product:
            return

        stock_label = "без ограничений" if is_virtual_product(product) else f"{product.stock_count} шт."
        if getattr(order, "target_username", None) and is_virtual_product(product):
            recipient_icon = "💎" if is_premium_product(product) else "⭐"
            recipient_line = f"{recipient_icon} Получатель: @{escape(order.target_username)}\n"
        else:
            recipient_line = ""
        quantity_line = (
            f"💎 Срок: {product.premium_months} мес."
            if is_premium_product(product)
            else f"📊 Количество: {order.quantity} шт."
        )
        text = f"""🛒 <b>Новая покупка</b>

👤 Пользователь: {format_user_for_html(user)} (ID: {user.telegram_id})
📦 Товар: {escape(product.name)}
{quantity_line}
{recipient_line}
💰 Сумма: {format_rubles(order.total_amount)} ₽
💳 Способ оплаты: {escape(order.payment_method or 'Не указан')}
📋 Остаток на складе: {stock_label}
🆔 Заказ: #{order.id}
"""

        await send_notification_to_admins(bot, text)

    except Exception as e:
        logger.error(f"Error in notify_admins_about_purchase: {e}")


async def notify_user_registration(session: AsyncSession, user: User, bot):
    """Уведомить о регистрации нового пользователя"""
    try:
        text = f"""👤 <b>Новая регистрация</b>

👤 Пользователь: {format_user_for_html(user)} (ID: {user.telegram_id})
📅 Дата: {user.created_at.strftime('%d.%m.%Y %H:%M')}
🔗 Реферальный код: {escape(user.referral_code or 'Нет')}
"""

        if user.referred_by:
            stmt_ref = select(User).where(User.id == user.referred_by)
            result_ref = await session.execute(stmt_ref)
            referrer = result_ref.scalar_one_or_none()
            if referrer:
                text += f"👥 Приглашен пользователем: {format_user_for_html(referrer)} (ID: {referrer.telegram_id})\n"

        await send_notification_to_admins(bot, text)
    except Exception as e:
        logger.error(f"Error in notify_user_registration: {e}")


async def notify_balance_topup(session: AsyncSession, user: User, amount, bot):
    """Уведомить о пополнении баланса"""
    try:
        text = f"""💰 <b>Пополнение баланса</b>

👤 Пользователь: {format_user_for_html(user)} (ID: {user.telegram_id})
💵 Сумма: {format_rubles(amount)} ₽
💳 Новый баланс: {format_rubles(user.balance)} ₽
"""
        await send_notification_to_admins(bot, text)
    except Exception as e:
        logger.error(f"Error in notify_balance_topup: {e}")


async def notify_new_order(session: AsyncSession, order, bot):
    """Уведомить о создании нового заказа"""
    try:
        from database.models import User, Product

        stmt_user = select(User).where(User.id == order.user_id)
        result_user = await session.execute(stmt_user)
        user = result_user.scalar_one_or_none()

        stmt_product = select(Product).where(Product.id == order.product_id)
        result_product = await session.execute(stmt_product)
        product = result_product.scalar_one_or_none()

        if not user or not product:
            return

        quantity_line = (
            f"💎 Срок: {product.premium_months} мес."
            if is_premium_product(product)
            else f"📊 Количество: {order.quantity} шт."
        )
        recipient_line = (
            f"{'💎' if is_premium_product(product) else '⭐'} Получатель: @{escape(order.target_username)}\n"
            if is_virtual_product(product) and getattr(order, "target_username", None)
            else ""
        )
        text = f"""📦 <b>Новый заказ</b>

👤 Пользователь: {format_user_for_html(user)} (ID: {user.telegram_id})
📦 Товар: {escape(product.name)}
{quantity_line}
{recipient_line}💰 Сумма: {order.total_amount:.2f} ₽
⏳ Статус: {escape(order.status)}
🆔 Заказ: #{order.id}
"""
        await send_notification_to_admins(bot, text)
    except Exception as e:
        logger.error(f"Error in notify_new_order: {e}")
