"""Модели базы данных."""
from decimal import Decimal
from sqlalchemy import (
    Column, Integer, BigInteger, String, Numeric, Boolean, DateTime, Text, ForeignKey,
    Index, CheckConstraint, UniqueConstraint, text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.db import Base


# Monetary values are stored in PostgreSQL exactly to the kopeck.  Never use
# binary ``FLOAT`` for balances, prices or payment amounts.
MONEY = Numeric(18, 2)
PERCENT = Numeric(9, 4)
ZERO_MONEY = Decimal("0.00")
ZERO_PERCENT = Decimal("0.0000")


class User(Base):
    """Пользователь"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    balance = Column(MONEY, default=ZERO_MONEY, nullable=False)
    personal_discount = Column(PERCENT, default=ZERO_PERCENT, nullable=False)
    personal_discount_until = Column(DateTime, nullable=True)
    personal_discount_enabled = Column(Boolean, default=False, nullable=False)
    personal_discount_scope = Column(String(20), default="ALL", nullable=False)
    personal_discount_mode = Column(String(20), default="MAXIMUM", nullable=False)
    personal_discount_type = Column(String(20), default="PERCENT", nullable=False)
    is_blocked = Column(Boolean, default=False, nullable=False)
    referral_code = Column(String(50), unique=True, nullable=True, index=True)
    referred_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(String(50), default="user", nullable=False)  # user, admin, developer
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    orders = relationship("Order", back_populates="user")
    cart = relationship("Cart", back_populates="user", uselist=False)
    order_batches = relationship("OrderBatch", back_populates="user")
    referrals = relationship("User", remote_side=[id], backref="referrer")


class Category(Base):
    """Категория товаров"""
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    parent_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    # Порядок отображения внутри одного родительского раздела.
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    # Relationships
    products = relationship("Product", back_populates="category")
    parent = relationship("Category", remote_side=[id], backref="subcategories")


class Product(Base):
    """Товар"""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(MONEY, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    # Порядок товара среди товаров его категории.
    sort_order = Column(Integer, default=0, nullable=False)
    stock_count = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    format_info = Column(Text, nullable=True)  # Формат выдаваемых аккаунтов
    recommendations = Column(Text, nullable=True)  # Рекомендации к покупке
    # Virtual Stars and Premium modes are fulfilled through Fragment.  The
    # generic ``account`` mode is retained for historical rows so old orders
    # remain readable, but it is never exposed by the public catalog.
    delivery_type = Column(String(32), default="account", nullable=False)
    # Premium products represent a fixed subscription plan.  Keeping the
    # duration on the product avoids parsing display text during fulfillment.
    premium_months = Column(Integer, nullable=True)
    # Pricing can remain a manually entered retail price or be derived from a
    # live Fragment cost quote (product + displayed network amount + API fee)
    # and a markup.
    pricing_mode = Column(String(20), default="FIXED", nullable=False)
    cost_price = Column(MONEY, nullable=True)
    markup_percent = Column(PERCENT, default=ZERO_PERCENT, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    category = relationship("Category", back_populates="products")
    accounts = relationship("Account", back_populates="product")
    orders = relationship("Order", back_populates="product")
    notifications = relationship("StockNotification", back_populates="product")

    __table_args__ = (
        CheckConstraint('price >= 0', name='check_price_positive'),
        # A product may be retained as inactive history at zero, but an active
        # catalogue item must be payable.  This backs up the admin UI and the
        # checkout's free-order refusal at the database boundary.
        CheckConstraint(
            'price > 0 OR is_active = false',
            name='check_active_product_price_positive',
        ),
        CheckConstraint('stock_count >= 0', name='check_stock_positive'),
        CheckConstraint(
            "pricing_mode IN ('FIXED', 'COST_PLUS')",
            name='check_product_pricing_mode',
        ),
        CheckConstraint(
            'cost_price IS NULL OR cost_price >= 0',
            name='check_product_cost_price_positive',
        ),
        CheckConstraint(
            'markup_percent >= 0',
            name='check_product_markup_nonnegative',
        ),
    )


class Account(Base):
    """Аккаунт (склад)"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    account_data = Column(Text, nullable=False)  # логин:пароль
    is_sold = Column(Boolean, default=False, nullable=False)
    sold_at = Column(DateTime, nullable=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    is_blocked = Column(Boolean, default=False, nullable=False)  # Заблокирован ли аккаунт
    blocked_at = Column(DateTime, nullable=True)  # Дата блокировки
    blocked_by = Column(Integer, ForeignKey("users.id"), nullable=True)  # Кто заблокировал (администратор)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    # Relationships
    product = relationship("Product", back_populates="accounts")
    order = relationship("Order", back_populates="accounts")

    __table_args__ = (
        Index('idx_product_sold', 'product_id', 'is_sold'),
        # Issuance and payment completion retrieve accounts by their order.
        Index('idx_account_order_id', 'order_id'),
    )


class Cart(Base):
    """Сохранённая корзина пользователя до оформления."""
    __tablename__ = "carts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    status = Column(String(20), default="DRAFT", nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="cart")
    items = relationship(
        "CartItem",
        back_populates="cart",
        cascade="all, delete-orphan",
        order_by="CartItem.id",
    )

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'CHECKED_OUT')", name="check_cart_status"),
        Index("idx_cart_user_status", "user_id", "status"),
    )


