"""Frozen baseline schema for the first Alembic revision.

This is deliberately independent from ``database.models``.  A fresh database
must not acquire columns merely because a future Python model was changed; all
schema changes after this point belong to explicit Alembic revisions.
"""
from __future__ import annotations

import sqlalchemy as sa


metadata = sa.MetaData()

users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("telegram_id", sa.BigInteger, nullable=False, unique=True, index=True),
    sa.Column("username", sa.String(255)),
    sa.Column("first_name", sa.String(255)),
    sa.Column("balance", sa.Float, nullable=False, default=0.0),
    sa.Column("is_blocked", sa.Boolean, nullable=False, default=False),
    sa.Column("referral_code", sa.String(50), unique=True, index=True),
    sa.Column("referred_by", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column("role", sa.String(50), nullable=False, default="user"),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)

categories = sa.Table(
    "categories", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(255), nullable=False, unique=True),
    sa.Column("description", sa.Text),
    sa.Column("parent_id", sa.Integer, sa.ForeignKey("categories.id")),
    sa.Column("is_active", sa.Boolean, nullable=False, default=True),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)

products = sa.Table(
    "products", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id"), nullable=False),
    sa.Column("stock_count", sa.Integer, nullable=False, default=0),
    sa.Column("is_active", sa.Boolean, nullable=False, default=True),
    sa.Column("format_info", sa.Text),
    sa.Column("recommendations", sa.Text),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("price >= 0", name="check_price_positive"),
    sa.CheckConstraint("stock_count >= 0", name="check_stock_positive"),
)

orders = sa.Table(
    "orders", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("price_per_unit", sa.Float, nullable=False),
    sa.Column("discount", sa.Float, nullable=False, default=0.0),
    sa.Column("discount_amount", sa.Float, nullable=False, default=0.0),
    sa.Column("total_amount", sa.Float, nullable=False),
    sa.Column("status", sa.String(50), nullable=False, default="ОЖИДАЕТ ОПЛАТЫ"),
    sa.Column("payment_method", sa.String(50)),
    sa.Column("payment_id", sa.String(255)),
    sa.Column("reserved_until", sa.DateTime),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.Column("paid_at", sa.DateTime),
    sa.Column("completed_at", sa.DateTime),
    sa.CheckConstraint("quantity > 0", name="check_quantity_positive"),
    sa.CheckConstraint("total_amount >= 0", name="check_amount_positive"),
)
sa.Index("idx_user_status", orders.c.user_id, orders.c.status)
sa.Index("idx_status", orders.c.status)

