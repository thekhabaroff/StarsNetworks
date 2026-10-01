"""Add persistent carts and grouped multi-recipient checkouts."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260918_17"
down_revision = "20260917_16"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "carts" not in tables:
        op.create_table(
            "carts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.CheckConstraint("status IN ('DRAFT', 'CHECKED_OUT')", name="check_cart_status"),
            sa.UniqueConstraint("user_id", name="uq_cart_user"),
        )
        op.create_index("idx_cart_user_status", "carts", ["user_id", "status"])

    tables = _tables(bind)
    if "cart_items" not in tables:
        op.create_table(
            "cart_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cart_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("target_username", sa.String(length=32), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_price", sa.Numeric(18, 2), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
            sa.CheckConstraint("quantity > 0", name="check_cart_item_quantity_positive"),
            sa.CheckConstraint("unit_price > 0", name="check_cart_item_price_positive"),
        )
        op.create_index("idx_cart_item_cart", "cart_items", ["cart_id", "id"])
        op.create_index("idx_cart_item_target", "cart_items", ["cart_id", "target_username"])

    tables = _tables(bind)
    if "order_batches" not in tables:
        op.create_table(
            "order_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="PENDING_PAYMENT"),
            sa.Column("subtotal_amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("discount_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
            sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("coupon_activation_id", sa.Integer(), nullable=True),
            sa.Column("reserved_until", sa.DateTime(), nullable=True),
            sa.Column("payment_method", sa.String(length=50), nullable=True),
            sa.Column("payment_id", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["coupon_activation_id"], ["coupon_activations.id"]),
            sa.CheckConstraint(
                "status IN ('PENDING_PAYMENT', 'PAID', 'PARTIAL', 'COMPLETED', 'CANCELLED')",
                name="check_order_batch_status",
            ),
            sa.CheckConstraint("subtotal_amount > 0", name="check_order_batch_subtotal_positive"),
            sa.CheckConstraint("total_amount > 0", name="check_order_batch_total_positive"),
            sa.CheckConstraint("discount_amount >= 0", name="check_order_batch_discount_nonnegative"),
        )
        op.create_index("idx_order_batch_user_status", "order_batches", ["user_id", "status"])
        op.create_index("idx_order_batch_reserved_until", "order_batches", ["reserved_until"])

    if "orders" in tables and "batch_id" not in _columns(bind, "orders"):
        op.add_column("orders", sa.Column("batch_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_orders_batch_id", "orders", "order_batches", ["batch_id"], ["id"])
        op.create_index("idx_orders_batch_id", "orders", ["batch_id"])

    if "payments" in tables and "batch_id" not in _columns(bind, "payments"):
        op.add_column("payments", sa.Column("batch_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_payments_batch_id", "payments", "order_batches", ["batch_id"], ["id"])
        op.create_index("idx_payments_batch_id", "payments", ["batch_id"])


def downgrade() -> None:
    raise NotImplementedError("The cart and batch checkout migration is forward-only.")
