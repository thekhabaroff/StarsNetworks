"""Make financial data exact and add durable financial invariants.

Revision ID: 20260801_09
Revises: 20260729_08
Create Date: 2026-08-01

This revision is deliberately self-contained: it does not import application
models or settings, and it never contains credentials or production data.  It
is therefore safe to keep in a public source repository.  Existing rows are
preserved.  A migration stops with a clear error if it encounters a non-finite
legacy float (``NaN``/``Infinity``) instead of silently changing a balance.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_09"
down_revision: Union[str, Sequence[str], None] = "20260729_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAYMENTS = "payments"
ORDERS = "orders"
BALANCE_TRANSFERS = "balance_transfers"
COUPON_ACTIVATIONS = "coupon_activations"
BALANCE_LEDGER = "balance_ledger"

PAYMENT_EXPIRY_INDEX = "idx_payment_expires_at"
TRANSFER_IDEMPOTENCY_INDEX = "ix_balance_transfers_idempotency_key"
SUCCESSFUL_ORDER_PAYMENT_INDEX = "uq_payments_success_order"
ORDER_COUPON_FK = "fk_orders_coupon_activation_id"
LEDGER_IMMUTABLE_FUNCTION = "digital_networks_balance_ledger_immutable"
LEDGER_UPDATE_TRIGGER = "trg_balance_ledger_no_update"
LEDGER_DELETE_TRIGGER = "trg_balance_ledger_no_delete"
LEDGER_AMOUNT_FINITE_CONSTRAINT = "check_balance_ledger_amount_finite"
LEDGER_BALANCE_AFTER_FINITE_CONSTRAINT = "check_balance_ledger_balance_after_finite"
PENDING_ORDER_AMOUNT_CONSTRAINT = "check_pending_order_amount_positive"
ACTIVE_PRODUCT_PRICE_CONSTRAINT = "check_active_product_price_positive"

# Monetary columns have a fixed two-decimal precision.  ``orders.discount`` is
# a percentage and keeps four decimal places.  The migration contains the
# complete list on purpose so newly added monetary fields cannot be converted
# accidentally just because a live ORM model changed.
NUMERIC_COLUMNS: tuple[tuple[str, str, int, int], ...] = (
    ("users", "balance", 18, 2),
    ("products", "price", 18, 2),
    ("orders", "price_per_unit", 18, 2),
    ("orders", "discount", 9, 4),
    ("orders", "discount_amount", 18, 2),
    ("orders", "total_amount", 18, 2),
    ("payments", "amount", 18, 2),
    ("referral_transactions", "amount", 18, 2),
    ("referral_transactions", "commission", 18, 2),
    ("referral_transactions", "cashback", 18, 2),
    ("refunds", "amount", 18, 2),
    ("promotions", "discount_value", 18, 2),
    ("coupons", "discount_value", 18, 2),
    ("balance_transfers", "amount", 18, 2),
)


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_map(bind, table_name: str) -> dict[str, dict]:
    return {
        column["name"]: column
        for column in sa.inspect(bind).get_columns(table_name)
    }


def _index_names(bind, table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(bind).get_indexes(table_name)
        if index.get("name")
    }


def _has_named_unique_index_or_constraint(bind, table_name: str, name: str) -> bool:
    """Return true only when the requested name is genuinely unique.

    Silently accepting a same-named ordinary index would leave an
    idempotency rule unenforced.  A conflicting object is reported by the
    caller instead of being dropped automatically.
    """
    inspector = sa.inspect(bind)
    for index in inspector.get_indexes(table_name):
        if index.get("name") == name:
            return bool(index.get("unique"))
    return any(
        constraint.get("name") == name
        for constraint in inspector.get_unique_constraints(table_name)
    )


def _named_unique_identity(
    bind,
    table_name: str,
    name: str,
    columns: tuple[str, ...],
) -> bool:
    """Check the *named* unique object, not merely any object with its name."""
    inspector = sa.inspect(bind)
    for index in inspector.get_indexes(table_name):
        if index.get("name") == name:
            return bool(index.get("unique")) and tuple(
                index.get("column_names") or ()
            ) == columns
    for constraint in inspector.get_unique_constraints(table_name):
        if constraint.get("name") == name:
            return tuple(constraint.get("column_names") or ()) == columns
    return False


def _named_object_exists(bind, table_name: str, name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(index.get("name") == name for index in inspector.get_indexes(table_name)) or any(
        constraint.get("name") == name
        for constraint in inspector.get_unique_constraints(table_name)
    )


def _has_conflicting_named_index_or_constraint(bind, table_name: str, name: str) -> bool:
    return (
        name in _index_names(bind, table_name)
        and not _has_named_unique_index_or_constraint(bind, table_name, name)
    )


def _require_no_duplicate_values(
    bind,
    table_name: str,
    column_name: str,
    label: str,
) -> None:
    """Fail before a new unique index would reject existing data."""
    duplicate_count = bind.execute(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM {table_name}
                WHERE {column_name} IS NOT NULL
                GROUP BY {column_name}
                HAVING COUNT(*) > 1
            ) AS duplicate_values
            """
        )
    ).scalar_one()
    if duplicate_count:
        raise RuntimeError(
            f"Cannot add {label}: table '{table_name}' contains "
            f"{int(duplicate_count)} duplicate non-empty value group(s) in "
            f"'{column_name}'. Resolve those rows manually and run "
            "'alembic upgrade head' again. No data was deleted."
        )


