"""Обработчик админ-панели"""
import asyncio

from aiogram import BaseMiddleware, Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, or_, literal, union_all, cast, String
from sqlalchemy.exc import SQLAlchemyError
from database.db import async_session_maker
from database.models import (
    User, Order, Payment, Product, Category, Account, Log, Setting, Coupon
)
from database.ledger import BalanceLedger
from utils.service import upload_accounts_from_file
from utils.keyboards import (
    get_admin_menu_keyboard, get_admin_orders_keyboard, get_admin_catalog_keyboard,
    get_admin_categories_keyboard, get_admin_subcategories_keyboard,
    get_admin_products_keyboard, get_confirm_keyboard,
)
from utils.payments import (
    PAYMENT_METHOD_TITLES,
    PAYMENT_PROVIDERS,
    get_payment_method_order,
    save_payment_enabled_setting,
    save_payment_setting,
)
from utils.payment_rates import synchronize_payment_rates
from utils.referral_settings import (
    DEFAULT_INVITATION_TEXT,
    REFERRAL_CASHBACK_ENABLED_KEY,
    REFERRAL_CASHBACK_PERCENT_KEY,
    REFERRAL_CONDITION_KEY,
    REFERRAL_INVITATION_TEXT_KEY,
    REFERRAL_LEVELS_KEY,
    REFERRAL_REWARD_PERCENT_KEY,
    get_referral_program_config,
    render_invitation_text,
    save_referral_setting,
)
from utils.global_discount import (
    GlobalDiscountConfig,
    PersonalDiscountConfig,
    SCOPES,
    get_global_discount_config,
    get_personal_discount_config,
    save_global_discount_config,
    save_personal_discount_config,
)
from utils.interactions import (
    INTERACTION_KEYS,
    get_interaction_config,
    normalize_external_source,
    save_interaction_setting,
)
from utils.envfile import (
    save_admin_ids_to_env,
    save_interaction_source_to_env,
    save_payment_enabled_to_env,
)
from bot import settings
from utils.text import FAQ_TEXT, RULES_TEXT
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
import logging
import re
from utils.money import ZERO, format_rubles, money, to_money
from utils.ledger import (
    get_ledger_overview,
    get_user_balance,
    ledger_reason_label,
    list_ledger_history,
    list_user_balances,
    record_manual_adjustment,
)
from utils.single_message import apply_single_message_workflow, edit_input_screen
from utils.stars_catalog import VIRTUAL_DELIVERY_TYPES, is_virtual_product
from utils.pricing import (
    FragmentPricingError,
    PRICING_MODE_COST_PLUS,
    PRICING_MODE_FIXED,
    refresh_product_cost_price,
    set_cost_plus_pricing,
    set_fixed_pricing,
)

logger = logging.getLogger(__name__)

router = Router()


def _input_cancel_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    """Единая кнопка отмены для любого экрана, ожидающего ввод."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Отмена", callback_data=callback_data)]
    ])

PAYMENT_SECTIONS = {
    "cards": {
        "title": "Банковские карты и СБП",
        "providers": ("yookassa", "yoomoney", "lava"),
        "back_callback": "admin_payment_cards",
    },
    "crypto": {
        "title": "Криптовалюта",
        "providers": ("heleket", "cryptobot"),
        "back_callback": "admin_payment_crypto",
    },
}

BOT_SETTING_LABELS = {
    "welcome_text": "👋 Приветственное сообщение",
    "support_chat": "💬 Контакт поддержки",
    "faq_text": "❓ Часто задаваемые вопросы",
    "rules_text": "📜 Пользовательское соглашение",
}


class AdminStates(StatesGroup):
    """Состояния для админ-панели"""
    waiting_product_name = State()
    waiting_product_price = State()
    waiting_product_description = State()
    waiting_product_format = State()
    waiting_product_recommendations = State()
    waiting_product_category = State()
    waiting_category_name = State()
    waiting_subcategory_name = State()
    waiting_category_edit_name = State()
    waiting_subcategory_edit_name = State()
    waiting_catalog_node_name = State()
    waiting_catalog_node_edit_name = State()
    waiting_order_id = State()
    waiting_balance_amount = State()

    # Редактирование товаров
    waiting_edit_product_id = State()
    waiting_edit_product_search = State()
    waiting_edit_product_field = State()
    waiting_edit_product_value = State()

    # Управление аккаунтами
    waiting_add_account = State()
    waiting_import_accounts_file = State()

    # Удаление категории
    waiting_bulk_block_users = State()

    # Фильтры заказов
    waiting_order_date_from = State()
    waiting_order_date_to = State()
    waiting_order_user_filter = State()
    waiting_user_search = State()

    # Настройки
    waiting_setting_edit_value = State()
    waiting_payment_setting_value = State()
    waiting_payment_rate_value = State()
    waiting_referral_reward = State()
    waiting_referral_cashback_percent = State()
    waiting_referral_invitation_text = State()
    waiting_coupon_draft_value = State()
    waiting_global_discount_value = State()
    waiting_interaction_value = State()
    waiting_personal_discount_percent = State()
    waiting_personal_discount_days = State()


def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором (только .env, синхронная)"""
    return user_id in settings.admin_ids_list or user_id in settings.developer_ids_list