class CartItem(Base):
    """Одна позиция виртуального товара и её получатель."""
    __tablename__ = "cart_items"

    id = Column(Integer, primary_key=True)
    cart_id = Column(Integer, ForeignKey("carts.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    target_username = Column(String(32), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(MONEY, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    cart = relationship("Cart", back_populates="items")
    product = relationship("Product")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="check_cart_item_quantity_positive"),
        CheckConstraint("unit_price > 0", name="check_cart_item_price_positive"),
        Index("idx_cart_item_cart", "cart_id", "id"),
        Index("idx_cart_item_target", "cart_id", "target_username"),
    )


class Order(Base):
    """Заказ"""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    # Parent checkout for multi-recipient purchases; NULL keeps old orders compatible.
    batch_id = Column(Integer, ForeignKey("order_batches.id"), nullable=True)
    quantity = Column(Integer, nullable=False)
    price_per_unit = Column(MONEY, nullable=False)
    discount = Column(PERCENT, default=ZERO_PERCENT, nullable=False)
    discount_amount = Column(MONEY, default=ZERO_MONEY, nullable=False)
    total_amount = Column(MONEY, nullable=False)
    # A one-time coupon is reserved by a pending order and returned if that
    # order is cancelled before payment.
    coupon_activation_id = Column(Integer, ForeignKey("coupon_activations.id"), nullable=True)
    status = Column(String(50), default="ОЖИДАЕТ ОПЛАТЫ", nullable=False)  # ОЖИДАЕТ ОПЛАТЫ, ОПЛАЧЕНО, ВЫПОЛНЕНО, ОТМЕНЕНО
    payment_method = Column(String(50), nullable=True)
    payment_id = Column(String(255), nullable=True)  # ID платежа в платежной системе
    reserved_until = Column(DateTime, nullable=True)  # Бронирование товара
    created_at = Column(DateTime, default=func.now(), nullable=False)
    paid_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    # Recipient of Telegram Stars.  It is stored on the immutable order so a
    # retry can never accidentally send stars to a different account.
    target_username = Column(String(32), nullable=True)
    fulfillment_status = Column(String(20), default="PENDING", nullable=False)
    fulfillment_attempts = Column(Integer, default=0, nullable=False)
    fulfillment_error = Column(Text, nullable=True)
    fulfillment_started_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="orders")
    product = relationship("Product", back_populates="orders")
    batch = relationship("OrderBatch", back_populates="orders")
    accounts = relationship("Account", back_populates="order")

    __table_args__ = (
        CheckConstraint('quantity > 0', name='check_quantity_positive'),
        CheckConstraint('total_amount >= 0', name='check_amount_positive'),
        # Historical completed/cancelled zero-sum records remain readable, but
        # a new pending order must always have a real amount to pay.
        CheckConstraint(
            "total_amount > 0 OR status <> 'ОЖИДАЕТ ОПЛАТЫ'",
            name='check_pending_order_amount_positive',
        ),
        Index('idx_user_status', 'user_id', 'status'),
        Index('idx_status', 'status'),
    )


