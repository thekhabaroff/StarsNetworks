"""Add Telegram Stars delivery fields and seed the single catalog item.

The generic Digital Networks schema is kept intact for historical orders, but
Stars Networks exposes one active product whose fulfillment is performed by
Fragment rather than by the account warehouse.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from alembic import op
import sqlalchemy as sa


revision = "20260916_12"
down_revision = "20260809_11"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _seed_price() -> str:
    raw = os.getenv("STARS_PRODUCT_PRICE_RUBLES", "1.50").strip()
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        value = Decimal("1.50")
    return str(max(value, Decimal("0.01")))


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "products" not in tables or "orders" not in tables:
        return

    product_columns = _columns(bind, "products")
    if "delivery_type" not in product_columns:
        op.add_column(
            "products",
            sa.Column(
                "delivery_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'account'"),
            ),
        )

    order_columns = _columns(bind, "orders")
    additions = (
        ("target_username", sa.String(length=32), None),
        ("fulfillment_status", sa.String(length=20), "'PENDING'"),
        ("fulfillment_attempts", sa.Integer(), "0"),
        ("fulfillment_error", sa.Text(), None),
        ("delivered_at", sa.DateTime(), None),
    )
    for name, column_type, default in additions:
        if name not in order_columns:
            kwargs = {"nullable": name in {"target_username", "fulfillment_error", "delivered_at"}}
            if default is not None:
                kwargs["server_default"] = sa.text(default)
            op.add_column("orders", sa.Column(name, column_type, **kwargs))

    # Keep old products as history, but make sure the only newly purchasable
    # item is the Stars product.  We do not delete rows or alter old orders.
    if "categories" not in tables:
        return
    category_id = bind.execute(
        sa.text("SELECT id FROM categories WHERE name = :name LIMIT 1"),
        {"name": "⭐ Telegram Stars"},
    ).scalar_one_or_none()
    if category_id is None:
        category_id = bind.execute(
            sa.text(
                "INSERT INTO categories (name, description, is_active) "
                "VALUES (:name, :description, true) RETURNING id"
            ),
            {
                "name": "⭐ Telegram Stars",
                "description": "Пополнение Telegram Stars с автоматической отправкой через Fragment.",
            },
        ).scalar_one()

    stars_product_id = bind.execute(
        sa.text("SELECT id FROM products WHERE delivery_type = 'telegram_stars' ORDER BY id LIMIT 1")
    ).scalar_one_or_none()
    if stars_product_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO products "
                "(name, description, price, category_id, stock_count, is_active, format_info, recommendations, delivery_type) "
                "VALUES (:name, :description, :price, :category_id, 0, true, :format_info, :recommendations, 'telegram_stars')"
            ),
            {
                "name": "Telegram Stars",
                "description": "Telegram Stars с автоматической отправкой на указанный @username.",
                "price": _seed_price(),
                "category_id": category_id,
                "format_info": "Звёзды Telegram (XTR)",
                "recommendations": "Проверьте username получателя перед оплатой. После оплаты отправка выполняется автоматически.",
            },
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE products SET is_active = true, category_id = :category_id "
                "WHERE id = :product_id"
            ),
            {"category_id": category_id, "product_id": stars_product_id},
        )


def downgrade() -> None:
    raise NotImplementedError("The Stars Networks catalog migration is forward-only.")
