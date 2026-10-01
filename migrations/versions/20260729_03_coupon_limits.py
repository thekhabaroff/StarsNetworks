"""Add per-user coupon limits and unlimited validity.

Revision ID: 20260729_03
Revises: 20260729_02
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_03"
down_revision = "20260729_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "coupons" in tables:
        columns = {column["name"] for column in inspector.get_columns("coupons")}
        if "max_uses_per_user" not in columns:
            op.add_column("coupons", sa.Column("max_uses_per_user", sa.Integer(), nullable=True))
        if "valid_until" in columns:
            # SQLite has no ``ALTER COLUMN``.  Keep this historical revision
            # executable for local smoke tests as well as PostgreSQL by using
            # Alembic's safe table-copy operation on SQLite.
            if bind.dialect.name == "sqlite":
                with op.batch_alter_table("coupons", recreate="always") as batch_op:
                    batch_op.alter_column(
                        "valid_until",
                        existing_type=sa.DateTime(),
                        nullable=True,
                    )
            else:
                op.alter_column(
                    "coupons",
                    "valid_until",
                    existing_type=sa.DateTime(),
                    nullable=True,
                )

    if "coupon_usages" not in tables:
        op.create_table(
            "coupon_usages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("coupon_id", sa.Integer(), sa.ForeignKey("coupons.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("coupon_id", "user_id", name="uq_coupon_usage_coupon_user"),
        )
        op.create_index("idx_coupon_usage_user", "coupon_usages", ["user_id"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("The coupon limits migration is forward-only.")