accounts = sa.Table(
    "accounts", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
    sa.Column("account_data", sa.Text, nullable=False),
    sa.Column("is_sold", sa.Boolean, nullable=False, default=False),
    sa.Column("sold_at", sa.DateTime),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id")),
    sa.Column("is_blocked", sa.Boolean, nullable=False, default=False),
    sa.Column("blocked_at", sa.DateTime),
    sa.Column("blocked_by", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)
sa.Index("idx_product_sold", accounts.c.product_id, accounts.c.is_sold)
sa.Index("idx_account_order_id", accounts.c.order_id)

stock_notifications = sa.Table(
    "stock_notifications", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
    sa.Column("is_notified", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)
sa.Index("idx_user_product", stock_notifications.c.user_id, stock_notifications.c.product_id)

payments = sa.Table(
    "payments", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("amount", sa.Float, nullable=False),
    sa.Column("provider_amount", sa.Integer),
    sa.Column("payment_method", sa.String(50), nullable=False),
    sa.Column("payment_id", sa.String(255)),
    sa.Column("external_order_id", sa.String(128), unique=True, index=True),
    sa.Column("payment_url", sa.Text),
    sa.Column("idempotency_key", sa.String(64), unique=True, index=True),
    sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.Column("completed_at", sa.DateTime),
    sa.CheckConstraint("amount > 0", name="check_amount_positive"),
    sa.UniqueConstraint("payment_method", "payment_id", name="uq_payment_method_payment_id"),
)
sa.Index("idx_payment_user_status", payments.c.user_id, payments.c.status)

referral_transactions = sa.Table(
    "referral_transactions", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("referrer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("referred_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
    sa.Column("amount", sa.Float, nullable=False),
    sa.Column("commission", sa.Float, nullable=False),
    sa.Column("cashback", sa.Float, nullable=False, default=0.0),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("order_id", "referrer_id", name="uq_referral_order_referrer"),
)

logs = sa.Table(
    "logs", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("level", sa.String(20), nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("user_id", sa.Integer),
    sa.Column("traceback", sa.Text),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)
sa.Index("idx_level_created", logs.c.level, logs.c.created_at)

settings = sa.Table(
    "settings", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("key", sa.String(100), nullable=False, unique=True),
    sa.Column("value", sa.Text),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)

global_discount_targets = sa.Table(
    "global_discount_targets", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("scope", sa.String(20), nullable=False),
    sa.Column("target_id", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("scope", "target_id", name="uq_global_discount_target"),
)
sa.Index("idx_global_discount_target_scope", global_discount_targets.c.scope, global_discount_targets.c.target_id)

refunds = sa.Table(
    "refunds", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("amount", sa.Float, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
    sa.Column("processed_at", sa.DateTime),
    sa.Column("processed_by", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("amount > 0", name="check_refund_amount_positive"),
)
sa.Index("idx_refund_status", refunds.c.status)

promotions = sa.Table(
    "promotions", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("discount_type", sa.String(50), nullable=False),
    sa.Column("discount_value", sa.Float, nullable=False),
    sa.Column("min_quantity", sa.Integer, nullable=False, default=1),
    sa.Column("start_date", sa.DateTime, nullable=False),
    sa.Column("end_date", sa.DateTime, nullable=False),
    sa.Column("is_active", sa.Boolean, nullable=False, default=True),
    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("discount_value > 0", name="check_discount_positive"),
)

coupons = sa.Table(
    "coupons", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(255)),
    sa.Column("code", sa.String(50), nullable=False, unique=True, index=True),
    sa.Column("discount_type", sa.String(50), nullable=False),
    sa.Column("percent_mode", sa.String(20)),
    sa.Column("discount_value", sa.Float, nullable=False),
    sa.Column("max_uses", sa.Integer),
    sa.Column("max_uses_per_user", sa.Integer),
    sa.Column("used_count", sa.Integer, nullable=False, default=0),
    sa.Column("is_active", sa.Boolean, nullable=False, default=True),
    sa.Column("valid_from", sa.DateTime, nullable=False),
    sa.Column("valid_until", sa.DateTime),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("discount_value > 0", name="check_coupon_discount_positive"),
)

coupon_usages = sa.Table(
    "coupon_usages", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("coupon_id", sa.Integer, sa.ForeignKey("coupons.id"), nullable=False),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("used_count", sa.Integer, nullable=False, default=0),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("coupon_id", "user_id", name="uq_coupon_usage_coupon_user"),
)
sa.Index("idx_coupon_usage_user", coupon_usages.c.user_id)

balance_transfers = sa.Table(
    "balance_transfers", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("sender_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("recipient_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("amount", sa.Float, nullable=False),
    sa.Column("message", sa.Text),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("amount > 0", name="check_balance_transfer_amount_positive"),
)
sa.Index("idx_balance_transfer_sender", balance_transfers.c.sender_id, balance_transfers.c.created_at)
sa.Index("idx_balance_transfer_recipient", balance_transfers.c.recipient_id, balance_transfers.c.created_at)

audit_logs = sa.Table(
    "audit_logs", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("action", sa.String(100), nullable=False),
    sa.Column("entity_type", sa.String(50)),
    sa.Column("entity_id", sa.Integer),
    sa.Column("details", sa.Text),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
)
sa.Index("idx_audit_user_created", audit_logs.c.user_id, audit_logs.c.created_at)
sa.Index("idx_audit_action", audit_logs.c.action)