def _require_at_most_one_successful_payment_per_order(bind) -> None:
    duplicate_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM payments
                WHERE order_id IS NOT NULL AND status = 'SUCCESS'
                GROUP BY order_id
                HAVING COUNT(*) > 1
            ) AS duplicate_successes
            """
        )
    ).scalar_one()
    if duplicate_count:
        raise RuntimeError(
            "Cannot add the one-successful-payment-per-order rule: "
            f"{int(duplicate_count)} order(s) already have multiple SUCCESS "
            "payments. Reconcile these orders manually, then run 'alembic "
            "upgrade head' again. No payment rows were deleted."
        )


def _reject_nonfinite_postgresql_values(bind) -> None:
    """Do not turn an invalid old float into a different financial amount."""
    if bind.dialect.name != "postgresql":
        return

    tables = _table_names(bind)
    for table_name, column_name, _, _ in NUMERIC_COLUMNS:
        if table_name not in tables or column_name not in _column_map(bind, table_name):
            continue
        # PostgreSQL renders the only non-finite double precision values with
        # these three spellings.  All names here are static module constants.
        invalid_count = bind.execute(
            sa.text(
                f"""
                SELECT COUNT(*)
                FROM {table_name}
                WHERE {column_name}::text IN ('NaN', 'Infinity', '-Infinity')
                """
            )
        ).scalar_one()
        if invalid_count:
            raise RuntimeError(
                f"Cannot convert {table_name}.{column_name} to exact NUMERIC: "
                f"{int(invalid_count)} row(s) contain NaN or Infinity. "
                "Correct those values manually, then run 'alembic upgrade head' again."
            )


def _convert_numeric_columns_postgresql(bind) -> None:
    tables = _table_names(bind)
    for table_name, column_name, precision, scale in NUMERIC_COLUMNS:
        if table_name not in tables or column_name not in _column_map(bind, table_name):
            continue
        # Casting to NUMERIC removes binary floating-point representation;
        # ROUND makes the documented ruble/kopeck (or percentage) precision
        # explicit and deterministic for legacy values.
        op.execute(
            sa.text(
                f"""
                ALTER TABLE {table_name}
                ALTER COLUMN {column_name}
                TYPE NUMERIC({precision}, {scale})
                USING ROUND({column_name}::numeric, {scale})
                """
            )
        )


def _convert_numeric_columns_sqlite(bind) -> None:
    """Use batch mode because SQLite cannot ALTER a column type in place."""
    tables = _table_names(bind)
    for table_name in {item[0] for item in NUMERIC_COLUMNS}:
        if table_name not in tables:
            continue
        columns = _column_map(bind, table_name)
        conversions = [
            (column_name, precision, scale)
            for candidate_table, column_name, precision, scale in NUMERIC_COLUMNS
            if candidate_table == table_name and column_name in columns
        ]
        if not conversions:
            continue

        # SQLite uses dynamic typing.  Rounding old REAL values before the
        # table rebuild keeps their intended visible amount, while the rebuilt
        # schema declares NUMERIC for SQLAlchemy and future PostgreSQL parity.
        for column_name, _, scale in conversions:
            op.execute(
                sa.text(
                    f"UPDATE {table_name} SET {column_name} = "
                    f"ROUND({column_name}, {scale}) WHERE {column_name} IS NOT NULL"
                )
            )

        with op.batch_alter_table(table_name, recreate="always") as batch_op:
            for column_name, precision, scale in conversions:
                batch_op.alter_column(
                    column_name,
                    existing_type=columns[column_name]["type"],
                    type_=sa.Numeric(precision, scale),
                )


def _convert_numeric_columns(bind) -> None:
    _reject_nonfinite_postgresql_values(bind)
    if bind.dialect.name == "postgresql":
        _convert_numeric_columns_postgresql(bind)
    elif bind.dialect.name == "sqlite":
        _convert_numeric_columns_sqlite(bind)
    else:
        # PostgreSQL is the supported production database.  This fallback
        # keeps the migration understandable for a compatible SQL dialect.
        tables = _table_names(bind)
        for table_name, column_name, precision, scale in NUMERIC_COLUMNS:
            if table_name not in tables or column_name not in _column_map(bind, table_name):
                continue
            op.alter_column(
                table_name,
                column_name,
                existing_type=_column_map(bind, table_name)[column_name]["type"],
                type_=sa.Numeric(precision, scale),
            )


def _add_payment_expiry(bind) -> None:
    if PAYMENTS not in _table_names(bind):
        return
    columns = _column_map(bind, PAYMENTS)
    if "expires_at" not in columns:
        op.add_column(PAYMENTS, sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Existing successful invoices do not need an expiration timestamp.  Only
    # old top-up invoices without an order reservation receive the historic
    # fifteen-minute lifetime; newly issued invoices use the configured value
    # in application code.
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE payments
                SET expires_at = created_at + INTERVAL '15 minutes'
                WHERE expires_at IS NULL
                  AND order_id IS NULL
                  AND status IN ('PENDING', 'STARS_AUTHORIZED')
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                UPDATE payments
                SET expires_at = datetime(created_at, '+15 minutes')
                WHERE expires_at IS NULL
                  AND order_id IS NULL
                  AND status IN ('PENDING', 'STARS_AUTHORIZED')
                """
            )
        )

    if PAYMENT_EXPIRY_INDEX not in _index_names(bind, PAYMENTS):
        op.create_index(PAYMENT_EXPIRY_INDEX, PAYMENTS, ["expires_at"], unique=False)


