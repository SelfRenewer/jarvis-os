from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from jarvis.core.money import Money
from jarvis.core.units import Quantity
from jarvis.shopping.optimizer import (
    Offer,
    Preference,
    Requirement,
    StoreFees,
    match_offer,
    optimize,
    packs_needed,
)
from jarvis.shopping.parsing import parse_list, product_key


def test_parse_russian_command() -> None:
    items = parse_list("Добавь яйца, молоко и 2 кг куриного филе.")
    assert [(i.key, i.quantity, i.unit) for i in items] == [
        ("eggs", None, None),
        ("milk", None, None),
        ("chicken fillet", Decimal(2), "кг"),
    ]
    assert items[2].base_quantity() == Quantity.of("2000", "g")


def test_parse_scenario4_list() -> None:
    items = parse_list("Milk 2 × 1L\nChicken fillet 2kg\nEggs 20")
    assert [(i.key, i.base_quantity()) for i in items] == [
        ("milk", Quantity.of("2", "l")),
        ("chicken fillet", Quantity.of("2", "kg")),
        ("eggs", Quantity.of("20", "pcs")),
    ]


def test_product_key_synonyms() -> None:
    assert product_key("Coca-Cola Zero 0.5") == "coca-cola"
    assert product_key("Молоко Sante 3.2%") == "milk"
    assert product_key("Куриное филе охлаждённое") == "chicken fillet"


def _milk_req(liters: str = "2") -> Requirement:
    return Requirement("m", "Milk", "milk", Quantity.of(liters, "l"))


def test_packs_distinguish_package_sizes() -> None:
    assert packs_needed(Quantity.of("2", "l"), Quantity.of("1", "l")) == 2
    assert packs_needed(Quantity.of("2", "l"), Quantity.of("900", "ml")) == 3
    assert packs_needed(Quantity.of("2", "l"), Quantity.of("500", "ml")) == 4
    assert packs_needed(None, Quantity.of("500", "ml")) == 1


def test_match_rejects_excluded_brand_and_missing_attribute() -> None:
    req = Requirement(
        "c",
        "Cola",
        "coca-cola",
        Quantity.of("1", "l"),
        Preference(required_attributes=("zero",), excluded_brands=("Pepsi",)),
    )
    zero = Offer("1", "s", "Coca-Cola Zero 1L", "coca-cola", "Coca-Cola", Quantity.of("1", "l"), Money.of("3.20"))
    classic = Offer("2", "s", "Coca-Cola Classic 1L", "coca-cola", "Coca-Cola", Quantity.of("1", "l"), Money.of("3.00"))
    assert match_offer(req, zero).confidence >= 0.9
    assert match_offer(req, classic).rejected == "missing_required:zero"


def test_llm_match_requires_approval() -> None:
    req = _milk_req()
    offer = Offer(
        "1", "s", "Sante 3.2%", "milk", "Sante", Quantity.of("1", "l"), Money.of("4"), attributes=("llm_match",)
    )
    result = optimize([req], [offer], [StoreFees("s", "S", Money.of("0"), Money.of("0"))])
    assert result.best is not None and result.best.lines[0].needs_approval


def _scenario4(fee_unknown: bool) -> tuple[list[Requirement], list[Offer], list[StoreFees]]:
    reqs = [
        Requirement("milk", "Milk 2×1L", "milk", Quantity.of("2", "l")),
        Requirement("chicken", "Chicken fillet 2kg", "chicken fillet", Quantity.of("2", "kg")),
        Requirement("eggs", "Eggs 20", "eggs", Quantity.of("20", "pcs")),
    ]
    offers = [
        # store A
        Offer("a1", "A", "Milk 1L", "milk", "Sante", Quantity.of("1", "l"), Money.of("4.50")),
        Offer("a2", "A", "Chicken fillet 1kg", "chicken fillet", "Agro", Quantity.of("1", "kg"), Money.of("14.00")),
        Offer("a3", "A", "Eggs 10", "eggs", None, Quantity.of("10", "pcs"), Money.of("5.50")),
        # store B: cheaper milk per package but 900 ml, cheaper chicken
        Offer("b1", "B", "Milk 900ml", "milk", "Parmalat", Quantity.of("900", "ml"), Money.of("4.20")),
        Offer("b2", "B", "Chicken fillet 500g", "chicken fillet", "Agro", Quantity.of("500", "g"), Money.of("6.40")),
        Offer("b3", "B", "Eggs 30", "eggs", None, Quantity.of("30", "pcs"), Money.of("15.00")),
    ]
    stores = [
        StoreFees("A", "Store A", Money.of("3.99"), Money.of("1.00")),
        StoreFees("B", "Store B", None if fee_unknown else Money.of("2.49"), Money.of("0.50")),
    ]
    return reqs, offers, stores


def test_scenario4_complete_fees_allows_cheapest_claim() -> None:
    reqs, offers, stores = _scenario4(fee_unknown=False)
    result = optimize(reqs, offers, stores)
    best = result.best
    assert best is not None
    # A alone: 2*4.50 + 2*14.00 + 2*5.50 = 48.00 + fees 4.99 = 52.99
    # B alone: 3*4.20 + 4*6.40 + 1*15.00 = 53.20 + fees 2.99 = 56.19
    # A+B:     A(milk 9.00, eggs 11.00) + B(chicken 25.60) = 45.60 + 4.99 + 2.99 = 53.58 (+3.00 penalty)
    totals = {b.stores: b.total for b in result.baskets}
    assert totals[("A",)] == Money.of("52.99")
    assert totals[("B",)] == Money.of("56.19")
    assert totals[("A", "B")] == Money.of("53.58")
    assert best.stores == ("A",)
    assert result.cheapest_claim_allowed
    assert best.is_complete


def test_scenario4_unknown_fee_blocks_cheapest_claim() -> None:
    reqs, offers, stores = _scenario4(fee_unknown=True)
    result = optimize(reqs, offers, stores)
    assert not result.cheapest_claim_allowed
    assert "неизвестные сборы" in result.note.lower() or "ИЗВЕСТНАЯ" in result.note
    b_only = next(b for b in result.baskets if b.stores == ("B",))
    assert not b_only.is_complete and b_only.unknown_fees == ["Store B: delivery fee"]


def test_small_order_fee_applied() -> None:
    req = [Requirement("m", "Milk", "milk", Quantity.of("1", "l"))]
    offers = [Offer("a", "A", "Milk 1L", "milk", None, Quantity.of("1", "l"), Money.of("4.50"))]
    stores = [StoreFees("A", "A", Money.of("2"), Money.of("0"), Money.of("20"), Money.of("3"))]
    result = optimize(req, offers, stores)
    assert result.best is not None and result.best.total == Money.of("9.50")


@settings(max_examples=100, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 5000), st.integers(0, 5000)), min_size=1, max_size=6),
    st.integers(0, 10000),
    st.integers(0, 10000),
)
def test_property_totals_never_negative(prices: list[tuple[int, int]], discount: int, fee: int) -> None:
    reqs = [Requirement(f"r{i}", "x", "milk", None) for i in range(len(prices))]
    offers = [
        Offer(f"o{i}", "A", "Milk", "milk", None, Quantity.of("1", "l"), Money(p), Money(q))
        for i, (p, q) in enumerate(prices)
    ]
    stores = [StoreFees("A", "A", Money(fee), Money(0), basket_discount=Money(discount))]
    result = optimize(reqs, offers, stores)
    for basket in result.baskets:
        assert not basket.total.is_negative()
        assert basket.total.minor >= basket.fees_total.minor