class OrderBatch(Base):
    """Неизменяемое оформление, объединяющее несколько заказов."""
    __tablename__ = "order_batches"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(24), default="PENDING_PAYMENT", nullable=False)
    subtotal_amount = Column(MONEY, nullable=False)
    discount_amount = Column(MONEY, default=ZERO_MONEY, nullable=False)
    total_amount = Column(MONEY, nullable=False)
    coupon_activation_id = Column(Integer, ForeignKey("coupon_activations.id"), nullable=True)
    reserved_until = Column(DateTime, nullable=True)
    payment_method = Column(String(50), nullable=True)
    payment_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    paid_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="order_batches")
    orders = relationship("Order", back_populates="batch")

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'PAID', 'PARTIAL', 'COMPLETED', 'CANCELLED')",
            name="check_order_batch_status",
        ),
        CheckConstraint("subtotal_amount > 0", name="check_order_batch_subtotal_positive"),
        CheckConstraint("total_amount > 0", name="check_order_batch_total_positive"),
        CheckConstraint("discount_amount >= 0", name="check_order_batch_discount_nonnegative"),
        Index("idx_order_batch_user_status", "user_id", "status"),
        Index("idx_order_batch_reserved_until", "reserved_until"),
    )


class StockNotification(Base):
    """Подписка на уведомление о поступлении товара"""
    __tablename__ = "stock_notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    is_notified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    # Relationships
    product = relationship("Product", back_populates="notifications")

    __table_args__ = (
        Index('idx_user_product', 'user_id', 'product_id'),
    )


class Payment(Base):
    """Платеж"""
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(MONEY, nullable=False)
    # Фактическое число единиц у провайдера. Сейчас используется для XTR,
    # чтобы изменение курса не делало ранее созданный Telegram invoice
    # недействительным. Рублёвая сумма всегда остаётся в amount.
    provider_amount = Column(Integer, nullable=True)
    payment_method = Column(String(50), nullable=False)
    payment_id = Column(String(255), nullable=True)  # ID в платежной системе
    # Внутренний идентификатор счета у провайдера. Он никогда не строится из
    # user_id/order_id и используется для безопасной сверки webhook.
    external_order_id = Column(String(128), nullable=True, unique=True, index=True)
    payment_url = Column(Text, nullable=True)
    idempotency_key = Column(String(64), nullable=True, unique=True, index=True)
    # PENDING, STARS_AUTHORIZED, SUCCESS, FAILED.  STARS_AUTHORIZED means
    # Telegram accepted pre-checkout and successful_payment may still arrive.
    status = Column(String(50), default="PENDING", nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    batch_id = Column(Integer, ForeignKey("order_batches.id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    # Applies to invoices that have no order reservation, such as balance
    # top-ups.  A stale external invoice must not block a new payment forever.
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint('amount > 0', name='check_amount_positive'),
        Index('idx_payment_user_status', 'user_id', 'status'),
        Index('idx_payment_expires_at', 'expires_at'),
        # Защита от повторных webhook одного и того же счета провайдера.
        UniqueConstraint('payment_method', 'payment_id', name='uq_payment_method_payment_id'),
        # Ровно одна финальная успешная оплата может принадлежать заказу.
        # Декларация нужна и в metadata, чтобы autogenerate Alembic не
        # предложил удалить индекс, созданный финансовой миграцией.
        Index(
            'uq_payments_success_order',
            'order_id',
            unique=True,
            postgresql_where=text("order_id IS NOT NULL AND status = 'SUCCESS'"),
            sqlite_where=text("order_id IS NOT NULL AND status = 'SUCCESS'"),
        ),
        Index(
            'uq_payments_success_batch',
            'batch_id',
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL AND status = 'SUCCESS'"),
            sqlite_where=text("batch_id IS NOT NULL AND status = 'SUCCESS'"),
        ),
    )


class ReferralTransaction(Base):
    """Реферальная транзакция"""
    __tablename__ = "referral_transactions"

    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    referred_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    amount = Column(MONEY, nullable=False)  # Сумма заказа
    commission = Column(MONEY, nullable=False)  # Комиссия реферера
    cashback = Column(MONEY, default=ZERO_MONEY, nullable=False)  # Кешбек приглашённому пользователю
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('order_id', 'referrer_id', name='uq_referral_order_referrer'),
    )


class Log(Base):
    """Лог ошибок"""
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    level = Column(String(20), nullable=False)  # ERROR, WARNING, INFO
    message = Column(Text, nullable=False)
    user_id = Column(Integer, nullable=True)
    traceback = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_level_created', 'level', 'created_at'),
    )