async def is_admin_async(user_id: int, session: AsyncSession) -> bool:
    """Проверка, является ли пользователь администратором (гибридная: .env + БД)"""
    # Сначала проверяем .env (суперадмины)
    if user_id in settings.admin_ids_list or user_id in settings.developer_ids_list:
        return True

    # Затем проверяем роль в БД
    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user and user.role in ("admin", "developer"):
        return True

    return False


async def is_developer_async(user_id: int, session: AsyncSession) -> bool:
    """Проверка, является ли пользователь разработчиком (гибридная: .env + БД)"""
    # Сначала проверяем .env
    if user_id in settings.developer_ids_list:
        return True

    # Затем проверяем роль в БД
    stmt = select(User).where(User.telegram_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user and user.role == "developer":
        return True

    return False


# Every callback that belongs to this module must pass through the same gate.
# Besides the regular ``admin_`` namespace, older screen callbacks deliberately
# use short prefixes and are listed here explicitly.  Keeping this list next
# to the guard makes a newly introduced namespace a reviewable change instead
# of relying on every individual handler to remember an ACL check.
ADMIN_CALLBACK_PREFIXES = (
    "admin_",
    "stats_",
    "ledger_",
    "user_action_",
    "delete_subcategory_",
    "confirm_delete_subcategory_",
    "delete_category_",
    "confirm_delete_category_",
    "cancel_delete_category_",
    "filter_status_",
    "edit_field_",
    "set_category_",
    "delete_product_",
    "confirm_delete_product_",
    "cancel_delete_product_",
    "setting_key_",
    "delete_account_",
    "confirm_delete_account_",
    "cancel_delete_account_",
)


def _is_admin_callback_data(callback_data: str | None) -> bool:
    return bool(callback_data) and callback_data.startswith(ADMIN_CALLBACK_PREFIXES)


class AdminCallbackAccessMiddleware(BaseMiddleware):
    """Deny non-admin clicks before any admin callback handler is selected.

    Inline messages can be forwarded or left in a group.  Per-handler checks
    are easy to miss on harmless-looking pagination/no-op buttons, so this
    middleware is the authoritative ACL.  It opens a short independent read
    session because router outer middleware runs before the regular callback
    database middleware injects the handler session.
    """

    async def __call__(self, handler, event: CallbackQuery, data: dict):
        if not _is_admin_callback_data(event.data):
            return await handler(event, data)

        chat = getattr(getattr(event.message, "chat", None), "type", None)
        # ChatType is a str enum, but str(ChatType.PRIVATE) renders as
        # ``ChatType.PRIVATE``. Compare the enum value directly instead.
        # An inline callback has no chat at all. It is not a private dialog,
        # therefore it must be denied as well instead of falling through.
        if chat != "private":
            await event.answer(
                "Пункт управления доступен только в личном чате с ботом.",
                show_alert=True,
            )
            return None

        async with async_session_maker() as authorization_session:
            if not await is_admin_async(event.from_user.id, authorization_session):
                await event.answer("Доступ запрещен", show_alert=True)
                return None
        return await handler(event, data)


# This protects callbacks that already have individual checks and the legacy
# pagination/edit callbacks that did not.  Non-admin routers remain untouched
# because the middleware returns immediately for unrelated callback_data.
router.callback_query.outer_middleware(AdminCallbackAccessMiddleware())


class AdminMessageAccessMiddleware(BaseMiddleware):
    """Re-check private chat and role for every active admin FSM state.

    Callback ACL is not sufficient on its own: an administrator can begin an
    edit flow and later lose the role before sending its next message.  All
    message handlers in this router are ``AdminStates`` handlers, so an inner
    router middleware provides one authoritative gate without interfering
    with normal user messages or group support.
    """

    async def __call__(self, handler, event: Message, data: dict):
        state = data.get("state")
        chat = getattr(getattr(event, "chat", None), "type", None)
        user = getattr(event, "from_user", None)

        if chat != "private" or user is None:
            if state is not None:
                await state.clear()
            try:
                await event.answer(
                    "Пункт управления доступен только в личном чате с ботом."
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not send private-chat denial for admin state: %s",
                    exc,
                    exc_info=True,
                )
            return None

        async with async_session_maker() as authorization_session:
            allowed = await is_admin_async(user.id, authorization_session)
        if not allowed:
            if state is not None:
                await state.clear()
            await event.answer("Доступ запрещен")
            return None

        return await handler(event, data)


# Use an inner middleware: it is invoked only after an AdminStates handler
# matched, rather than consuming unrelated user messages before later routers.
router.message.middleware(AdminMessageAccessMiddleware())
apply_single_message_workflow(router)


# Export the shared namespace so section modules can use the same
# router, FSM states, access checks, models and service helpers.
__all__ = [name for name in globals() if not name.startswith("__")]
