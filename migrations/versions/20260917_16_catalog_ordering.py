"""Add persistent ordering for catalog categories and products."""
from __future__ import annotations

from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "20260917_16"
down_revision = "20260916_15"
branch_labels = None
depends_on = None


def _table_columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _add_order_column(bind, table: str) -> None:
    if table not in sa.inspect(bind).get_table_names():
        return
    if "sort_order" not in _table_columns(bind, table):
        op.add_column(
            table,
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def _backfill_order(bind, table: str, parent_column: str) -> None:
    if table not in sa.inspect(bind).get_table_names():
        return
    rows = bind.execute(
        sa.text(f"SELECT id, {parent_column} FROM {table}")
    ).all()
    siblings: dict[object, list[int]] = defaultdict(list)
    for item_id, parent_id in rows:
        siblings[parent_id].append(item_id)
    for ids in siblings.values():
        for position, item_id in enumerate(sorted(ids)):
            bind.execute(
                sa.text(f"UPDATE {table} SET sort_order = :position WHERE id = :item_id"),
                {"position": position, "item_id": item_id},
            )


def upgrade() -> None:
    bind = op.get_bind()
    _add_order_column(bind, "categories")
    _add_order_column(bind, "products")
    _backfill_order(bind, "categories", "parent_id")
    _backfill_order(bind, "products", "category_id")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" in inspector.get_table_names() and "sort_order" in _table_columns(bind, "products"):
        op.drop_column("products", "sort_order")
    if "categories" in inspector.get_table_names() and "sort_order" in _table_columns(bind, "categories"):
        op.drop_column("categories", "sort_order")
