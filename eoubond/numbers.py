"""Decimal helpers. Money is kept to 2 places, quantities to 3 (matches the schema)."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_Q = Decimal("0.01")
QTY_Q = Decimal("0.001")
RUPEE_Q = Decimal("1")
ZERO = Decimal("0")


def D(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {value!r}") from exc


def money(value) -> Decimal:
    return D(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def qty(value) -> Decimal:
    return D(value).quantize(QTY_Q, rounding=ROUND_HALF_UP)


def rupee(value) -> Decimal:
    return D(value).quantize(RUPEE_Q, rounding=ROUND_HALF_UP).quantize(MONEY_Q)
