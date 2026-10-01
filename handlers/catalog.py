"""Обработчик каталога"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from database.models import Category, Product, StockNotification
from utils.keyboards import (
    get_back_keyboard, get_cancel_keyboard, get_categories_keyboard, get_products_keyboard,
    get_product_detail_keyboard, get_payment_methods_keyboard,
)
from utils.global_discount import (
    calculate_order_price,
    get_global_discount_config,
    get_personal_discount_config,
    product_category_lineage,
)
from utils.service import reserve_accounts
from database.models import Order, User
from datetime import datetime, timedelta
from decimal import Decimal
from bot import settings
import logging
from html import escape
from utils.promotions import reserve_active_coupon_for_order
from utils.single_message import apply_single_message_workflow, edit_input_screen
from utils.private_chat import apply_private_chat_guard
from utils.stars_catalog import (
    VIRTUAL_DELIVERY_TYPES,
    is_premium_product,
    is_virtual_product,
)
from utils.pricing import (
    FragmentPricingError,
    PRICING_MODE_COST_PLUS,
    refresh_product_cost_price,
    sync_cost_plus_prices,
)
import re

logger = logging.getLogger(__name__)

router = Router()
apply_private_chat_guard(router)
apply_single_message_workflow(router)


async def refresh_catalog_prices(session: AsyncSession) -> None:
    """Best-effort refresh for catalog screens; keep the last safe cache on outage."""
    try:
        await sync_cost_plus_prices(session)
    except Exception as exc:
        logger.warning("Could not refresh automatic catalog prices: %s", exc)


class OrderStates(StatesGroup):
    """Состояния для заказа"""
    waiting_quantity = State()
    waiting_target_username = State()


async def get_visible_category_ids(session: AsyncSession) -> set[int]:
    """Категории, в дереве которых есть активный товар в наличии."""
    category_rows = (
        await session.execute(
            select(Category.id, Category.parent_id).where(
                Category.is_active.is_(True)
            )
        )
    ).all()
    parent_by_id = {category_id: parent_id for category_id, parent_id in category_rows}
    stocked_category_ids = set(
        (
            await session.scalars(
                select(Product.category_id)
                .where(
                    Product.is_active.is_(True),
                    Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
                )
                .distinct()
            )
        ).all()
    )

    visible: set[int] = set()
    for category_id in stocked_category_ids:
        current_id: int | None = category_id
        visited: set[int] = set()
        while current_id in parent_by_id and current_id not in visited:
            visited.add(current_id)
            visible.add(current_id)
            current_id = parent_by_id[current_id]
    return visible


async def get_active_categories(session: AsyncSession):
    """Получить непустые корневые категории, доступные покупателям."""
    visible_ids = await get_visible_category_ids(session)
    if not visible_ids:
        return []
    stmt = select(Category).where(
        Category.is_active.is_(True),
        Category.parent_id.is_(None),
        Category.id.in_(visible_ids),
    ).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_category_depth(session: AsyncSession, category: Category) -> int:
    """Вернуть уровень узла каталога, не зацикливаясь на повреждённых данных."""
    depth = 0
    parent_id = category.parent_id
    visited = {category.id}
    while parent_id is not None and parent_id not in visited and depth < 20:
        visited.add(parent_id)
        parent = await session.get(Category, parent_id)
        if parent is None:
            break
        depth += 1
        parent_id = parent.parent_id
    return depth


@router.callback_query(F.data == "menu_catalog")
async def show_catalog_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Показать каталог из inline-главного меню."""
    await state.clear()
    await refresh_catalog_prices(session)
    categories = await get_active_categories(session)

    if not categories:
        await callback.message.edit_text(
            "Каталог пуст. Обратитесь к администратору.",
            reply_markup=get_back_keyboard(),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "📂 Выберите категорию:",
        reply_markup=get_categories_keyboard(categories)
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_catalog")
async def back_to_catalog(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Вернуться в каталог"""
    await state.clear()
    await refresh_catalog_prices(session)
    categories = await get_active_categories(session)

    if not categories:
        await callback.message.edit_text(
            "Каталог пуст. Обратитесь к администратору.",
            reply_markup=get_back_keyboard(),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "📂 Выберите категорию:",
        reply_markup=get_categories_keyboard(categories)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category_"))
async def show_category_products(callback: CallbackQuery, session: AsyncSession):
    """Показать следующий уровень каталога или товары конечной категории."""
    try:
        category_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректная категория", show_alert=True)
        return

    category_result = await session.execute(
        select(Category).where(Category.id == category_id, Category.is_active.is_(True))
    )
    category = category_result.scalar_one_or_none()
    if category is None:
        await callback.answer("Категория недоступна", show_alert=True)
        return

    await refresh_catalog_prices(session)

    visible_ids = await get_visible_category_ids(session)
    if category.id not in visible_ids:
        await callback.answer(
            "В этой категории пока нет товаров в наличии", show_alert=True
        )
        return

    children = (await session.execute(
        select(Category).where(
            Category.parent_id == category.id,
            Category.is_active.is_(True),
            Category.id.in_(visible_ids),
        ).order_by(Category.sort_order.asc(), Category.name.asc(), Category.id.asc())
    )).scalars().all()
    if children:
        back_callback = (
            f"category_{category.parent_id}"
            if category.parent_id is not None
            else "back_to_catalog"
        )
        child_depth = await get_category_depth(session, category) + 1
        level_names = {
            1: ("🗂", "подкатегорию"),
            2: ("📁", "группу"),
            3: ("🏷", "тип"),
        }
        icon, level_name = level_names.get(child_depth, ("📂", "раздел"))
        keyboard = get_categories_keyboard(children, back_callback=back_callback)
        direct_products_count = await session.scalar(
            select(func.count(Product.id)).where(
                Product.category_id == category.id,
                Product.is_active.is_(True),
                Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
            )
        )
        if direct_products_count:
            keyboard.inline_keyboard.insert(-1, [InlineKeyboardButton(
                text=f"📦 Товары этого раздела ({direct_products_count})",
                callback_data=f"catalog_direct_products_{category.id}",
            )])
        await callback.message.edit_text(
            f"{icon} Выберите {level_name} в «{escape(category.name)}»:",
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    stmt = select(Product).where(
        Product.category_id == category_id,
        Product.is_active.is_(True),
        Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
    ).order_by(Product.sort_order.asc(), Product.name.asc(), Product.id.asc())
    result = await session.execute(stmt)
    products = result.scalars().all()

    if not products:
        await callback.answer("В этой категории пока нет товаров", show_alert=True)
        return

    try:
        back_callback = (
            f"category_{category.parent_id}"
            if category.parent_id is not None
            else "back_to_catalog"
        )
        await callback.message.edit_text(
            "🛒 Выберите товар:",
            reply_markup=get_products_keyboard(
                products, category_id, back_callback=back_callback
            ),
        )
    except Exception as e:
        # Игнорируем ошибку "message is not modified"
        if "message is not modified" not in str(e).lower():
            raise

    await callback.answer()


@router.callback_query(F.data.startswith("catalog_direct_products_"))
async def show_direct_category_products(callback: CallbackQuery, session: AsyncSession):
    """Не скрывать старые товары, пока администратор переносит их глубже в дерево."""
    try:
        category_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный раздел", show_alert=True)
        return
    category = await session.get(Category, category_id)
    if category is None or not category.is_active:
        await callback.answer("Раздел недоступен", show_alert=True)
        return
    await refresh_catalog_prices(session)
    products = (await session.execute(
        select(Product).where(
            Product.category_id == category.id,
            Product.is_active.is_(True),
            Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
        ).order_by(Product.sort_order.asc(), Product.name.asc(), Product.id.asc())
    )).scalars().all()
    if not products:
        await callback.answer("В этом разделе больше нет товаров", show_alert=True)
        return
    await callback.message.edit_text(
        "🛒 Выберите товар:",
        reply_markup=get_products_keyboard(
            products, category.id, back_callback=f"category_{category.id}"
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("product_"))
async def show_product_detail(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    """Показать детали товара"""
    await state.clear()
    try:
        product_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный товар", show_alert=True)
        return

    stmt = select(Product).where(
        Product.id == product_id,
        Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
        Product.is_active.is_(True),
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    if product.pricing_mode == PRICING_MODE_COST_PLUS:
        try:
            await refresh_product_cost_price(session, product)
            await session.commit()
        except FragmentPricingError as exc:
            logger.warning("Could not refresh price for product %s: %s", product.id, exc)

    has_stock = is_virtual_product(product) or product.stock_count > 0
    stock_text = "без ограничений" if is_virtual_product(product) else f"{product.stock_count if has_stock else 0} шт."

    text = f"""📦 <b>{escape(product.name)}</b>

💰 Цена: {product.price:.2f} ₽ за единицу
📊 В наличии: {stock_text}

"""

    if product.description:
        text += f"📝 Описание:\n{escape(product.description)}\n\n"

    if product.format_info:
        text += f"📋 Формат: {escape(product.format_info)}\n\n"

    if product.recommendations:
        text += f"💡 Рекомендации: {escape(product.recommendations)}\n\n"

    if not has_stock:
        text += "❌ Товар временно отсутствует на складе"

    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_product_detail_keyboard(product_id, has_stock, product.category_id),
            parse_mode="HTML",
        )
    except Exception as e:
        error_str = str(e).lower()
        # Игнорируем некритичные ошибки
        if "message is not modified" in error_str:
            # Это не критичная ошибка, просто игнорируем
            pass
        elif any(
            phrase in error_str
            for phrase in [
                "timeout",
                "таймаут",
                "семафора",
                "semaphore",
                "connection",
                "соединение",
                "network",
            ]
        ):
            # Сетевые ошибки - не критичны
            logger.warning(f"Network error in show_product_detail (non-critical): {e}")
        else:
            # Другие ошибки пробрасываем дальше
            raise

    await callback.answer()


@router.callback_query(F.data.startswith("buy_"))
async def start_buy_process(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Начать процесс покупки"""
    try:
        product_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный товар", show_alert=True)
        return

    stmt = select(Product).where(
        Product.id == product_id,
        Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
        Product.is_active.is_(True),
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if not product or (not is_virtual_product(product) and product.stock_count == 0):
        await callback.answer("Товар недоступен", show_alert=True)
        return

    if product.pricing_mode == PRICING_MODE_COST_PLUS:
        try:
            await refresh_product_cost_price(session, product)
            await session.commit()
        except FragmentPricingError:
            await callback.answer(
                "Не удалось получить актуальную себестоимость. Попробуйте позже.",
                show_alert=True,
            )
            return

    # Проверяем количество неоплаченных заказов
    user_id = callback.from_user.id
    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден. Используйте /start", show_alert=True)
        return

    stmt_orders = select(Order).where(
        Order.user_id == user.id,
        Order.status == "ОЖИДАЕТ ОПЛАТЫ"
    )
    result_orders = await session.execute(stmt_orders)
    pending_orders = result_orders.scalars().all()

    if len(pending_orders) >= 3:
        await callback.answer(
            "У вас слишком много неоплаченных заказов. Оплатите или отмените существующие заказы.",
            show_alert=True
        )
        return

    await state.update_data(
        product_id=product_id,
        max_quantity=None if is_virtual_product(product) else product.stock_count,
    )
    if is_virtual_product(product):
        await state.set_state(OrderStates.waiting_target_username)
        recipient_name = "Telegram Premium" if is_premium_product(product) else "звёзд"
        await callback.message.edit_text(
            f"📦 <b>{escape(product.name)}</b>\n\n"
            f"Введите @username получателя {recipient_name} (без @).\n"
            "Чтобы отправить себе, введите <code>себе</code>.",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(f"product_{product_id}"),
        )
    else:
        await state.set_state(OrderStates.waiting_quantity)
        await callback.message.edit_text(
            f"📦 <b>{escape(product.name)}</b>\n\n"
            f"💰 Цена за единицу: {product.price:.2f} ₽\n"
            f"📊 Доступно: {product.stock_count} шт.\n\n"
            f"Введите количество товара:",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(f"product_{product_id}"),
        )
    await callback.answer()


@router.message(OrderStates.waiting_target_username)
async def process_target_username(
    message: Message, state: FSMContext, session: AsyncSession
):
    """Validate and persist the recipient before creating a virtual order."""
    data = await state.get_data()
    raw = (message.text or "").strip()
    if raw.casefold() in {"себе", "я", "me", "self"}:
        username = message.from_user.username
        if not username:
            await edit_input_screen(
                message, state,
                "У вашего аккаунта нет username. Укажите username получателя явно:",
                reply_markup=get_cancel_keyboard(f"product_{data.get('product_id')}"),
                state_data=data,
            )
            return
    else:
        username = raw.removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await edit_input_screen(
                message, state,
                "❌ Некорректный username. Используйте 5–32 латинских символа, цифры или _:",
                reply_markup=get_cancel_keyboard(f"product_{data.get('product_id')}"),
                state_data=data,
            )
            return
    await state.update_data(target_username=username)
    await state.set_state(OrderStates.waiting_quantity)
    product = await session.get(Product, data.get("product_id"))
    if is_premium_product(product):
        await process_quantity(message, state, session, quantity_override=1)
        return
    await edit_input_screen(
        message, state,
        "✅ Получатель: <b>@%s</b>\n\nВведите количество Telegram Stars:" % escape(username),
        reply_markup=get_cancel_keyboard(f"product_{data.get('product_id')}"),
        parse_mode="HTML",
        state_data={**data, "target_username": username},
    )


@router.message(OrderStates.waiting_quantity)
async def process_quantity(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    quantity_override: int | None = None,
):
    """Обработка количества Stars или автоматическое создание тарифа Premium."""
    data = await state.get_data()
    quantity_back_keyboard = get_cancel_keyboard(
        f"product_{data.get('product_id')}" if data.get("product_id") else "menu_catalog"
    )
    try:
        quantity = quantity_override if quantity_override is not None else int(message.text or "")
        if quantity <= 0:
            await edit_input_screen(
                message,
                state,
                "❌ Количество должно быть больше нуля. Введите количество снова:",
                reply_markup=quantity_back_keyboard,
                state_data=data,
            )
            return
        if (
            quantity > settings.STARS_MAX_QUANTITY
            and data.get("max_quantity") is None
            and quantity_override is None
        ):
            await edit_input_screen(
                message,
                state,
                f"❌ За один заказ можно купить не более {settings.STARS_MAX_QUANTITY:,} Stars.\nВведите количество снова:",
                reply_markup=quantity_back_keyboard,
                state_data=data,
            )
            return
        product_id = data.get("product_id")
        max_quantity = data.get("max_quantity")

        if isinstance(max_quantity, int) and quantity > max_quantity:
            await edit_input_screen(
                message,
                state,
                f"Недостаточно товара на складе. Доступно: {max_quantity} шт.\n"
                f"Введите количество снова:",
                reply_markup=quantity_back_keyboard,
                state_data=data,
            )
            return

        # Получаем товар
        stmt = select(Product).where(
            Product.id == product_id,
            Product.delivery_type.in_(VIRTUAL_DELIVERY_TYPES),
            Product.is_active.is_(True),
        )
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()

        if not product:
            await edit_input_screen(
                message,
                state,
                "❌ Товар не найден.",
                reply_markup=get_back_keyboard("menu_catalog"),
                state_data=data,
            )
            await state.clear()
            return

        if product.pricing_mode == PRICING_MODE_COST_PLUS:
            try:
                await refresh_product_cost_price(session, product)
                await session.commit()
            except FragmentPricingError:
                await edit_input_screen(
                    message,
                    state,
                    "❌ Не удалось получить актуальную себестоимость Fragment. "
                    "Попробуйте оформить заказ позже.",
                    reply_markup=quantity_back_keyboard,
                    state_data=data,
                )
                await state.clear()
                return

        # Получаем пользователя
        user_id = message.from_user.id
        stmt_user = select(User).where(User.telegram_id == user_id)
        result_user = await session.execute(stmt_user)
        user = result_user.scalar_one_or_none()

        if not user:
            await edit_input_screen(
                message,
                state,
                "❌ Пользователь не найден. Используйте /start.",
                reply_markup=get_back_keyboard("back_to_menu"),
                state_data=data,
            )
            await state.clear()
            return

        # Price is fixed on reservation: later global/coupon changes never
        # change an already issued invoice.
        coupon, coupon_activation = await reserve_active_coupon_for_order(
            session, user_id=user.id
        )
        global_discount = await get_global_discount_config(session)
        personal_discount = await get_personal_discount_config(session, user)
        category_lineage = await product_category_lineage(session, product.category_id)
        pricing = calculate_order_price(
            price_per_unit=product.price,
            quantity=quantity,
            product=product,
            config=global_discount,
            category_lineage=category_lineage,
            coupon_percent=(
                Decimal(str(coupon.discount_value)) if coupon is not None else Decimal("0")
            ),
            personal_config=personal_discount,
        )

        # There is deliberately no free checkout path.  Do this before stock
        # is reserved and roll back the one-time coupon reservation made
        # above, so an overly large historic discount cannot consume a coupon
        # or hide accounts from other buyers.
        if pricing.total_amount <= Decimal("0"):
            await session.rollback()
            await edit_input_screen(
                message,
                state,
                "❌ Скидки не могут сделать заказ бесплатным. "
                "Измените скидку или промокод и попробуйте снова.",
                reply_markup=quantity_back_keyboard,
                state_data=data,
            )
            await state.clear()
            return

        # Telegram Stars and Premium have unlimited virtual stock.  Legacy account
        # products still use the original row-level reservation path.
        reserved_accounts = []
        if not is_virtual_product(product):
            try:
                reserved_accounts = await reserve_accounts(session, product_id, quantity, None)
            except ValueError as e:
                await session.rollback()
                await edit_input_screen(
                    message,
                    state,
                    f"❌ {str(e)}",
                    reply_markup=quantity_back_keyboard,
                    state_data=data,
                )
                await state.clear()
                return

        # Создаем заказ
        order = Order(
            user_id=user.id,
            product_id=product_id,
            quantity=quantity,
            price_per_unit=product.price,
            discount=pricing.discount_percent,
            discount_amount=pricing.discount_amount,
            total_amount=pricing.total_amount,
            coupon_activation_id=(coupon_activation.id if coupon_activation else None),
            status="ОЖИДАЕТ ОПЛАТЫ",
            target_username=(data.get("target_username") if is_virtual_product(product) else None),
            reserved_until=datetime.now() + timedelta(minutes=settings.ORDER_RESERVATION_MINUTES)
        )
        session.add(order)
        await session.flush()  # Получаем ID заказа

        # Привязываем зарезервированные аккаунты к заказу
        from database.models import Account
        from sqlalchemy import update
        account_ids = [acc.id for acc in reserved_accounts]
        if account_ids:
            await session.execute(
                update(Account)
                .where(Account.id.in_(account_ids))
                .values(order_id=order.id)
            )

        await session.commit()
        await session.refresh(order)

        # Уведомляем администраторов о новом заказе
        try:
            from utils.notifications import notify_new_order
            await notify_new_order(session, order, message.bot)
        except Exception as e:
            logger.error(f"Error notifying about new order: {e}")

        await state.clear()

        # Показываем способы оплаты
        if is_premium_product(product):
            quantity_line = f"Срок: {product.premium_months} мес.\n"
            unit_line = f"Цена тарифа: {product.price:.2f} ₽\n"
        else:
            quantity_line = f"Количество: {quantity} шт.\n"
            unit_line = f"Цена за единицу: {product.price:.2f} ₽\n"
        text = f"""📦 <b>Заказ #{order.id}</b>

Товар: {escape(product.name)}
{quantity_line}{unit_line}"""

        if pricing.discount_amount > 0:
            text += f"Скидка: {pricing.discount_amount:.2f} ₽\n"
        if coupon is not None:
            text += f"Промокод: {escape(coupon.code)}\n"

        text += f"💰 Итого: {pricing.total_amount:.2f} ₽\n\nВыберите категорию оплаты:"

        await edit_input_screen(
            message,
            state,
            text,
            reply_markup=get_payment_methods_keyboard(order.id),
            state_data=data,
        )
        await state.clear()

    except ValueError as e:
        await session.rollback()
        error_msg = str(e)
        if "недостаточно" in error_msg.lower() or "insufficient" in error_msg.lower():
            await edit_input_screen(
                message,
                state,
                f"❌ {error_msg}",
                reply_markup=quantity_back_keyboard,
                state_data=data,
            )
        else:
            await edit_input_screen(
                message,
                state,
                "Пожалуйста, введите число:",
                reply_markup=quantity_back_keyboard,
                state_data=data,
            )
    except Exception as e:
        await session.rollback()
        logger.error(f"Error processing quantity: {e}", exc_info=True)
        error_msg = str(e)
        # Более информативное сообщение об ошибке
        if "недостаточно" in error_msg.lower() or "insufficient" in error_msg.lower():
            error_text = f"❌ {error_msg}"
        elif "integrity" in error_msg.lower() or "constraint" in error_msg.lower():
            error_text = "❌ Ошибка при резервировании товара. Попробуйте снова."
        else:
            error_text = f"❌ Произошла ошибка: {error_msg[:100]}"
        await edit_input_screen(
            message,
            state,
            error_text,
            reply_markup=quantity_back_keyboard,
            state_data=data,
        )
        await state.clear()


@router.callback_query(F.data.startswith("notify_"))
async def subscribe_notification(callback: CallbackQuery, session: AsyncSession):
    """Подписка на уведомление о поступлении товара"""
    try:
        product_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный товар", show_alert=True)
        return
    user_id = callback.from_user.id

    # Получаем пользователя
    stmt_user = select(User).where(User.telegram_id == user_id)
    result_user = await session.execute(stmt_user)
    user = result_user.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    product_result = await session.execute(
        select(Product).where(Product.id == product_id, Product.is_active.is_(True))
    )
    if product_result.scalar_one_or_none() is None:
        await callback.answer("Товар недоступен", show_alert=True)
        return

    # Проверяем, есть ли уже подписка
    stmt = select(StockNotification).where(
        StockNotification.user_id == user.id,
        StockNotification.product_id == product_id,
        StockNotification.is_notified == False
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        await callback.answer("Вы уже подписаны на уведомления", show_alert=True)
        return

    # Создаем подписку
    notification = StockNotification(
        user_id=user.id,
        product_id=product_id
    )
    session.add(notification)
    await session.commit()

    await callback.answer("✅ Вы подписаны на уведомления о поступлении товара", show_alert=True)
