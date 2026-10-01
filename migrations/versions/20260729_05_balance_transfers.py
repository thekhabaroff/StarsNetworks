"""Add an audit table for balance transfers.

Revision ID: 20260729_05
Revises: 20260729_04
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_05"
down_revision = "20260729_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "balance_transfers" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "balance_transfers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="check_balance_transfer_amount_positive"),
    )
    op.create_index("idx_balance_transfer_sender", "balance_transfers", ["sender_id", "created_at"])
    op.create_index("idx_balance_transfer_recipient", "balance_transfers", ["recipient_id", "created_at"])


def downgrade() -> None:
    raise NotImplementedError("The balance transfer migration is forward-only.")
