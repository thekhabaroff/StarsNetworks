"""User-facing persistent cart and multi-recipient checkout screens."""
from __future__ import annotations

import logging
import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CartItem, Product, User
from utils.batch_checkout import BatchCheckoutError, create_order_batch
from utils.cart import (
    CartError,
    add_cart_item,
    clear_cart,
    get_cart_items,
    remove_cart_item,
    update_cart_quantity,
    update_cart_target,
)
from utils.fragment_pricing import FragmentPricingError
from utils.keyboards import get_back_keyboard
from utils.money import format_rubles, money
from utils.payments import get_available_payment_categories, get_payment_category_title
from utils.pricing import refresh_product_cost_price, sync_cost_plus_prices
from utils.single_message import apply_single_message_workflow, edit_input_screen
from utils.stars_catalog import is_premium_product, is_virtual_product
from utils.private_chat import apply_private_chat_guard

logger = logging.getLogger(__name__)
router = Router()
apply_private_chat_guard(router)
apply_single_message_workflow(router)


class CartStates(StatesGroup):
    waiting_target = State()
    waiting_quantity = State()
    editing_target = State()
    editing_quantity = State()


def _user_id(callback: CallbackQuery | Message) -> int:
    return callback.from_user.id


def _username(raw: str) -> str | None:
    value = (raw or "").strip()
    if value.casefold() in {"себе", "я", "me", "self"}:
        return None
    value = value.removeprefix("@").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_]{5,32}", value) else ""


