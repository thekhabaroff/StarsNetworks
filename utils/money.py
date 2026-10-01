"""Единые точные операции с суммами в рублях."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


KOPECK = Decimal("0.01")
ZERO = Decimal("0.00")


def to_money(value: object, *, minimum: Decimal | None = None, maximum: Decimal | None = None) -> Decimal:
    """Parse a finite ruble amount and round it to kopecks.

    ``float('nan')`` and ``float('inf')`` are deliberately rejected.  All
    callers that accept a monetary value from Telegram or an admin panel must
    go through this function before writing to the database.
    """
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (AttributeError, InvalidOperation, ValueError):
        raise ValueError("Введите корректную сумму") from None
    if not amount.is_finite():
        raise ValueError("Сумма должна быть конечным числом")
    amount = amount.quantize(KOPECK, rounding=ROUND_HALF_UP)
    if minimum is not None and amount < minimum:
        raise ValueError(f"Сумма должна быть не меньше {format_rubles(minimum)} ₽")
    if maximum is not None and amount > maximum:
        raise ValueError(f"Сумма не может быть больше {format_rubles(maximum)} ₽")
    return amount


def money(value: object) -> Decimal:
    """Convert a persisted/internal amount to a finite Decimal.

    Corrupted financial data must stop a transaction instead of being silently
    treated as zero.  Returning zero for ``NaN`` used to hide a damaged
    balance and could lead to an incorrect debit or credit.
    """
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Внутренняя сумма содержит некорректное значение") from exc
    if not result.is_finite():
        raise ValueError("Внутренняя сумма должна быть конечным числом")
    return result.quantize(KOPECK, rounding=ROUND_HALF_UP)


def format_rubles(value: float | int | Decimal) -> str:
    """Показать копейки только если дробная часть действительно есть."""
    try:
        amount = money(value)
    except (InvalidOperation, ValueError, TypeError):
        # Never render a damaged persisted amount as an apparently valid zero.
        return "—"
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount:.2f}"
