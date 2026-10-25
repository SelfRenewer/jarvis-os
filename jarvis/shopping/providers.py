"""Shopping provider abstraction.

Current status (checked 2026-09-23 against the providers' public terms):
* Wolt and Bolt Food expose merchant-side APIs only; their consumer terms forbid bots/automated use.
  → `AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE`. No scraping, private API or browser automation exists here.
* Manual import (JSON/CSV offers collected by the user) and user-assisted flows are implemented.
A permitted official API can be added later as another `ShoppingProvider`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from jarvis.core.errors import CapabilityUnavailableError

AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE = "AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE"


class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MANUAL = "MANUAL"
    UNAVAILABLE = AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE


@dataclass(frozen=True)
class CheckoutPreparation:
    provider: str
    automated: bool
    steps: list[str]
    links: list[str] = field(default_factory=list)


class ShoppingProvider(Protocol):
    name: str

    def capabilities(self) -> dict[str, CapabilityStatus]: ...

    async def search_products(self, query: str) -> list[dict[str, Any]]: ...

    async def quote_basket(self, lines: list[dict[str, Any]]) -> dict[str, Any]: ...

    async def prepare_checkout(self, basket: dict[str, Any]) -> CheckoutPreparation: ...

    async def place_order(self, prepared: CheckoutPreparation, approval_id: str) -> dict[str, Any]: ...


class UnavailableAutomationProvider:
    """Wolt / Bolt: no permitted consumer automation. Every automated capability is unavailable."""

    def __init__(self, name: str, public_url: str) -> None:
        self.name = name
        self.public_url = public_url

    def capabilities(self) -> dict[str, CapabilityStatus]:
        return {c: CapabilityStatus.UNAVAILABLE for c in ("search", "quote", "checkout", "place_order")}

    def _unavailable(self, what: str) -> CapabilityUnavailableError:
        return CapabilityUnavailableError(
            AUTOMATED_PROVIDER_ACCESS_UNAVAILABLE,
            f"{self.name}: automated {what} is not offered through a permitted API; use manual import",
        )

    async def search_products(self, query: str) -> list[dict[str, Any]]:
        raise self._unavailable("product search")

    async def quote_basket(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        raise self._unavailable("quoting")

    async def prepare_checkout(self, basket: dict[str, Any]) -> CheckoutPreparation:
        items = basket.get("lines", [])
        steps = [
            f"Открой приложение {self.name.capitalize()} вручную и выбери магазин «{basket.get('store_name', '')}»."
        ]
        steps += [f"Добавь: {ln['product_name']} × {ln['packs']}" for ln in items]
        steps.append("Проверь итоговую сумму и сборы в приложении перед оплатой. Оплата — только в приложении.")
        return CheckoutPreparation(self.name, automated=False, steps=steps, links=[self.public_url])

    async def place_order(self, prepared: CheckoutPreparation, approval_id: str) -> dict[str, Any]:
        raise self._unavailable("ordering")


class ManualImportProvider:
    """Offers imported by the user (JSON). Checkout is always manual."""

    name = "manual"

    def capabilities(self) -> dict[str, CapabilityStatus]:
        return {
            "search": CapabilityStatus.MANUAL,
            "quote": CapabilityStatus.AVAILABLE,
            "checkout": CapabilityStatus.MANUAL,
            "place_order": CapabilityStatus.UNAVAILABLE,
        }

    async def search_products(self, query: str) -> list[dict[str, Any]]:
        return []  # offers are served from the database after import

    async def quote_basket(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        return {"computed_by": "jarvis_optimizer"}

    async def prepare_checkout(self, basket: dict[str, Any]) -> CheckoutPreparation:
        steps = [f"Магазин: {basket.get('store_name', '')}"]
        steps += [f"• {ln['product_name']} × {ln['packs']} = {ln['cost']}" for ln in basket.get("lines", [])]
        steps.append("Оформи заказ вручную у выбранного магазина/сервиса.")
        return CheckoutPreparation(self.name, automated=False, steps=steps)

    async def place_order(self, prepared: CheckoutPreparation, approval_id: str) -> dict[str, Any]:
        raise CapabilityUnavailableError("MANUAL_CHECKOUT_ONLY", "manual import provider cannot place orders")


# --- import format --------------------------------------------------------------------------------


class ImportStore(BaseModel):
    store_key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(default="manual", max_length=40)
    delivery_fee: Decimal | None = None
    service_fee: Decimal | None = None
    small_order_threshold: Decimal | None = None
    small_order_fee: Decimal | None = None

    @field_validator("delivery_fee", "service_fee", "small_order_threshold", "small_order_fee")
    @classmethod
    def _non_negative(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise ValueError("fees must be non-negative")
        return v


class ImportOffer(BaseModel):
    store_key: str
    product_name: str = Field(min_length=1, max_length=300)
    brand: str | None = Field(default=None, max_length=100)
    package_quantity: Decimal = Field(gt=0)
    package_unit: str = Field(min_length=1, max_length=20)
    price: Decimal = Field(ge=0)
    promo_price: Decimal | None = Field(default=None, ge=0)
    available: bool = True
    attributes: list[str] = Field(default_factory=list)


class ImportDocument(BaseModel):
    stores: list[ImportStore]
    offers: list[ImportOffer]
