"""Exchange rates: the same free sources as the user's currency-tracker site (~/Sites/currency-tracker).

Fiat — open.er-api.com (exchangerate-api.com open endpoint, no key, updated once a day). Crypto — CoinGecko
`simple/price` (no key). Numbers are parsed straight into Decimal (money is never a float).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx

from jarvis.core.errors import PermanentError, TransientError
from jarvis.core.http import ResilientHttp
from jarvis.core.retry import RetryPolicy

# A chat answer waits for this: one quick retry, no long backoff.
CHAT_RETRY = RetryPolicy(base_seconds=0.5, cap_seconds=1.0, max_attempts=2, jitter_ratio=0)


@dataclass(frozen=True)
class FiatRates:
    per_usd: dict[str, Decimal]  # units of a currency for 1 USD
    updated_at: datetime


@dataclass(frozen=True)
class CryptoQuote:
    usd: Decimal  # price of one coin in USD
    updated_at: datetime


class RatesApi(Protocol):
    async def fiat(self) -> FiatRates: ...

    async def crypto(self, coins: Mapping[str, str]) -> dict[str, CryptoQuote]:
        """`coins`: code → CoinGecko id. Unknown ids are left out of the answer."""
        ...


def _decimal_json(response: httpx.Response) -> Any:
    try:
        return json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
    except ValueError as exc:
        raise PermanentError("rates: not JSON") from exc


def parse_fiat(data: Any) -> FiatRates:
    try:
        if data.get("result") != "success":
            raise PermanentError(f"open.er-api: {data.get('error-type') or data.get('result')}")
        rates = {str(code).upper(): Decimal(value) for code, value in data["rates"].items() if Decimal(value) > 0}
        updated = datetime.fromtimestamp(int(data["time_last_update_unix"]), UTC)
    except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise PermanentError(f"open.er-api: unexpected response ({type(exc).__name__})") from exc
    if rates.get("USD") != 1:
        raise PermanentError("open.er-api: base is not USD")
    return FiatRates(rates, updated)


def parse_crypto(data: Any, coins: Mapping[str, str]) -> dict[str, CryptoQuote]:
    quotes: dict[str, CryptoQuote] = {}
    for code, coin in coins.items():
        try:
            entry = data[coin]
            price = Decimal(entry["usd"])
            updated = datetime.fromtimestamp(int(entry.get("last_updated_at") or 0), UTC)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if price > 0:
            quotes[code] = CryptoQuote(price, updated)
    return quotes


class HttpRates:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._fiat = ResilientHttp(
            "open-er-api", base_url="https://open.er-api.com/v6/", timeout=6.0, retry=CHAT_RETRY, transport=transport
        )
        self._crypto = ResilientHttp(
            "coingecko",
            base_url="https://api.coingecko.com/api/v3/",
            timeout=6.0,
            retry=CHAT_RETRY,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._fiat.aclose()
        await self._crypto.aclose()

    async def fiat(self) -> FiatRates:
        return parse_fiat(_decimal_json(await self._fiat.get("latest/USD")))

    async def crypto(self, coins: Mapping[str, str]) -> dict[str, CryptoQuote]:
        if not coins:
            return {}
        params = {
            "ids": ",".join(sorted(set(coins.values()))),
            "vs_currencies": "usd",
            "include_last_updated_at": "true",
        }
        return parse_crypto(_decimal_json(await self._crypto.get("simple/price", params=params)), coins)


@dataclass
class FakeRates:
    """Fixed rates for tests: set `per_usd` / `crypto_usd`, or `unavailable`."""

    per_usd: dict[str, Decimal] = field(
        default_factory=lambda: {
            "USD": Decimal(1),
            "RUB": Decimal("84.043643"),
            "GEL": Decimal("2.596409"),
            "AMD": Decimal("362.201696"),
            "EUR": Decimal("0.895156"),
            "TRY": Decimal("49.355625"),
            "KZT": Decimal("450.740106"),
        }
    )
    crypto_usd: dict[str, Decimal] = field(default_factory=lambda: {"QRL": Decimal("1.11"), "BTC": Decimal("83160")})
    updated_at: datetime = datetime(2026, 9, 22, 23, 57, tzinfo=UTC)
    unavailable: bool = False
    crypto_unavailable: bool = False
    calls: list[str] = field(default_factory=list)

    async def fiat(self) -> FiatRates:
        self.calls.append("fiat")
        if self.unavailable:
            raise TransientError("rates: fake outage")
        return FiatRates(dict(self.per_usd), self.updated_at)

    async def crypto(self, coins: Mapping[str, str]) -> dict[str, CryptoQuote]:
        self.calls.append("crypto")
        if self.unavailable or self.crypto_unavailable:
            raise TransientError("coingecko: fake outage")
        return {code: CryptoQuote(self.crypto_usd[code], self.updated_at) for code in coins if code in self.crypto_usd}
