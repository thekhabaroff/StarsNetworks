"""Add global discount targets and order discount amounts.

Revision ID: 20260729_07
Revises: 20260729_06
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_07"
down_revision = "20260729_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "categories" in tables:
        columns = {column["name"] for column in inspector.get_columns("categories")}
        if "parent_id" not in columns:
            with op.batch_alter_table("categories") as batch:
                batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
                batch.create_foreign_key(
                    "fk_categories_parent_id", "categories", ["parent_id"], ["id"]
                )

    if "orders" in tables:
        columns = {column["name"] for column in inspector.get_columns("orders")}
        if "discount_amount" not in columns:
            with op.batch_alter_table("orders") as batch:
                batch.add_column(
                    sa.Column(
                        "discount_amount", sa.Float(), nullable=False,
                        server_default=sa.text("0"),
                    )
                )

    if "global_discount_targets" not in tables:
        op.create_table(
            "global_discount_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("scope", "target_id", name="uq_global_discount_target"),
        )
        op.create_index(
            "idx_global_discount_target_scope", "global_discount_targets", ["scope", "target_id"]
        )


def downgrade() -> None:
    raise NotImplementedError("The migration is forward-only.")
