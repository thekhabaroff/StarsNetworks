"""Rename persisted payment setting keys to the current .env names.

Revision ID: 20260729_06
Revises: 20260729_05
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_06"
down_revision = "20260729_05"
branch_labels = None
depends_on = None


RENAMED_KEYS = {
    "payment.LAVA_PROJECT_ID": "payment.LAVA_ID",
    "payment.CRYPTOBOT_API_TOKEN": "payment.CRYPTOBOT_API",
}


def upgrade() -> None:
    """Keep payment credentials entered before the environment rename."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "settings" not in inspector.get_table_names():
        return

    for old_key, new_key in RENAMED_KEYS.items():
        old_exists = bind.execute(
            sa.text("SELECT 1 FROM settings WHERE key = :key"), {"key": old_key}
        ).scalar()
        if not old_exists:
            continue
        new_exists = bind.execute(
            sa.text("SELECT 1 FROM settings WHERE key = :key"), {"key": new_key}
        ).scalar()
        if new_exists:
            bind.execute(
                sa.text("DELETE FROM settings WHERE key = :key"), {"key": old_key}
            )
        else:
            bind.execute(
                sa.text("UPDATE settings SET key = :new_key WHERE key = :old_key"),
                {"old_key": old_key, "new_key": new_key},
            )


def downgrade() -> None:
    raise NotImplementedError("The migration is forward-only.")