def _add_transfer_idempotency_key(bind) -> None:
    if BALANCE_TRANSFERS not in _table_names(bind):
        return
    columns = _column_map(bind, BALANCE_TRANSFERS)
    if "idempotency_key" not in columns:
        op.add_column(
            BALANCE_TRANSFERS,
            sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        )

    if _named_object_exists(bind, BALANCE_TRANSFERS, TRANSFER_IDEMPOTENCY_INDEX) and not _named_unique_identity(
        bind,
        BALANCE_TRANSFERS,
        TRANSFER_IDEMPOTENCY_INDEX,
        ("idempotency_key",),
    ):
        raise RuntimeError(
            f"Cannot add the balance-transfer idempotency rule: existing object "
            f"'{TRANSFER_IDEMPOTENCY_INDEX}' does not uniquely cover "
            "idempotency_key. Rename or replace it in a reviewed manual "
            "migration, then retry."
        )
    if not _named_unique_identity(
        bind,
        BALANCE_TRANSFERS,
        TRANSFER_IDEMPOTENCY_INDEX,
        ("idempotency_key",),
    ):
        _require_no_duplicate_values(
            bind,
            BALANCE_TRANSFERS,
            "idempotency_key",
            "the balance-transfer idempotency rule",
        )
        op.create_index(
            TRANSFER_IDEMPOTENCY_INDEX,
            BALANCE_TRANSFERS,
            ["idempotency_key"],
            unique=True,
        )


