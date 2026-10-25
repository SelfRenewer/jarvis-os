"""Currency-safe money.

Money is stored as integer minor units (tetri for GEL) and computed with `Decimal`. Floats are
rejected at construction so binary rounding errors can never enter a total.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MINOR_UNITS: dict[str, int] = {"GEL": 2, "USD": 2, "EUR": 2}
_CENT = Decimal("0.01")


class MoneyError(ValueError):
    pass


def to_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, float):
        raise MoneyError("float values are not accepted for money; pass str or Decimal")
    if isinstance(value, bool):
        raise MoneyError("bool is not money")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise MoneyError("money must be finite")
    return result


@dataclass(frozen=True, slots=True, order=True)
class Money:
    minor: int
    currency: str = "GEL"

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise MoneyError("minor units must be int")
        if self.currency not in MINOR_UNITS:
            raise MoneyError(f"unsupported currency {self.currency}")

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str = "GEL") -> Money:
        dec = to_decimal(amount)
        exp = MINOR_UNITS.get(currency, 2)
        minor = (dec * (Decimal(10) ** exp)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return cls(int(minor), currency)

    @classmethod
    def zero(cls, currency: str = "GEL") -> Money:
        return cls(0, currency)

    @property
    def amount(self) -> Decimal:
        exp = MINOR_UNITS[self.currency]
        return (Decimal(self.minor) / (Decimal(10) ** exp)).quantize(_CENT)

    def _check(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise MoneyError("can only combine Money with Money")
        if other.currency != self.currency:
            raise MoneyError(f"currency mismatch {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def times(self, factor: Decimal | int | str) -> Money:
        """Multiply by a quantity, rounding half-up to the minor unit."""
        dec = to_decimal(factor)
        minor = (Decimal(self.minor) * dec).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return Money(int(minor), self.currency)

    def is_negative(self) -> bool:
        return self.minor < 0

    def format(self) -> str:
        return f"{self.amount} {self.currency}"

    def __str__(self) -> str:
        return self.format()
