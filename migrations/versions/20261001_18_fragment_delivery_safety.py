"""Track the start of each paid Fragment delivery attempt."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261001_18"
down_revision = ("20260918_17", "20260729_08")
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "orders" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("orders")}
    if "fulfillment_started_at" not in columns:
        op.add_column("orders", sa.Column("fulfillment_started_at", sa.DateTime(), nullable=True))
    # Before UNKNOWN existed, timeouts and network failures were persisted as
    # FAILED and exposed a retry button. Those attempts may already have been
    # accepted by Fragment, so migrate known ambiguous errors out of retry.
    op.execute(sa.text(
        """
        UPDATE orders
        SET fulfillment_status = 'UNKNOWN'
        WHERE fulfillment_status = 'FAILED'
          AND fulfillment_error IS NOT NULL
          AND (
              lower(fulfillment_error) LIKE '%fragment request timed out%'
              OR lower(fulfillment_error) LIKE '%fragment network error%'
              OR lower(fulfillment_error) LIKE '%fragment returned an invalid response%'
              OR lower(fulfillment_error) LIKE '%fragment http 408:%'
              OR lower(fulfillment_error) LIKE '%fragment http 409:%'
              OR lower(fulfillment_error) LIKE '%fragment http 5__: %'
          )
        """
    ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "orders" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("orders")}
        if "fulfillment_started_at" in columns:
            op.drop_column("orders", "fulfillment_started_at")
