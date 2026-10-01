"""Add cashback amount to referral transactions.

Revision ID: 20260729_02
Revises: 20260728_01
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_02"
down_revision = "20260728_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "referral_transactions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("referral_transactions")}
    if "cashback" not in columns:
        op.add_column(
            "referral_transactions",
            sa.Column("cashback", sa.Float(), nullable=False, server_default=sa.text("0")),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("referral_transactions", "cashback", server_default=None)


def downgrade() -> None:
    raise NotImplementedError("The referral cashback migration is forward-only.")
