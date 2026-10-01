"""Add product cost-plus pricing fields."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260916_14"
down_revision = "20260916_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "products" not in tables:
        return

    columns = {item["name"] for item in inspector.get_columns("products")}
    if "pricing_mode" not in columns:
        op.add_column(
            "products",
            sa.Column(
                "pricing_mode",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'FIXED'"),
            ),
        )
    if "cost_price" not in columns:
        op.add_column("products", sa.Column("cost_price", sa.Numeric(18, 2), nullable=True))
    if "markup_percent" not in columns:
        op.add_column(
            "products",
            sa.Column(
                "markup_percent",
                sa.Numeric(9, 4),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    # Constraints are intentionally added only once and do not rewrite any
    # existing prices: all historical products stay in fixed-price mode.
    constraint_names = {
        item.get("name")
        for item in inspector.get_check_constraints("products")
        if item.get("name")
    }
    if "check_product_pricing_mode" not in constraint_names:
        op.create_check_constraint(
            "check_product_pricing_mode",
            "products",
            "pricing_mode IN ('FIXED', 'COST_PLUS')",
        )
    if "check_product_cost_price_positive" not in constraint_names:
        op.create_check_constraint(
            "check_product_cost_price_positive",
            "products",
            "cost_price IS NULL OR cost_price >= 0",
        )
    if "check_product_markup_nonnegative" not in constraint_names:
        op.create_check_constraint(
            "check_product_markup_nonnegative",
            "products",
            "markup_percent >= 0",
        )


def downgrade() -> None:
    raise NotImplementedError("The cost-plus pricing migration is forward-only.")