def _foreign_key_exists(bind, table_name: str, constrained_columns: Iterable[str]) -> bool:
    expected = tuple(constrained_columns)
    return any(
        tuple(foreign_key.get("constrained_columns") or ()) == expected
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
    )


def _ensure_coupon_activations(bind) -> None:
    tables = _table_names(bind)
    if COUPON_ACTIVATIONS not in tables:
        op.create_table(
            COUPON_ACTIVATIONS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("coupon_id", sa.Integer(), sa.ForeignKey("coupons.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("uses_remaining", sa.Integer(), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "uses_remaining IS NULL OR uses_remaining >= 0",
                name="check_coupon_activation_uses",
            ),
            sa.UniqueConstraint(
                "coupon_id",
                "user_id",
                name="uq_coupon_activation_coupon_user",
            ),
        )
        op.create_index(
            "idx_coupon_activation_user_active",
            COUPON_ACTIVATIONS,
            ["user_id", "is_active"],
            unique=False,
        )
    else:
        required_columns = {
            "id",
            "coupon_id",
            "user_id",
            "uses_remaining",
            "is_active",
            "created_at",
            "updated_at",
        }
        missing_columns = required_columns - set(_column_map(bind, COUPON_ACTIVATIONS))
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise RuntimeError(
                "Existing table 'coupon_activations' has an unsupported partial schema; "
                f"missing: {missing}. Restore it or migrate it manually before upgrade."
            )
        if "idx_coupon_activation_user_active" not in _index_names(bind, COUPON_ACTIVATIONS):
            op.create_index(
                "idx_coupon_activation_user_active",
                COUPON_ACTIVATIONS,
                ["user_id", "is_active"],
                unique=False,
            )

    if ORDERS not in _table_names(bind):
        return
    order_columns = _column_map(bind, ORDERS)
    needs_column = "coupon_activation_id" not in order_columns
    needs_fk = not _foreign_key_exists(bind, ORDERS, ("coupon_activation_id",))
    if not needs_column and not needs_fk:
        return

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(ORDERS, recreate="always") as batch_op:
            if needs_column:
                batch_op.add_column(
                    sa.Column("coupon_activation_id", sa.Integer(), nullable=True)
                )
            if needs_fk:
                batch_op.create_foreign_key(
                    ORDER_COUPON_FK,
                    COUPON_ACTIVATIONS,
                    ["coupon_activation_id"],
                    ["id"],
                )
    else:
        if needs_column:
            op.add_column(ORDERS, sa.Column("coupon_activation_id", sa.Integer(), nullable=True))
        if needs_fk:
            op.create_foreign_key(
                ORDER_COUPON_FK,
                ORDERS,
                COUPON_ACTIVATIONS,
                ["coupon_activation_id"],
                ["id"],
            )


def _ensure_balance_ledger(bind) -> None:
    tables = _table_names(bind)
    if BALANCE_LEDGER not in tables:
        op.create_table(
            BALANCE_LEDGER,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("balance_after", sa.Numeric(18, 2), nullable=True),
            sa.Column("reason", sa.String(length=100), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("reference_type", sa.String(length=50), nullable=True),
            sa.Column("reference_id", sa.String(length=255), nullable=True),
            sa.Column("operation_key", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "amount <> 0",
                name="check_balance_ledger_amount_nonzero",
            ),
            sa.CheckConstraint(
                "CAST(amount AS TEXT) <> 'NaN'",
                name=LEDGER_AMOUNT_FINITE_CONSTRAINT,
            ),
            sa.CheckConstraint(
                "balance_after IS NULL OR CAST(balance_after AS TEXT) <> 'NaN'",
                name=LEDGER_BALANCE_AFTER_FINITE_CONSTRAINT,
            ),
            sa.UniqueConstraint(
                "operation_key",
                name="uq_balance_ledger_operation_key",
            ),
        )
        op.create_index(
            "idx_balance_ledger_user_created",
            BALANCE_LEDGER,
            ["user_id", "created_at"],
            unique=False,
        )
        op.create_index(
            "idx_balance_ledger_created",
            BALANCE_LEDGER,
            ["created_at"],
            unique=False,
        )
        op.create_index(
            "idx_balance_ledger_reason_created",
            BALANCE_LEDGER,
            ["reason", "created_at"],
            unique=False,
        )
        return

    required_columns = {
        "id",
        "user_id",
        "amount",
        "balance_after",
        "reason",
        "description",
        "reference_type",
        "reference_id",
        "operation_key",
        "created_at",
    }
    missing_columns = required_columns - set(_column_map(bind, BALANCE_LEDGER))
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise RuntimeError(
            "Existing table 'balance_ledger' has an unsupported partial schema; "
            f"missing: {missing}. Restore it or migrate it manually before upgrade."
        )
    if not _named_unique_identity(
        bind,
        BALANCE_LEDGER,
        "uq_balance_ledger_operation_key",
        ("operation_key",),
    ):
        if _named_object_exists(
            bind, BALANCE_LEDGER, "uq_balance_ledger_operation_key"
        ):
            raise RuntimeError(
                "Existing object 'uq_balance_ledger_operation_key' does not "
                "uniquely cover operation_key; the ledger idempotency rule "
                "cannot be guaranteed."
            )
        _require_no_duplicate_values(
            bind,
            BALANCE_LEDGER,
            "operation_key",
            "the ledger idempotency rule",
        )
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(BALANCE_LEDGER, recreate="always") as batch_op:
                batch_op.create_unique_constraint(
                    "uq_balance_ledger_operation_key", ["operation_key"]
                )
        else:
            op.create_unique_constraint(
                "uq_balance_ledger_operation_key",
                BALANCE_LEDGER,
                ["operation_key"],
            )
    for index_name, columns in (
        ("idx_balance_ledger_user_created", ["user_id", "created_at"]),
        ("idx_balance_ledger_created", ["created_at"]),
        ("idx_balance_ledger_reason_created", ["reason", "created_at"]),
    ):
        if index_name not in _index_names(bind, BALANCE_LEDGER):
            op.create_index(index_name, BALANCE_LEDGER, columns, unique=False)


def _ensure_balance_ledger_finite_constraints(bind) -> None:
    """Reject PostgreSQL NUMERIC NaN in the append-only ledger too.

    The general financial-column helper runs before this revision creates the
    ledger. Keep the two ledger checks here so fresh and already partially
    migrated databases receive the same guarantee.
    """
    if BALANCE_LEDGER not in _table_names(bind):
        return

    required = (
        (LEDGER_AMOUNT_FINITE_CONSTRAINT, "CAST(amount AS TEXT) <> 'NaN'"),
        (
            LEDGER_BALANCE_AFTER_FINITE_CONSTRAINT,
            "balance_after IS NULL OR CAST(balance_after AS TEXT) <> 'NaN'",
        ),
    )
    existing = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(BALANCE_LEDGER)
    }
    missing = [item for item in required if item[0] not in existing]
    if not missing:
        return

    invalid_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM balance_ledger
            WHERE CAST(amount AS TEXT) = 'NaN'
               OR CAST(balance_after AS TEXT) = 'NaN'
            """
        )
    ).scalar_one()
    if invalid_count:
        raise RuntimeError(
            "Cannot protect balance_ledger: "
            f"{int(invalid_count)} row(s) contain NaN. Correct them manually "
            "before running Alembic again."
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(BALANCE_LEDGER, recreate="always") as batch_op:
            for name, condition in missing:
                batch_op.create_check_constraint(name, condition)
        return

    for name, condition in missing:
        op.create_check_constraint(name, BALANCE_LEDGER, condition)


def _add_successful_payment_order_constraint(bind) -> None:
    if PAYMENTS not in _table_names(bind):
        return
    existing = next(
        (
            index
            for index in sa.inspect(bind).get_indexes(PAYMENTS)
            if index.get("name") == SUCCESSFUL_ORDER_PAYMENT_INDEX
        ),
        None,
    )
    if existing is not None:
        if not existing.get("unique") or tuple(existing.get("column_names") or ()) != ("order_id",):
            raise RuntimeError(
                f"Existing index '{SUCCESSFUL_ORDER_PAYMENT_INDEX}' does not "
                "uniquely cover order_id; the one-successful-payment-per-order "
                "rule cannot be guaranteed."
            )
        dialect_options = existing.get("dialect_options") or {}
        predicate = dialect_options.get("postgresql_where") or dialect_options.get("sqlite_where")
        normalized = "".join(str(predicate or "").lower().split())
        if not (
            "order_id" in normalized
            and "isnotnull" in normalized
            and "status" in normalized
            and "success" in normalized
        ):
            raise RuntimeError(
                f"Existing index '{SUCCESSFUL_ORDER_PAYMENT_INDEX}' is missing "
                "the required SUCCESS/order_id partial predicate. Replace it in "
                "a reviewed manual migration, then retry."
            )
        return
    _require_at_most_one_successful_payment_per_order(bind)

    predicate = sa.text("order_id IS NOT NULL AND status = 'SUCCESS'")
    kwargs: dict[str, object] = {"unique": True}
    if bind.dialect.name == "postgresql":
        kwargs["postgresql_where"] = predicate
    elif bind.dialect.name == "sqlite":
        kwargs["sqlite_where"] = predicate
    else:
        raise RuntimeError(
            "The successful-payment partial index is implemented only for "
            "PostgreSQL (production) and SQLite (local tests)."
        )
    op.create_index(
        SUCCESSFUL_ORDER_PAYMENT_INDEX,
        PAYMENTS,
        ["order_id"],
        **kwargs,
    )


def _ensure_pending_order_amount_constraint(bind) -> None:
    """Refuse new zero-sum pending orders without rewriting old history.

    A past completed/cancelled order can legitimately be retained at zero for
    audit purposes.  The active state is what opens a payment flow, so this
    constraint makes a free order impossible at the database boundary while
    preserving historical records during migration.
    """
    if ORDERS not in _table_names(bind):
        return
    existing = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(ORDERS)
    }
    if PENDING_ORDER_AMOUNT_CONSTRAINT in existing:
        return

    invalid_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE status = 'ОЖИДАЕТ ОПЛАТЫ' AND total_amount <= 0
            """
        )
    ).scalar_one()
    if invalid_count:
        raise RuntimeError(
            "Cannot prevent free pending orders: "
            f"{int(invalid_count)} pending order(s) have a zero or negative total. "
            "Cancel or correct those orders manually, then run 'alembic upgrade head' again."
        )

    condition = "total_amount > 0 OR status <> 'ОЖИДАЕТ ОПЛАТЫ'"
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(ORDERS, recreate="always") as batch_op:
            batch_op.create_check_constraint(PENDING_ORDER_AMOUNT_CONSTRAINT, condition)
    else:
        op.create_check_constraint(PENDING_ORDER_AMOUNT_CONSTRAINT, ORDERS, condition)


