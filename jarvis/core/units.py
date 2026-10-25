"""Deterministic quantity/unit normalization.

Base dimensions: mass → gram, volume → millilitre, count → piece. Unit prices are expressed per
kilogram, per litre or per item.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from jarvis.core.money import to_decimal


class Dimension(StrEnum):
    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


class UnitError(ValueError):
    pass


# alias -> (dimension, factor to base unit)
_UNITS: dict[str, tuple[Dimension, Decimal]] = {}


def _register(aliases: list[str], dimension: Dimension, factor: str) -> None:
    for alias in aliases:
        _UNITS[alias] = (dimension, Decimal(factor))


_register(["g", "gr", "gram", "grams", "г", "гр", "грамм", "грамма", "граммов"], Dimension.MASS, "1")
_register(["kg", "kilo", "kilogram", "kilograms", "кг", "килограмм", "килограмма", "кило"], Dimension.MASS, "1000")
_register(["mg"], Dimension.MASS, "0.001")
_register(["ml", "мл", "миллилитр", "миллилитров"], Dimension.VOLUME, "1")
_register(["cl"], Dimension.VOLUME, "10")
_register(["l", "lt", "liter", "litre", "liters", "л", "литр", "литра", "литров"], Dimension.VOLUME, "1000")
_register(
    ["pc", "pcs", "piece", "pieces", "item", "items", "x", "шт", "штук", "штуки", "штука", "уп", "pack"],
    Dimension.COUNT,
    "1",
)
_register(["dozen", "десяток", "дюжина"], Dimension.COUNT, "12")

BASE_UNIT = {Dimension.MASS: "g", Dimension.VOLUME: "ml", Dimension.COUNT: "pcs"}
PRICE_UNIT = {
    Dimension.MASS: ("kg", Decimal(1000)),
    Dimension.VOLUME: ("L", Decimal(1000)),
    Dimension.COUNT: ("item", Decimal(1)),
}


def normalize_unit_name(unit: str) -> str:
    return unit.strip().lower().rstrip(".")


def lookup_unit(unit: str) -> tuple[Dimension, Decimal]:
    key = normalize_unit_name(unit)
    if key not in _UNITS:
        raise UnitError(f"unknown unit {unit!r}")
    return _UNITS[key]


@dataclass(frozen=True, slots=True)
class Quantity:
    """A quantity in base units (g / ml / pcs)."""

    value: Decimal
    dimension: Dimension

    @classmethod
    def of(cls, amount: Decimal | int | str, unit: str) -> Quantity:
        dimension, factor = lookup_unit(unit)
        value = to_decimal(amount) * factor
        if value < 0:
            raise UnitError("quantity must be non-negative")
        return cls(value.normalize() if value != 0 else Decimal(0), dimension)

    def to(self, unit: str) -> Decimal:
        dimension, factor = lookup_unit(unit)
        if dimension != self.dimension:
            raise UnitError(f"cannot convert {self.dimension} to {dimension}")
        return self.value / factor

    def __add__(self, other: Quantity) -> Quantity:
        if other.dimension != self.dimension:
            raise UnitError("dimension mismatch")
        return Quantity(self.value + other.value, self.dimension)

    def describe(self) -> str:
        if self.dimension == Dimension.MASS and self.value >= 1000:
            return f"{(self.value / 1000).normalize():f} кг"
        if self.dimension == Dimension.VOLUME and self.value >= 1000:
            return f"{(self.value / 1000).normalize():f} л"
        labels = {Dimension.MASS: "г", Dimension.VOLUME: "мл", Dimension.COUNT: "шт"}
        return f"{self.value.normalize():f} {labels[self.dimension]}"


def unit_price(price: Decimal, package: Quantity) -> tuple[Decimal, str]:
    """Price per kg / L / item, rounded to 4 decimals (display precision, not a money total)."""
    if package.value <= 0:
        raise UnitError("package quantity must be positive")
    label, per = PRICE_UNIT[package.dimension]
    value = (to_decimal(price) * per / package.value).quantize(Decimal("0.0001"))
    return value, label
