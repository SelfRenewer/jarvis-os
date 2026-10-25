"""Deterministic product matching and basket optimization (Decimal / minor units only).

total(basket) = Σ items + delivery fees + service fees + small-order fees − basket discounts (≥ 0)
A basket is *complete* only if every item is matched and every applicable fee is known. The service
may call the best basket "the cheapest" only if every compared full basket is complete.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from jarvis.core.money import Money
from jarvis.core.units import Quantity, unit_price


@dataclass(frozen=True)
class Preference:
    allowed_brands: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()
    required_attributes: tuple[str, ...] = ()  # e.g. ("zero",), ("fillet",), ("chilled",)
    preferred_attributes: tuple[str, ...] = ()
    min_package: Quantity | None = None


@dataclass(frozen=True)
class Requirement:
    item_id: str
    name: str
    key: str
    quantity: Quantity | None
    preference: Preference = Preference()
    substitution_allowed: bool = True


@dataclass(frozen=True)
class Offer:
    offer_id: str
    store_id: str
    product_name: str
    key: str
    brand: str | None
    package: Quantity
    price: Money
    promo_price: Money | None = None
    available: bool = True
    attributes: tuple[str, ...] = ()

    @property
    def effective_price(self) -> Money:
        if self.promo_price is not None and self.promo_price.minor <= self.price.minor:
            return self.promo_price
        return self.price


@dataclass(frozen=True)
class StoreFees:
    store_id: str
    name: str
    delivery_fee: Money | None  # None = unknown
    service_fee: Money | None
    small_order_threshold: Money | None = None
    small_order_fee: Money | None = None
    basket_discount: Money | None = None


@dataclass(frozen=True)
class Match:
    confidence: float
    reasons: tuple[str, ...]
    rejected: str | None = None


def match_offer(req: Requirement, offer: Offer) -> Match:
    if not offer.available:
        return Match(0.0, (), "unavailable")
    brand = (offer.brand or "").strip().lower()
    pref = req.preference
    if brand and brand in {b.lower() for b in pref.excluded_brands}:
        return Match(0.0, (), "excluded_brand")
    if req.quantity is not None and req.quantity.dimension != offer.package.dimension:
        return Match(0.0, (), "unit_dimension_mismatch")
    if (
        pref.min_package is not None
        and offer.package.dimension == pref.min_package.dimension
        and offer.package.value < pref.min_package.value
    ):
        return Match(0.0, (), "package_too_small")
    attrs = {a.lower() for a in offer.attributes} | set(offer.product_name.lower().split())
    reasons: list[str] = []
    if offer.key == req.key:
        # Deterministic keyword match → 0.9; an LLM-suggested match is capped at 0.7 → always needs approval.
        confidence = 0.7 if "llm_match" in offer.attributes else 0.9
        reasons.append("llm_match" if "llm_match" in offer.attributes else "same_product")
    else:
        return Match(0.0, (), "different_product")
    missing = [a for a in pref.required_attributes if a.lower() not in attrs]
    if missing:
        return Match(0.0, (), "missing_required:" + ",".join(missing))
    if pref.allowed_brands:
        if brand in {b.lower() for b in pref.allowed_brands}:
            confidence += 0.05
            reasons.append("allowed_brand")
        else:
            confidence -= 0.25
            reasons.append("brand_not_in_allowed")
    if pref.preferred_attributes and any(a.lower() in attrs for a in pref.preferred_attributes):
        confidence += 0.03
        reasons.append("preferred_attribute")
    return Match(round(min(confidence, 0.99), 3), tuple(reasons))


@dataclass(frozen=True)
class Line:
    item_id: str
    offer: Offer
    packs: int
    cost: Money
    confidence: float
    needs_approval: bool
    unit_price: Decimal
    unit_label: str


def packs_needed(required: Quantity | None, package: Quantity) -> int:
    if required is None:
        return 1
    ratio = (required.value / package.value).quantize(Decimal(1), rounding=ROUND_CEILING)
    return max(1, int(ratio))


def best_line(req: Requirement, offers: list[Offer], min_confidence: float) -> Line | None:
    best: Line | None = None
    for offer in offers:
        m = match_offer(req, offer)
        if m.rejected or m.confidence <= 0:
            continue
        packs = packs_needed(req.quantity, offer.package)
        cost = offer.effective_price.times(packs)
        per, label = unit_price(offer.effective_price.amount, offer.package)
        needs_approval = m.confidence < min_confidence
        if needs_approval and not req.substitution_allowed:
            continue
        line = Line(req.item_id, offer, packs, cost, m.confidence, needs_approval, per, label)
        if best is None or (line.cost, -line.confidence) < (best.cost, -best.confidence):
            best = line
    return best


@dataclass
class Basket:
    stores: tuple[str, ...]
    lines: list[Line]
    missing: list[str]
    items_total: Money
    fees_total: Money
    discounts_total: Money
    total: Money
    unknown_fees: list[str]
    score: Money
    substitutions: int

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.unknown_fees


@dataclass
class OptimizationResult:
    baskets: list[Basket] = field(default_factory=list)
    cheapest_claim_allowed: bool = False
    note: str = ""

    @property
    def best(self) -> Basket | None:
        return self.baskets[0] if self.baskets else None


def _fees_for(store: StoreFees, subtotal: Money) -> tuple[Money, Money, list[str]]:
    fees = Money.zero(subtotal.currency)
    unknown: list[str] = []
    if store.delivery_fee is None:
        unknown.append(f"{store.name}: delivery fee")
    else:
        fees = fees + store.delivery_fee
    if store.service_fee is None:
        unknown.append(f"{store.name}: service fee")
    else:
        fees = fees + store.service_fee
    if store.small_order_threshold is not None and subtotal < store.small_order_threshold:
        if store.small_order_fee is None:
            unknown.append(f"{store.name}: small-order fee")
        else:
            fees = fees + store.small_order_fee
    discount = Money.zero(subtotal.currency)
    if store.basket_discount is not None:
        discount = min(store.basket_discount, subtotal)  # never below zero for items
    return fees, discount, unknown


def optimize(
    requirements: list[Requirement],
    offers: list[Offer],
    stores: list[StoreFees],
    *,
    min_confidence: float = 0.8,
    extra_delivery_penalty: Money = Money.of("3.00"),
    substitution_penalty: Money = Money.of("1.50"),
    max_stores: int = 2,
) -> OptimizationResult:
    currency = "GEL"
    store_by_id = {s.store_id: s for s in stores}
    per_store_lines: dict[str, dict[str, Line]] = {}
    for store in stores:
        store_offers = [o for o in offers if o.store_id == store.store_id]
        lines: dict[str, Line] = {}
        for req in requirements:
            line = best_line(req, store_offers, min_confidence)
            if line is not None:
                lines[req.item_id] = line
        per_store_lines[store.store_id] = lines

    baskets: list[Basket] = []
    seen: set[tuple[str, ...]] = set()
    for size in range(1, max(1, min(max_stores, len(stores))) + 1):
        for combo in itertools.combinations(sorted(store_by_id), size):
            chosen: dict[str, Line] = {}
            for req in requirements:
                candidates = [per_store_lines[s][req.item_id] for s in combo if req.item_id in per_store_lines[s]]
                if not candidates:
                    continue
                chosen[req.item_id] = min(
                    candidates,
                    key=lambda ln: (
                        ln.cost.minor + (substitution_penalty.minor if ln.needs_approval else 0),
                        -ln.confidence,
                    ),
                )
            used = tuple(sorted({ln.offer.store_id for ln in chosen.values()}))
            if not used or used in seen:
                continue
            seen.add(used)
            items_total = Money.zero(currency)
            fees_total = Money.zero(currency)
            discounts = Money.zero(currency)
            unknown: list[str] = []
            for store_id in used:
                subtotal = Money.zero(currency)
                for ln in chosen.values():
                    if ln.offer.store_id == store_id:
                        subtotal = subtotal + ln.cost
                fees, discount, missing_fees = _fees_for(store_by_id[store_id], subtotal)
                items_total = items_total + subtotal
                fees_total = fees_total + fees
                discounts = discounts + discount
                unknown.extend(missing_fees)
            total = items_total + fees_total - discounts
            substitutions = sum(1 for ln in chosen.values() if ln.needs_approval)
            score = total + extra_delivery_penalty.times(len(used) - 1) + substitution_penalty.times(substitutions)
            missing = [r.name for r in requirements if r.item_id not in chosen]
            baskets.append(
                Basket(
                    used,
                    list(chosen.values()),
                    missing,
                    items_total,
                    fees_total,
                    discounts,
                    total,
                    unknown,
                    score,
                    substitutions,
                )
            )
    baskets.sort(key=lambda b: (len(b.missing), b.score.minor, len(b.stores)))
    full = [b for b in baskets if not b.missing]
    allowed = bool(full) and all(b.is_complete for b in full)
    note = ""
    if not full:
        note = "Ни один вариант не покрывает весь список."
    elif not allowed:
        note = "Есть неизвестные сборы — это минимальная ИЗВЕСТНАЯ сумма, а не гарантированно самая дешёвая корзина."
    return OptimizationResult(baskets, allowed, note)
