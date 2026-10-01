"""Add coupon names and percentage reward mode.

Revision ID: 20260729_04
Revises: 20260729_03
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_04"
down_revision = "20260729_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "coupons" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("coupons")}
    if "name" not in columns:
        op.add_column("coupons", sa.Column("name", sa.String(length=255), nullable=True))
        op.execute("UPDATE coupons SET name = code WHERE name IS NULL")
    if "percent_mode" not in columns:
        op.add_column("coupons", sa.Column("percent_mode", sa.String(length=20), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("The coupon editor migration is forward-only.")
