"""Store the issued provider amount for rate-dependent payments.

Revision ID: 20260729_08
Revises: 20260729_07
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_08"
down_revision = "20260729_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "payments" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("payments")}
    if "provider_amount" not in columns:
        op.add_column("payments", sa.Column("provider_amount", sa.Integer(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("The migration is forward-only to preserve payment history.")
