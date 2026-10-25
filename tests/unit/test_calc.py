"""Calculator and currency conversion: parsed and computed by code (no LLM)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from jarvis.bot.intents import Intent, keyword_route
from jarvis.calc.currency import ConversionRequest, FxService, money_text, parse_conversion
from jarvis.calc.expression import CalcError, calculate, parse_amount
from jarvis.integrations.fx.client import FakeRates, HttpRates, parse_crypto, parse_fiat
from tests.conftest import make_settings

NB = " "


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        # R26 (2026-10-13): the LLM router took 33 s on this message; now it never reaches the LLM.
        ("10 000 000 ÷ 1500", f"🧮 10 000 000 ÷ 1500 ≈ 6{NB}666,67"),
        ("сколько будет 2+2", "🧮 2+2 = 4"),
        ("Сколько будет 1500 * 12?", f"🧮 1500 * 12 = 18{NB}000"),
        ("2 * (3 + 4)", "🧮 2 * (3 + 4) = 14"),
        ("15% от 2000", "🧮 15% от 2000 = 300"),
        ("2000 + 15%", f"🧮 2000 + 15% = 2{NB}300"),
        ("2 в степени 10", f"🧮 2 в степени 10 = 1{NB}024"),
        ("корень из 16", "🧮 корень из 16 = 4"),
        ("корень из 2", "🧮 корень из 2 ≈ 1,4142"),
        ("1/3", "🧮 1/3 ≈ 0,333333"),
        ("0.1+0.2", "🧮 0.1+0.2 = 0,3"),  # exact fractions: no 0.30000000000000004
        ("10 разделить на 4", "🧮 10 разделить на 4 = 2,5"),
        ("100 минус 7 плюс 3", "🧮 100 минус 7 плюс 3 = 96"),
        ("1,5 млн / 12", f"🧮 1,5 млн / 12 = 125{NB}000"),
        ("10к * 3", f"🧮 10к * 3 = 30{NB}000"),
        ("2,5 x 4", "🧮 2,5 x 4 = 10"),
        ("10 : 4", "🧮 10 : 4 = 2,5"),
        ("7.5 - 10", "🧮 7.5 - 10 = -2,5"),
        ("2^1000", "🧮 2^1000 ≈ 1,0715 × 10^301"),
    ],
)
def test_calculate(text: str, answer: str) -> None:
    result = calculate(text)
    assert result is not None
    assert result.text() == answer


@pytest.mark.parametrize(
    "text",
    [
        "25.09",
        "12.10.2026",
        "2026-10-13",
        "10:30",
        "в 10:30",
        "24",
        "-5",
        "приоритет 1",
        "перенеси на 2 часа позже",
        "добавь 2 кг филе",
        "+1 555 555 0111",
        "+7 (999) 123-45-67",
        "с 25.09 по 30.09",
        "5 к 3",
    ],
)
def test_not_a_calculation(text: str) -> None:
    assert calculate(text) is None


@pytest.mark.parametrize(("text", "error"), [("1/0", "деление на ноль"), ("2^999999999", "слишком большое число")])
def test_calc_errors(text: str, error: str) -> None:
    with pytest.raises(CalcError, match=error):
        calculate(text)


def test_parse_amount() -> None:
    assert parse_amount("10 000") == 10000
    assert parse_amount("1,5 млн") == 1_500_000
    assert parse_amount("10к") == 10000
    assert parse_amount("полторы тысячи") == 1500
    assert parse_amount("10 тыс.") == 10000
    assert parse_amount("abc") is None


@pytest.mark.parametrize(
    ("text", "amount", "source", "targets"),
    [
        ("сколько 10 000 руб. в долларах", 10000, "RUB", ("USD",)),
        ("10000 рублей в доллары", 10000, "RUB", ("USD",)),
        ("100 долларов в лари", 100, "USD", ("GEL",)),
        ("курс доллара", None, "USD", ()),
        ("курс доллара к рублю", None, "USD", ("RUB",)),
        ("сколько стоит биткоин", None, "BTC", ()),
        ("$100 в рублях", 100, "USD", ("RUB",)),
        ("€50 в рублях", 50, "EUR", ("RUB",)),
        ("сколько в долларах 10 000 рублей", 10000, "RUB", ("USD",)),
        ("10к рублей в евро", 10000, "RUB", ("EUR",)),
        ("1,5 млн драм в рублях", 1_500_000, "AMD", ("RUB",)),
        ("100 USD to GEL", 100, "USD", ("GEL",)),
        ("100 THB в RUB", 100, "THB", ("RUB",)),
        ("полторы тысячи долларов в лари", 1500, "USD", ("GEL",)),
        ("1000 QRL в рублях", 1000, "QRL", ("RUB",)),
        ("10 000 рублей это сколько долларов", 10000, "RUB", ("USD",)),
        ("сколько 500 лари", 500, "GEL", ()),
        ("курс валют", None, "USD", ()),
    ],
)
def test_parse_conversion(text: str, amount: int | None, source: str, targets: tuple[str, ...]) -> None:
    request = parse_conversion(text)
    assert request == ConversionRequest(None if amount is None else Decimal(amount), source, targets)


@pytest.mark.parametrize(
    "text",
    [
        "потратил 45 лари на такси",  # a future finance agent's phrase, not a conversion
        "купить хлеб за 100 рублей",
        "напомни заплатить 500 лари за квартиру",
        "стрижка 30 лари в пятницу",
        "100 долларов и 50 евро в рублях",
        "эфир в 19:00",
        "курс",
        "что у меня завтра",
    ],
)
def test_not_a_conversion(text: str) -> None:
    assert parse_conversion(text) is None


def test_router_sends_them_to_code() -> None:
    assert keyword_route("10 000 000 ÷ 1500").intent == Intent.CALCULATE
    assert keyword_route("Джарвис, 10 000 руб. в долларах").intent == Intent.CONVERT_CURRENCY
    assert keyword_route("потратил 45 лари на такси").intent != Intent.CONVERT_CURRENCY
    assert keyword_route("перенеси стрижку на 2 часа позже").intent == Intent.SCHEDULE_REFERENCED_TASK


def test_money_text() -> None:
    assert money_text(Decimal("118.986"), "USD") == "118,99"
    assert money_text(Decimal("43097.2549"), "AMD") == f"43{NB}097,25"
    assert money_text(Decimal("0.895156"), "EUR") == "0,8952"
    assert money_text(Decimal("1.2344"), "KWD") == "1,234"  # L10: dinars have three decimals
    assert money_text(Decimal("1500.4"), "JPY") == f"1{NB}500"  # and yen none
    assert money_text(Decimal("0.00120245678"), "BTC") == "0,00120246"


def test_parse_fiat_and_crypto_use_decimal() -> None:
    fiat = parse_fiat(
        {
            "result": "success",
            "time_last_update_unix": Decimal(1791849451),
            "rates": {"USD": Decimal(1), "RUB": Decimal("84.04")},
        }
    )
    assert fiat.per_usd["RUB"] == Decimal("84.04")
    quotes = parse_crypto(
        {"bitcoin": {"usd": Decimal("83160"), "last_updated_at": Decimal(1791912634)}}, {"BTC": "bitcoin"}
    )
    assert quotes["BTC"].usd == Decimal("83160")


async def test_convert_and_cache() -> None:
    api = FakeRates()
    fx = FxService(make_settings(), api)
    text = await fx.convert(ConversionRequest(Decimal(10000), "RUB", ("USD",)))
    assert text.splitlines()[:2] == [f"💱 10{NB}000 RUB ≈ 118,99 USD", "Курс: 1 USD = 84,04 RUB"]
    assert text.splitlines()[2].startswith("Курсы: open.er-api.com, 23.09 03:57")
    await fx.convert(ConversionRequest(Decimal(5), "USD", ("GEL",)))
    assert api.calls == ["fiat"]  # cached: one request for both answers


async def test_default_targets_and_crypto() -> None:
    fx = FxService(make_settings(), FakeRates())
    lines = (await fx.convert(ConversionRequest(None, "USD", ()))).splitlines()
    assert lines[0] == "💱 1 USD ≈"
    assert lines[1:7] == ["• 84,04 RUB", "• 362,20 AMD", "• 0,8952 EUR", "• 2,60 GEL", "• 49,36 TRY", "• 0,900901 QRL"]
    assert "QRL — CoinGecko" in lines[-1]


async def test_outages() -> None:
    api = FakeRates(crypto_unavailable=True)
    fx = FxService(make_settings(), api)
    lines = (await fx.convert(ConversionRequest(None, "USD", ()))).splitlines()
    assert not any("QRL" in line for line in lines)  # a default coin without a quote is left out
    assert "• QRL — курс недоступен" in await fx.convert(ConversionRequest(Decimal(1), "USD", ("QRL",)))
    fx._fiat = (float("-inf"), fx._fiat[1]) if fx._fiat else None  # expire it (monotonic() is small on a new VM)
    api.unavailable = True
    stale = await fx.convert(ConversionRequest(Decimal(1), "USD", ("RUB",)))
    assert stale.splitlines()[-1].startswith("⚠️ Нет связи — последние известные курсы")
    cold = FxService(make_settings(), FakeRates(unavailable=True))
    with pytest.raises(Exception, match=r"open\.er-api\.com"):
        await cold.convert(ConversionRequest(Decimal(1), "USD", ("RUB",)))


@respx.mock
async def test_http_rates_adapter() -> None:
    respx.get("https://open.er-api.com/v6/latest/USD").mock(
        return_value=httpx.Response(
            200,
            text='{"result":"success","base_code":"USD","time_last_update_unix":1791849451,'
            '"rates":{"USD":1,"RUB":84.043643,"GEL":2.596409}}',
        )
    )
    coingecko = respx.get("https://api.coingecko.com/api/v3/simple/price").mock(
        return_value=httpx.Response(200, text='{"quantum-resistant-ledger":{"usd":1.11,"last_updated_at":1791912644}}')
    )
    api = HttpRates()
    try:
        fiat = await api.fiat()
        assert fiat.per_usd["RUB"] == Decimal("84.043643")  # straight to Decimal, never through float
        quotes = await api.crypto({"QRL": "quantum-resistant-ledger"})
        assert quotes["QRL"].usd == Decimal("1.11")
        assert coingecko.calls[0].request.url.params["ids"] == "quantum-resistant-ledger"
    finally:
        await api.aclose()


@pytest.mark.parametrize(
    ("said", "text"),
    [
        ("8 млрд умножить на 0 3%", "🧮 8 млрд умножить на 0,3% = 24 000 000"),  # R33: the Mac heard «0,3» as «0 3»
        ("8 млрд умножить на ноль три процента", "🧮 8 млрд умножить на 0,3 процента = 24 000 000"),
        (
            "восемь миллиардов умножить на ноль целых три десятых процента",
            "🧮 8 миллиардов умножить на 0,3 процента = 24 000 000",
        ),
        ("8 миллиардов умножить на ноль запятая три процента", "🧮 8 миллиардов умножить на 0,3 процента = 24 000 000"),
        ("2 целых 25 сотых плюс 1", "🧮 2,25 плюс 1 = 3,25"),
        ("три плюс два", "🧮 3 плюс 2 = 5"),
        ("10 000 000 ÷ 1500", "🧮 10 000 000 ÷ 1500 ≈ 6 666,67"),  # typed text is shown as written
    ],
)
def test_dictated_numbers_and_decimals(said: str, text: str) -> None:
    calc = calculate(said)
    assert calc is not None and calc.text().replace("\xa0", " ") == text
