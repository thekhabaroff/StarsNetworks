"""Use automatic Fragment cost pricing for the virtual catalog by default."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260916_15"
down_revision = "20260916_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("products")}
    if not {"delivery_type", "pricing_mode", "markup_percent"}.issubset(columns):
        return

    # Only the system virtual products switch to automatic mode. Any regular
    # product deliberately set to FIXED remains untouched, and the admin can
    # still switch a virtual product back to a fixed fallback price.
    op.execute(
        sa.text(
            """
            UPDATE products
               SET pricing_mode = 'COST_PLUS',
                   markup_percent = COALESCE(markup_percent, 0)
             WHERE delivery_type IN ('telegram_stars', 'telegram_premium')
            """
        )
    )


def downgrade() -> None:
    raise NotImplementedError("The automatic Fragment pricing migration is forward-only.")