class Setting(Base):
    """Настройки бота (тексты, контакты)"""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class GlobalDiscountTarget(Base):
    """Выбранные категории или товары для единственной глобальной скидки."""
    __tablename__ = "global_discount_targets"

    id = Column(Integer, primary_key=True)
    scope = Column(String(20), nullable=False)
    target_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", "target_id", name="uq_global_discount_target"),
        Index("idx_global_discount_target_scope", "scope", "target_id"),
    )


class PersonalDiscountTarget(Base):
    """Категории или товары, выбранные для скидки конкретного пользователя."""
    __tablename__ = "personal_discount_targets"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scope = Column(String(20), nullable=False)
    target_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "scope", "target_id",
            name="uq_personal_discount_target",
        ),
        Index(
            "idx_personal_discount_target_user_scope",
            "user_id", "scope", "target_id",
        ),
    )


class Refund(Base):
    """Возврат средств"""
    __tablename__ = "refunds"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(MONEY, nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(50), default="PENDING", nullable=False)  # PENDING, APPROVED, REJECTED
    processed_at = Column(DateTime, nullable=True)
    processed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint('amount > 0', name='check_refund_amount_positive'),
        Index('idx_refund_status', 'status'),
    )


class Promotion(Base):
    """Промоакции и скидки"""
    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    discount_type = Column(String(50), nullable=False)  # PERCENT, FIXED
    discount_value = Column(MONEY, nullable=False)
    min_quantity = Column(Integer, default=1, nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)  # NULL = для всех товаров
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint('discount_value > 0', name='check_discount_positive'),
    )


class Coupon(Base):
    """Промокоды"""
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_type = Column(String(50), nullable=False)  # PERCENT, FIXED
    percent_mode = Column(String(20), nullable=True)  # ONE_TIME, PERMANENT; only for PERCENT
    discount_value = Column(MONEY, nullable=False)
    max_uses = Column(Integer, nullable=True)  # Общий лимит, NULL = безлимит
    max_uses_per_user = Column(Integer, nullable=True)  # Личный лимит, NULL = безлимит
    used_count = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_until = Column(DateTime, nullable=True)  # NULL = бессрочно
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint('discount_value > 0', name='check_coupon_discount_positive'),
    )


class CouponUsage(Base):
    """Счётчик применений конкретного промокода одним пользователем."""
    __tablename__ = "coupon_usages"

    id = Column(Integer, primary_key=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    used_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("coupon_id", "user_id", name="uq_coupon_usage_coupon_user"),
        Index("idx_coupon_usage_user", "user_id"),
    )


class CouponActivation(Base):
    """A percentage coupon activated by a user.

    Fixed-value coupons credit the balance immediately.  Percentage coupons
    are activated here so a one-time discount can survive an abandoned order
    and a permanent discount can be applied to future purchases.
    """
    __tablename__ = "coupon_activations"

    id = Column(Integer, primary_key=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # NULL means a permanent percentage discount; 0 means it is exhausted.
    uses_remaining = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("coupon_id", "user_id", name="uq_coupon_activation_coupon_user"),
        CheckConstraint("uses_remaining IS NULL OR uses_remaining >= 0", name="check_coupon_activation_uses"),
        Index("idx_coupon_activation_user_active", "user_id", "is_active"),
    )


class BalanceTransfer(Base):
    """Перевод средств между внутренними балансами пользователей."""
    __tablename__ = "balance_transfers"

    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(MONEY, nullable=False)
    message = Column(Text, nullable=True)
    # One Telegram callback may be delivered or clicked more than once.  The
    # durable key turns an otherwise duplicate debit into a no-op.
    idempotency_key = Column(String(64), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="check_balance_transfer_amount_positive"),
        Index("idx_balance_transfer_sender", "sender_id", "created_at"),
        Index("idx_balance_transfer_recipient", "recipient_id", "created_at"),
    )


class AuditLog(Base):
    """Журнал аудита (действия администраторов)"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(100), nullable=False)  # Тип действия
    entity_type = Column(String(50), nullable=True)  # Тип сущности (order, product, user)
    entity_id = Column(Integer, nullable=True)  # ID сущности
    details = Column(Text, nullable=True)  # Детали действия
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_audit_user_created', 'user_id', 'created_at'),
        Index('idx_audit_action', 'action'),
    )
