"""Add Telegram Premium delivery metadata and default plans."""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from alembic import op
import sqlalchemy as sa


revision = "20260916_13"
down_revision = "20260916_12"
branch_labels = None
depends_on = None


PREMIUM_DELIVERY_TYPE = "telegram_premium"
PREMIUM_CATEGORY_NAME = "💎 Telegram Premium"
PREMIUM_PRICES = {
    3: ("PREMIUM_3_MONTHS_PRICE_RUBLES", "1200.00"),
    6: ("PREMIUM_6_MONTHS_PRICE_RUBLES", "1600.00"),
    12: ("PREMIUM_12_MONTHS_PRICE_RUBLES", "2900.00"),
}


def _price(env_key: str, default: str) -> str:
    raw = os.getenv(env_key, default).strip().replace(",", ".")
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        value = Decimal(default)
    return str(max(value, Decimal("0.01")))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "products" not in tables or "categories" not in tables:
        return

    product_columns = {item["name"] for item in inspector.get_columns("products")}
    if "premium_months" not in product_columns:
        op.add_column("products", sa.Column("premium_months", sa.Integer(), nullable=True))

    category_id = bind.execute(
        sa.text("SELECT id FROM categories WHERE name = :name LIMIT 1"),
        {"name": PREMIUM_CATEGORY_NAME},
    ).scalar_one_or_none()
    if category_id is None:
        category_id = bind.execute(
            sa.text(
                "INSERT INTO categories (name, description, is_active) "
                "VALUES (:name, :description, true) RETURNING id"
            ),
            {
                "name": PREMIUM_CATEGORY_NAME,
                "description": "Подарочные подписки Telegram Premium с автоматической отправкой через Fragment.",
            },
        ).scalar_one()

    for months, (env_key, default_price) in PREMIUM_PRICES.items():
        exists = bind.execute(
            sa.text(
                "SELECT id FROM products "
                "WHERE delivery_type = :delivery_type AND premium_months = :months "
                "ORDER BY id LIMIT 1"
            ),
            {
                "delivery_type": PREMIUM_DELIVERY_TYPE,
                "months": months,
            },
        ).scalar_one_or_none()
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO products "
                    "(name, description, price, category_id, stock_count, is_active, "
                    "format_info, recommendations, delivery_type, premium_months) "
                    "VALUES (:name, :description, :price, :category_id, 0, true, "
                    ":format_info, :recommendations, :delivery_type, :months)"
                ),
                {
                    "name": f"💎 Telegram Premium — {months} мес.",
                    "description": f"Подарочная подписка Telegram Premium на {months} месяца.",
                    "price": _price(env_key, default_price),
                    "category_id": category_id,
                    "format_info": f"Telegram Premium ({months} мес.)",
                    "recommendations": "Проверьте username получателя. После оплаты подписка отправляется автоматически.",
                    "delivery_type": PREMIUM_DELIVERY_TYPE,
                    "months": months,
                },
            )
        else:
            bind.execute(
                sa.text(
                    "UPDATE products SET category_id = :category_id, is_active = true "
                    "WHERE id = :id"
                ),
                {"category_id": category_id, "id": exists},
            )


def downgrade() -> None:
    raise NotImplementedError("The Premium catalog migration is forward-only.")
