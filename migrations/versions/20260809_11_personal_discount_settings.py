"""Add full per-user discount settings.

Revision ID: 20260809_11
Revises: 20260808_10
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260809_11"
down_revision = "20260808_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    additions = (
        ("personal_discount_enabled", sa.Boolean(), "false"),
        ("personal_discount_scope", sa.String(length=20), "'ALL'"),
        ("personal_discount_mode", sa.String(length=20), "'MAXIMUM'"),
        ("personal_discount_type", sa.String(length=20), "'PERCENT'"),
    )
    for name, column_type, default in additions:
        if name not in columns:
            op.add_column(
                "users",
                sa.Column(name, column_type, nullable=False, server_default=sa.text(default)),
            )

    # Existing non-zero discounts remain active after this migration.
    op.execute(
        sa.text(
            "UPDATE users SET personal_discount_enabled = true, "
            "personal_discount_mode = 'STACK' "
            "WHERE personal_discount > 0"
        )
    )

    if "personal_discount_targets" not in tables:
        op.create_table(
            "personal_discount_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "user_id", "scope", "target_id",
                name="uq_personal_discount_target",
            ),
        )
        op.create_index(
            "idx_personal_discount_target_user_scope",
            "personal_discount_targets",
            ["user_id", "scope", "target_id"],
        )


def downgrade() -> None:
    raise NotImplementedError("The migration is forward-only to preserve user discounts.")
