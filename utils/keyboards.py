"""Inline-клавиатуры бота."""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Optional
from decimal import Decimal
from utils.money import format_rubles
from utils.stars_catalog import is_virtual_product


def get_back_keyboard(callback_data: str = "back_to_menu") -> InlineKeyboardMarkup:
    """Получить клавиатуру только с кнопкой 'назад'"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]
    ])


def get_cancel_keyboard(callback_data: str = "back_to_menu") -> InlineKeyboardMarkup:
    """Клавиатура отмены для экранов, ожидающих ввод пользователя."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Отмена", callback_data=callback_data)]
    ])


def get_main_menu_keyboard(
    is_admin: bool = False,
    balance: float = 0.0,
    cart_count: int = 0,
    cart_total: Decimal | float = Decimal("0"),
    community_url: Optional[str] = None,
    support_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Главное меню, доступное только в виде inline-клавиатуры."""
    keyboard = [
        [InlineKeyboardButton(text=f"💰 Баланс: {format_rubles(balance)} ₽", callback_data="menu_balance")],
        [InlineKeyboardButton(
            text=f"🛒 Корзина: {format_rubles(cart_total)} ₽ ({cart_count})",
            callback_data="cart_open",
        )],
        [InlineKeyboardButton(text="📂 Каталог", callback_data="menu_catalog")],
        [
            InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders"),
            InlineKeyboardButton(text="👥 Пригласить друга", callback_data="menu_referral"),
        ],
    ]

    interaction_buttons = []
    if community_url:
        interaction_buttons.append(InlineKeyboardButton(text="🌐 Сообщество", url=community_url))
    if support_url:
        interaction_buttons.append(InlineKeyboardButton(text="💬 Поддержка", url=support_url))
    if interaction_buttons:
        keyboard.append(interaction_buttons)

    keyboard.extend([
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="menu_coupon")],
        [InlineKeyboardButton(text="ℹ️ Информация", callback_data="menu_info")],
    ])

    if is_admin:
        keyboard.append([InlineKeyboardButton(text="⚙️ Пункт управления", callback_data="admin_menu")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_categories_keyboard(
    categories: List,
    back_callback: str = "back_to_menu",
) -> InlineKeyboardMarkup:
    """Клавиатура категорий"""
    buttons = []
    for category in categories:
        buttons.append([InlineKeyboardButton(
            text=category.name,
            callback_data=f"category_{category.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_products_keyboard(
    products: List,
    category_id: int,
    back_callback: str = "back_to_catalog",
) -> InlineKeyboardMarkup:
    """Клавиатура товаров"""
    buttons = []
    for product in products:
        status = "✅" if is_virtual_product(product) or product.stock_count > 0 else "❌ НЕТ В НАЛИЧИИ"
        buttons.append([InlineKeyboardButton(
            text=f"{product.name} - {product.price:.2f} ₽ {status}",
            callback_data=f"product_{product.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_product_detail_keyboard(product_id: int, has_stock: bool, category_id: int) -> InlineKeyboardMarkup:
    """Клавиатура деталей товара"""
    buttons = []
    if has_stock:
        buttons.append([InlineKeyboardButton(text="🛒 В корзину", callback_data=f"cart_add_{product_id}")])
    else:
        buttons.append([InlineKeyboardButton(
            text="🔔 Уведомить о поступлении",
            callback_data=f"notify_{product_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад к товарам", callback_data=f"category_{category_id}")])
    buttons.append([InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_payment_methods_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Категории оплаты заказа с отдельной оплатой с внутреннего баланса."""
    from utils.payments import (
        get_available_payment_categories,
        get_payment_category_title,
    )

    buttons = [
        [InlineKeyboardButton(text="💳 С баланса", callback_data=f"pay_balance_{order_id}")]
    ]

    for category in get_available_payment_categories():
        buttons.append([
            InlineKeyboardButton(
                text=get_payment_category_title(category),
                callback_data=f"pay_category_{category}_{order_id}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад к заказу", callback_data=f"pay_order_{order_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"cancel_order_{order_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_payment_category_methods_keyboard(
    order_id: int, category: str
) -> InlineKeyboardMarkup:
    """Показать провайдеров только внутри выбранной категории заказа."""
    from utils.payments import PAYMENT_METHOD_TITLES, get_enabled_category_methods

    buttons = [
        [
            InlineKeyboardButton(
                text=PAYMENT_METHOD_TITLES[method],
                callback_data=f"pay_{method}_{order_id}",
            )
        ]
        for method in get_enabled_category_methods(category)
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"pay_order_{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_balance_topup_keyboard() -> InlineKeyboardMarkup:
    """Категории внешнего пополнения баланса."""
    from utils.payments import (
        get_available_payment_categories,
        get_payment_category_title,
    )

    buttons = []

    for category in get_available_payment_categories():
        buttons.append([
            InlineKeyboardButton(
                text=get_payment_category_title(category),
                callback_data=f"topup_category_{category}",
            )
        ])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_balance")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_balance_topup_category_methods_keyboard(category: str) -> InlineKeyboardMarkup:
    """Провайдеры выбранной категории при пополнении баланса."""
    from utils.payments import PAYMENT_METHOD_TITLES, get_enabled_category_methods

    buttons = [
        [
            InlineKeyboardButton(
                text=PAYMENT_METHOD_TITLES[method],
                callback_data=f"topup_{method}",
            )
        ]
        for method in get_enabled_category_methods(category)
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="balance_topup_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_balance_actions_keyboard(
    has_payment_methods: bool = True,
) -> InlineKeyboardMarkup:
    """Действия в разделе баланса до выбора способа пополнения."""
    topup_button = InlineKeyboardButton(
        text="💳 Пополнить" if has_payment_methods else "⛔ Пополнение недоступно",
        callback_data="balance_topup_menu" if has_payment_methods else "balance_topup_unavailable",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            topup_button,
            InlineKeyboardButton(text="🔁 Перевести", callback_data="balance_transfer"),
        ],
        [InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="balance_coupon")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")],
    ])


def get_orders_keyboard(orders: List) -> InlineKeyboardMarkup:
    """Клавиатура заказов"""
    buttons = []
    for order in orders:
        status_emoji = {
            "ОЖИДАЕТ ОПЛАТЫ": "⏳",
            "ОПЛАЧЕНО": "✅",
            "ВЫПОЛНЕНО": "✔️",
            "ОТМЕНЕНО": "❌"
        }.get(order.status, "❓")

        buttons.append([InlineKeyboardButton(
            text=f"{status_emoji} Заказ #{order.id} - {order.total_amount:.2f} ₽",
            callback_data=f"order_{order.id}"
        )])
    pending_orders = [
        order for order in orders
        if order.status == "ОЖИДАЕТ ОПЛАТЫ" and not getattr(order, "batch_id", None)
    ]
    # Корзина оплачивается одной транзакцией: кнопка доступна только когда
    # действительно есть несколько заказов для объединённой оплаты.
    if len(pending_orders) > 1:
        buttons.append([
            InlineKeyboardButton(
                text="🛒 Оплатить все с баланса",
                callback_data="pay_cart_balance",
            )
        ])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_order_detail_keyboard(
    order_id: int,
    status: str,
    fulfillment_status: str | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура деталей заказа"""
    buttons = []
    if status == "ОЖИДАЕТ ОПЛАТЫ":
        buttons.append([InlineKeyboardButton(
            text="💳 Оплатить заказ",
            callback_data=f"pay_order_{order_id}"
        )])
        buttons.append([InlineKeyboardButton(
            text="❌ Отменить заказ",
            callback_data=f"cancel_order_{order_id}"
        )])
    elif status == "ВЫПОЛНЕНО":
        if fulfillment_status in {"FAILED", "SENDING"}:
            buttons.append([InlineKeyboardButton(
                text="🔁 Повторить отправку",
                callback_data=f"retry_delivery_{order_id}",
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text="📥 Скачать товар",
                callback_data=f"download_{order_id}"
            )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="my_orders")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_menu_keyboard(is_developer: bool = False) -> InlineKeyboardMarkup:
    """Админ меню"""
    buttons = []
    if is_developer:
        buttons.append([
            InlineKeyboardButton(
                text="💳 Платёжные системы",
                callback_data="admin_payment_settings",
            )
        ])
    buttons.extend([
        [
            InlineKeyboardButton(text="📦 Заказы", callback_data="admin_orders"),
            InlineKeyboardButton(text="📂 Каталог", callback_data="admin_catalog"),
        ],
        [
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        ],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast_menu")],
        [
            InlineKeyboardButton(text="👥 Реферальная система", callback_data="admin_referral_settings"),
            InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin_coupons"),
        ],
        [
            InlineKeyboardButton(text="🏷 Скидка", callback_data="admin_global_discount"),
            InlineKeyboardButton(text="🤝 Взаимодействие", callback_data="admin_interaction"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_orders_keyboard() -> InlineKeyboardMarkup:
    """Админ - заказы"""
    buttons = [
        [
            InlineKeyboardButton(text="📋 Все заказы", callback_data="admin_orders_all"),
            InlineKeyboardButton(text="🔍 Поиск по ID", callback_data="admin_orders_search"),
        ],
        [
            InlineKeyboardButton(text="📅 По дате", callback_data="admin_orders_date"),
            InlineKeyboardButton(text="📊 По статусу", callback_data="admin_orders_status"),
        ],
        [InlineKeyboardButton(text="👤 По пользователю", callback_data="admin_orders_user")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_catalog_keyboard() -> InlineKeyboardMarkup:
    """Верхний уровень управления каталогом."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📂 Категории", callback_data="admin_catalog_categories"),
            InlineKeyboardButton(text="🗂 Подкатегории", callback_data="admin_catalog_subcategories"),
        ],
        [
            InlineKeyboardButton(text="📁 Группы", callback_data="admin_catalog_groups"),
            InlineKeyboardButton(text="🏷 Типы", callback_data="admin_catalog_types"),
        ],
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin_catalog_products")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
    ])


def get_admin_categories_keyboard() -> InlineKeyboardMarkup:
    """Действия над категориями верхнего уровня."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="admin_add_category")],
        [InlineKeyboardButton(text="🗑️ Удалить категорию", callback_data="admin_delete_category")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")],
    ])


def get_admin_subcategories_keyboard() -> InlineKeyboardMarkup:
    """Действия над подкатегориями."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить подкатегорию", callback_data="admin_add_subcategory")],
        [InlineKeyboardButton(text="🗑️ Удалить подкатегорию", callback_data="admin_delete_subcategory")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")],
    ])


def get_admin_products_keyboard() -> InlineKeyboardMarkup:
    """Действия над товарами и складом."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_delete_product")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_catalog")],
    ])


def get_confirm_keyboard(action: str, item_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения"""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_{action}_{item_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"cancel_{action}_{item_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