def _ensure_active_product_price_constraint(bind) -> None:
    """Do not let a new active catalog product bypass paid checkout."""
    products = "products"
    if products not in _table_names(bind):
        return
    existing = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(products)
    }
    if ACTIVE_PRODUCT_PRICE_CONSTRAINT in existing:
        return

    invalid_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = true AND price <= 0
            """
        )
    ).scalar_one()
    if invalid_count:
        raise RuntimeError(
            "Cannot prevent free active products: "
            f"{int(invalid_count)} active product(s) have a zero or negative price. "
            "Deactivate or correct them manually, then run 'alembic upgrade head' again."
        )

    condition = "price > 0 OR is_active = false"
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(products, recreate="always") as batch_op:
            batch_op.create_check_constraint(ACTIVE_PRODUCT_PRICE_CONSTRAINT, condition)
    else:
        op.create_check_constraint(ACTIVE_PRODUCT_PRICE_CONSTRAINT, products, condition)


def _ensure_finite_numeric_constraints(bind) -> None:
    """Prevent PostgreSQL ``NUMERIC NaN`` from entering financial columns."""
    if bind.dialect.name != "postgresql":
        return
    tables = _table_names(bind)
    inspector = sa.inspect(bind)
    for table_name, column_name, _precision, _scale in NUMERIC_COLUMNS:
        if table_name not in tables or column_name not in _column_map(bind, table_name):
            continue
        constraint_name = f"ck_{table_name}_{column_name}_finite"
        existing = {
            item.get("name")
            for item in inspector.get_check_constraints(table_name)
        }
        if constraint_name not in existing:
            # PostgreSQL NUMERIC has no Infinity, but it does accept NaN and
            # compares it surprisingly. Explicit text comparison is stable
            # across supported PostgreSQL versions.
            op.create_check_constraint(
                constraint_name,
                table_name,
                f"{column_name}::text <> 'NaN'",
            )


def _backfill_opening_balance_entries(bind) -> None:
    """Make the first ledger total reconcile with existing user balances."""
    if {
        "users",
        BALANCE_LEDGER,
    } - _table_names(bind):
        return
    # Every user that had a balance before this migration receives one
    # immutable opening record. Existing experimental ledger rows are kept;
    # in that unusual case an opening entry is added only if the user has no
    # history at all.
    op.execute(
        sa.text(
            """
            INSERT INTO balance_ledger (
                user_id, amount, balance_after, reason, description,
                reference_type, reference_id, operation_key
            )
            SELECT
                users.id,
                users.balance,
                users.balance,
                'opening_balance',
                'Стартовый остаток при внедрении журнала',
                'migration',
                :revision,
                'opening_balance:' || CAST(users.id AS TEXT)
            FROM users
            WHERE users.balance <> 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM balance_ledger existing
                  WHERE existing.user_id = users.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM balance_ledger existing_key
                  WHERE existing_key.operation_key =
                        'opening_balance:' || CAST(users.id AS TEXT)
              )
            """
        ).bindparams(revision=revision)
    )


def _ensure_balance_ledger_immutable(bind) -> None:
    """Reject UPDATE/DELETE of ledger rows at the database boundary."""
    if BALANCE_LEDGER not in _table_names(bind):
        return
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {LEDGER_IMMUTABLE_FUNCTION}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    RAISE EXCEPTION 'balance_ledger is immutable';
                END;
                $$
                """
            )
        )
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {LEDGER_UPDATE_TRIGGER} ON {BALANCE_LEDGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {LEDGER_DELETE_TRIGGER} ON {BALANCE_LEDGER}"))
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {LEDGER_UPDATE_TRIGGER}
                BEFORE UPDATE ON {BALANCE_LEDGER}
                FOR EACH ROW EXECUTE FUNCTION {LEDGER_IMMUTABLE_FUNCTION}()
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {LEDGER_DELETE_TRIGGER}
                BEFORE DELETE ON {BALANCE_LEDGER}
                FOR EACH ROW EXECUTE FUNCTION {LEDGER_IMMUTABLE_FUNCTION}()
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {LEDGER_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {LEDGER_DELETE_TRIGGER}"))
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {LEDGER_UPDATE_TRIGGER}
                BEFORE UPDATE ON {BALANCE_LEDGER}
                BEGIN
                    SELECT RAISE(ABORT, 'balance_ledger is immutable');
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {LEDGER_DELETE_TRIGGER}
                BEFORE DELETE ON {BALANCE_LEDGER}
                BEGIN
                    SELECT RAISE(ABORT, 'balance_ledger is immutable');
                END
                """
            )
        )


def upgrade() -> None:
    bind = op.get_bind()

    _convert_numeric_columns(bind)
    _ensure_finite_numeric_constraints(bind)
    _add_payment_expiry(bind)
    _add_transfer_idempotency_key(bind)
    _ensure_coupon_activations(bind)
    _ensure_balance_ledger(bind)
    _ensure_balance_ledger_finite_constraints(bind)
    _add_successful_payment_order_constraint(bind)
    _ensure_pending_order_amount_constraint(bind)
    _ensure_active_product_price_constraint(bind)
    _backfill_opening_balance_entries(bind)
    _ensure_balance_ledger_immutable(bind)


def downgrade() -> None:
    """This financial-safety migration is intentionally forward-only.

    Downgrading would either reintroduce binary floating-point money or remove
    audit records and idempotency protections.  Restore a tested backup or add
    a separately reviewed rollback migration if that is ever required.
    """
    raise NotImplementedError(
        "20260801_09 is forward-only to preserve exact balances, ledger rows "
        "and payment idempotency protections."
    )
