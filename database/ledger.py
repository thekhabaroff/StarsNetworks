"""Неизменяемый журнал операций с внутренним балансом.

Модель намеренно находится отдельно от ``database.models``.  Это позволяет
подключать журнал поэтапно: сначала применить миграцию, затем добавлять записи
в обработчиках платежей, покупок и переводов без смешивания этих изменений с
существующими моделями.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from database.db import Base


class BalanceLedger(Base):
    """Одна неизменяемая запись о зачислении или списании средств.

    ``amount`` положителен для зачисления и отрицателен для списания.
    ``operation_key`` используется вызывающим кодом как идемпотентный ключ:
    повторная доставка одного и того же webhook-а не должна создать вторую
    операцию.
    """

    __tablename__ = "balance_ledger"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    balance_after = Column(Numeric(18, 2), nullable=True)
    reason = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    reference_type = Column(String(50), nullable=True)
    reference_id = Column(String(255), nullable=True)
    operation_key = Column(String(255), nullable=True)
    created_at = Column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("amount <> 0", name="check_balance_ledger_amount_nonzero"),
        # PostgreSQL NUMERIC accepts NaN and considers it non-zero. CAST is
        # valid in both PostgreSQL and SQLite, so metadata stays portable
        # while the database rejects damaged financial entries.
        CheckConstraint(
            "CAST(amount AS TEXT) <> 'NaN'",
            name="check_balance_ledger_amount_finite",
        ),
        CheckConstraint(
            "balance_after IS NULL OR CAST(balance_after AS TEXT) <> 'NaN'",
            name="check_balance_ledger_balance_after_finite",
        ),
        UniqueConstraint("operation_key", name="uq_balance_ledger_operation_key"),
        Index("idx_balance_ledger_user_created", "user_id", "created_at"),
        Index("idx_balance_ledger_created", "created_at"),
        Index("idx_balance_ledger_reason_created", "reason", "created_at"),
    )
