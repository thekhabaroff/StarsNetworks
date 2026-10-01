"""Add personal permanent and temporary user discounts.

Revision ID: 20260808_10
Revises: 20260801_09
Create Date: 2026-08-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_10"
down_revision = "20260801_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "personal_discount" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "personal_discount",
                sa.Numeric(9, 4),
                nullable=False,
                server_default="0",
            ),
        )
    if "personal_discount_until" not in columns:
        op.add_column(
            "users",
            sa.Column("personal_discount_until", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    raise NotImplementedError("The migration is forward-only to preserve user settings.")