def _cart_keyboard(items: list[tuple[CartItem, Product]], *, empty: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item, product in items:
        quantity = f"{item.quantity} шт." if not is_premium_product(product) else "1 подписка"
        rows.append([InlineKeyboardButton(
            text=f"✏️ {product.name} — @{item.target_username} ({quantity})",
            callback_data=f"cart_item_{item.id}",
        )])
    if not empty:
        rows.append([InlineKeyboardButton(text="💳 Перейти к оплате", callback_data="cart_checkout")])
        rows.append([
            InlineKeyboardButton(text="➕ Добавить ещё", callback_data="cart_to_catalog"),
            InlineKeyboardButton(text="🗑 Очистить", callback_data="cart_clear"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="📂 Открыть каталог", callback_data="menu_catalog")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cart_text(items: list[tuple[CartItem, Product]]) -> str:
    if not items:
        return "🛒 <b>Корзина пуста</b>\n\nДобавьте товары из каталога."
    total = sum((money(item.unit_price) * item.quantity for item, _ in items), money(0))
    lines = ["🛒 <b>Корзина</b>", ""]
    for index, (item, product) in enumerate(items, 1):
        quantity = (
            f"Premium на {product.premium_months} мес."
            if is_premium_product(product)
            else f"{item.quantity:,} Stars"
        )
        line_total = money(item.unit_price) * item.quantity
        lines.append(
            f"{index}. <b>{escape(product.name)}</b>\n"
            f"   Получатель: @{escape(item.target_username)}\n"
            f"   {quantity} — {format_rubles(line_total)} ₽"
        )
    lines.extend(["", f"💰 Итого: <b>{format_rubles(total)} ₽</b>"])
    return "\n".join(lines)


async def _show_cart(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    rows = await get_cart_items(session, callback.from_user.id)
    await callback.message.edit_text(
        _cart_text(rows),
        reply_markup=_cart_keyboard(rows, empty=not rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "cart_open")
async def cart_open(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await _show_cart(callback, session, state)


@router.callback_query(F.data.startswith("cart_add_"))
async def cart_add_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    try:
        product_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный товар", show_alert=True)
        return
    product = await session.scalar(select(Product).where(
        Product.id == product_id, Product.is_active.is_(True)
    ))
    if not product or not is_virtual_product(product):
        await callback.answer("Товар недоступен", show_alert=True)
        return
    await state.update_data(product_id=product_id)
    await state.set_state(CartStates.waiting_target)
    title = "Telegram Premium" if is_premium_product(product) else "Telegram Stars"
    await callback.message.edit_text(
        f"📦 <b>{escape(product.name)}</b>\n\n"
        f"Введите @username получателя {title} (без @).\n"
        "Чтобы отправить себе, введите <code>себе</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart_open")],
            [InlineKeyboardButton(text="⛔ Отмена", callback_data=f"product_{product_id}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CartStates.waiting_target)
async def cart_target_input(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    raw = (message.text or "").strip()
    if raw.casefold() in {"себе", "я", "me", "self"}:
        username = message.from_user.username
        if not username:
            await edit_input_screen(
                message, state, "У вас нет username Telegram. Укажите получателя явно:",
                reply_markup=get_back_keyboard("cart_open"), state_data=data,
            )
            return
    else:
        username = raw.removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await edit_input_screen(
                message, state, "❌ Некорректный username. Используйте 5–32 латинских символа, цифры или _:",
                reply_markup=get_back_keyboard("cart_open"), state_data=data,
            )
            return
    await state.update_data(target_username=username)
    product = await session.get(Product, data.get("product_id"))
    if product and is_premium_product(product):
        await _add_item_from_input(message, state, session, 1)
        return
    await state.set_state(CartStates.waiting_quantity)
    await edit_input_screen(
        message, state,
        f"✅ Получатель: <b>@{escape(username)}</b>\n\nВведите количество Telegram Stars:",
        reply_markup=get_back_keyboard("cart_open"), parse_mode="HTML", state_data={**data, "target_username": username},
    )


async def _add_item_from_input(
    message: Message, state: FSMContext, session: AsyncSession, quantity: int
) -> None:
    data = await state.get_data()
    product = await session.scalar(select(Product).where(
        Product.id == data.get("product_id"), Product.is_active.is_(True)
    ))
    try:
        if product is None or not is_virtual_product(product):
            raise CartError("Товар больше недоступен")
        if product.pricing_mode == "COST_PLUS":
            try:
                await refresh_product_cost_price(session, product)
                await session.commit()
            except FragmentPricingError:
                await session.rollback()
                raise CartError("Не удалось получить актуальную цену товара")
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user is None:
            raise CartError("Пользователь не найден. Нажмите /start")
        await add_cart_item(
            session, user_id=user.id, product=product,
            target_username=data.get("target_username", ""), quantity=quantity,
        )
        await session.commit()
        rows = await get_cart_items(session, user.id)
        screen_data = await state.get_data()
        await edit_input_screen(
            message, state, _cart_text(rows),
            reply_markup=_cart_keyboard(rows), parse_mode="HTML", state_data=screen_data,
        )
        await state.clear()
    except CartError as exc:
        await session.rollback()
        await edit_input_screen(
            message, state, f"❌ {escape(str(exc))}",
            reply_markup=get_back_keyboard("cart_open"), parse_mode="HTML", state_data=data,
        )
    except Exception:
        await session.rollback()
        logger.exception("Could not add cart item")
        await edit_input_screen(
            message, state, "❌ Не удалось добавить товар. Попробуйте ещё раз.",
            reply_markup=get_back_keyboard("cart_open"), state_data=data,
        )


@router.message(CartStates.waiting_quantity)
async def cart_quantity_input(message: Message, state: FSMContext, session: AsyncSession):
    try:
        quantity = int((message.text or "").strip())
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        await edit_input_screen(message, state, "❌ Введите положительное целое количество:", reply_markup=get_back_keyboard("cart_open"))
        return
    await _add_item_from_input(message, state, session, quantity)


@router.callback_query(F.data.startswith("cart_item_"))
async def cart_item_detail(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    try:
        item_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректная позиция", show_alert=True)
        return
    rows = await get_cart_items(session, callback.from_user.id)
    row = next(((item, product) for item, product in rows if item.id == item_id), None)
    if row is None:
        await callback.answer("Позиция уже удалена", show_alert=True)
        return
    item, product = row
    quantity = f"Premium на {product.premium_months} мес." if is_premium_product(product) else f"{item.quantity:,} Stars"
    keyboard = [
        [InlineKeyboardButton(text="✏️ Получатель", callback_data=f"cart_edit_target_{item.id}")],
    ]
    if not is_premium_product(product):
        keyboard.append([InlineKeyboardButton(text="✏️ Количество", callback_data=f"cart_edit_qty_{item.id}")])
    keyboard.extend([
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cart_remove_{item.id}")],
        [InlineKeyboardButton(text="◀️ В корзину", callback_data="cart_open")],
    ])
    await callback.message.edit_text(
        f"🛒 <b>{escape(product.name)}</b>\n\n"
        f"Получатель: @{escape(item.target_username)}\n"
        f"Количество: {quantity}\n"
        f"Сумма: {format_rubles(money(item.unit_price) * item.quantity)} ₽",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cart_remove_"))
async def cart_remove(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    try:
        item_id = int(callback.data.rsplit("_", 1)[1])
        removed = await remove_cart_item(session, user_id=(await session.scalar(select(User.id).where(User.telegram_id == callback.from_user.id))), item_id=item_id)
        await session.commit()
    except (TypeError, ValueError, CartError):
        await session.rollback()
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    await _show_cart(callback, session, state)


@router.callback_query(F.data == "cart_clear")
async def cart_clear(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = await session.scalar(select(User.id).where(User.telegram_id == callback.from_user.id))
    if user_id is not None:
        await clear_cart(session, user_id=user_id)
        await session.commit()
    await _show_cart(callback, session, state)


@router.callback_query(F.data == "cart_to_catalog")
async def cart_to_catalog(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("📂 Выберите категорию:", reply_markup=get_back_keyboard("menu_catalog"))
    await callback.answer()


@router.callback_query(F.data.startswith("cart_edit_target_"))
async def cart_edit_target_start(callback: CallbackQuery, state: FSMContext):
    item_id = callback.data.rsplit("_", 1)[1]
    await state.update_data(edit_item_id=int(item_id))
    await state.set_state(CartStates.editing_target)
    await callback.message.edit_text(
        "Введите новый @username получателя (без @):",
        reply_markup=get_back_keyboard(f"cart_item_{item_id}"),
    )
    await callback.answer()


@router.message(CartStates.editing_target)
async def cart_edit_target_input(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    raw = (message.text or "").strip()
    username = message.from_user.username if raw.casefold() in {"себе", "я", "me", "self"} else raw.removeprefix("@").strip()
    if not username or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        await edit_input_screen(message, state, "❌ Некорректный username:", reply_markup=get_back_keyboard("cart_open"), state_data=data)
        return
    try:
        user_id = await session.scalar(select(User.id).where(User.telegram_id == message.from_user.id))
        await update_cart_target(session, user_id=user_id, item_id=int(data["edit_item_id"]), target_username=username)
        await session.commit()
        rows = await get_cart_items(session, user_id)
        await edit_input_screen(message, state, _cart_text(rows), reply_markup=_cart_keyboard(rows), parse_mode="HTML", state_data=data)
        await state.clear()
    except CartError as exc:
        await session.rollback()
        await edit_input_screen(message, state, f"❌ {escape(str(exc))}", reply_markup=get_back_keyboard("cart_open"), state_data=data)


@router.callback_query(F.data.startswith("cart_edit_qty_"))
async def cart_edit_quantity_start(callback: CallbackQuery, state: FSMContext):
    item_id = callback.data.rsplit("_", 1)[1]
    await state.update_data(edit_item_id=int(item_id))
    await state.set_state(CartStates.editing_quantity)
    await callback.message.edit_text(
        "Введите новое количество Stars:",
        reply_markup=get_back_keyboard(f"cart_item_{item_id}"),
    )
    await callback.answer()


@router.message(CartStates.editing_quantity)
async def cart_edit_quantity_input(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    try:
        quantity = int((message.text or "").strip())
        user_id = await session.scalar(select(User.id).where(User.telegram_id == message.from_user.id))
        await update_cart_quantity(session, user_id=user_id, item_id=int(data["edit_item_id"]), quantity=quantity)
        await session.commit()
        rows = await get_cart_items(session, user_id)
        await edit_input_screen(message, state, _cart_text(rows), reply_markup=_cart_keyboard(rows), parse_mode="HTML", state_data=data)
        await state.clear()
    except (TypeError, ValueError, CartError) as exc:
        await session.rollback()
        await edit_input_screen(message, state, f"❌ {escape(str(exc) or 'Некорректное количество')}", reply_markup=get_back_keyboard("cart_open"), state_data=data)


def _batch_payment_keyboard(batch_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💳 С баланса", callback_data=f"batch_pay_balance_{batch_id}")]]
    for category in get_available_payment_categories():
        rows.append([InlineKeyboardButton(
            text=get_payment_category_title(category),
            callback_data=f"batch_pay_category_{category}_{batch_id}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отменить оформление", callback_data=f"batch_cancel_{batch_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "cart_checkout")
async def cart_checkout(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    try:
        await sync_cost_plus_prices(session)
        batch, orders = await create_order_batch(session, callback.from_user.id)
        await session.commit()
    except (BatchCheckoutError, CartError) as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await session.rollback()
        logger.exception("Could not create order batch")
        await callback.answer("Не удалось оформить корзину. Попробуйте позже.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"📦 <b>Оформление #{batch.id}</b>\n\n"
        f"Позиций: <b>{len(orders)}</b>\n"
        f"К оплате: <b>{format_rubles(batch.total_amount)} ₽</b>\n\n"
        "Выберите способ оплаты:",
        reply_markup=_batch_payment_keyboard(batch.id), parse_mode="HTML",
    )
    await callback.answer()
