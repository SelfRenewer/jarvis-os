"""Currency conversion from chat: «сколько 10 000 руб. в долларах», «100 лари в рублях», «курс доллара».

The phrase is parsed by code (amount, source, targets) and converted with Decimal through USD-based rates
(the same sources and default currency list as the user's currency-tracker site). A message with any other
words («потратил 45 лари на такси») is not a conversion, so it keeps going to the other agents. No LLM.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from jarvis.calc.expression import decimal_text, group_digits, parse_amount
from jarvis.core.config import Settings
from jarvis.core.errors import TransientError
from jarvis.integrations.fx.client import CryptoQuote, FiatRates, RatesApi

log = logging.getLogger(__name__)

# Code → CoinGecko id (currency-tracker has QRL; the rest are the usual questions).
CRYPTO_IDS = {
    "QRL": "quantum-resistant-ledger",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "tether",
    "TON": "the-open-network",
}
FIAT_TTL = 30 * 60.0  # open.er-api updates once a day; currency-tracker treats 30 min as fresh
CRYPTO_TTL = 5 * 60.0

_ALIASES: list[tuple[str, str]] = [
    (r"белорусск\w*\s+рубл\w*|byn", "BYN"),
    (r"рубл\w*|руб\b\.?|₽|rub\b|rur\b", "RUB"),
    (r"доллар\w*|долл\b\.?|бакс\w*|usd\b|\$", "USD"),
    (r"евро\b|€|eur\b", "EUR"),
    (r"лари\b|₾|gel\b", "GEL"),
    (r"драм\w*|֏|amd\b", "AMD"),
    (r"(?:турецк\w*\s+)?лир(?:а|ы|у|ах|ам|ами)?\b|₺", "TRY"),
    (r"тенге\b|₸|kzt\b", "KZT"),
    (r"гривн\w*|₴|uah\b", "UAH"),
    (r"юан\w*|cny\b|rmb\b", "CNY"),
    (r"фунт\w*\s+стерлинг\w*|фунт\w*|£|gbp\b", "GBP"),
    (r"[йи]ен(?:а|ы|у|ах|ам|ами)?\b|jpy\b", "JPY"),
    (r"(?:швейцарск\w*\s+)?франк\w*|chf\b", "CHF"),
    (r"дирхам\w*|aed\b", "AED"),
    (r"злот\w*|pln\b", "PLN"),
    (r"манат\w*|azn\b", "AZN"),
    (r"рупи[йияю]\w*|inr\b", "INR"),
    (r"(?:киргизск\w*|кыргызск\w*)\s+сом\w*|kgs\b", "KGS"),
    (r"узбекск\w*\s+сум\w*|uzs\b", "UZS"),
    (r"биткоин\w*|биткойн\w*|btc\b", "BTC"),
    (r"эфириум\w*|эфир[аеу]?\b|eth\b", "ETH"),
    (r"usdt\b|тезер\w*|tether\b", "USDT"),
    (r"тонкоин\w*|toncoin\b|ton\b", "TON"),
    (r"qrl\b|кью\s*ар\s*эл\w*", "QRL"),
]
_CURRENCY = re.compile(
    "|".join(rf"(?P<c{i}>(?<![a-zа-яё]){pattern})" for i, (pattern, _) in enumerate(_ALIASES)), re.IGNORECASE
)
_ISO = re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])")
_AMOUNT = re.compile(
    r"(?<![\w.,])(?:полтор[аы]|\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s*(?:тыс(?:яч[аиу]?)?\.?|к|k|млн\.?|миллион(?:а|ов)?|млрд\.?|миллиард(?:а|ов)?)(?![a-zа-яё]))?"
)
# Words that may surround a conversion; any other word means the message is about something else.
_GLUE = frozenset(
    [
        "в",
        "во",
        "на",
        "к",
        "ко",
        "to",
        "in",
        "into",
        "сколько",
        "это",
        "будет",
        "курс",
        "курсы",
        "курса",
        "курсу",
        "по",
        "стоит",
        "стоят",
        "почем",
        "почём",
        "переведи",
        "перевести",
        "конвертируй",
        "конвертировать",
        "посчитай",
        "сегодня",
        "сейчас",
        "текущий",
        "какой",
        "какая",
        "а",
        "и",
        "получится",
        "выйдет",
        "мне",
        "пожалуйста",
        "джарвис",
        "jarvis",
        "равно",
        "примерно",
        "один",
        "одна",
        "валют",
        "валюты",
    ]
)
_RATE_WORD = re.compile(r"курс|сто[иия]т|поч[её]м|сколько|переведи|конвертир")
_NOT_CURRENCIES = {"TLT", "TPL", "LMF"}  # the user's trackers, not ISO codes
_ALL_RATES = re.compile(r"(?:какой\s+|какие\s+)?курс[ыа]?\s+валют|курсы")


@dataclass(frozen=True)
class ConversionRequest:
    amount: Decimal | None  # None → a rate question («курс доллара») = 1 unit
    source: str
    targets: tuple[str, ...]  # empty → the default list


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    code: str


def _mentions(text: str) -> list[_Mention]:
    found = []
    for m in _CURRENCY.finditer(text.lower()):
        index = int(next(name for name, value in m.groupdict().items() if value is not None)[1:])
        found.append(_Mention(m.start(), m.end(), _ALIASES[index][1]))
    for m in _ISO.finditer(text):
        if m.group() not in _NOT_CURRENCIES and not any(f.start <= m.start() < f.end for f in found):
            found.append(_Mention(m.start(), m.end(), m.group()))
    return sorted(found, key=lambda f: f.start)


def parse_conversion(text: str) -> ConversionRequest | None:
    """«сколько 10 000 руб. в долларах» → (10000, RUB, (USD,)); None when the message is about something else."""
    t = text.strip().rstrip("?!. ")
    low = t.lower()
    mentions = _mentions(t)
    if not mentions:
        return ConversionRequest(None, "USD", ()) if _ALL_RATES.fullmatch(low) else None
    amounts = list(_AMOUNT.finditer(low))
    if len(amounts) > 1:
        return None
    if not amounts and len(mentions) == 1 and not _RATE_WORD.search(low):
        return None  # «USD» or «доллар» alone is not a question
    amount = parse_amount(amounts[0].group()) if amounts else None
    if amounts and amount is None:
        return None
    rest = list(low)
    for start, end in [(m.start, m.end) for m in mentions] + [a.span() for a in amounts]:
        rest[start:end] = " " * (end - start)
    for word in re.findall(r"[a-zа-яё]+|[^\s\w]", "".join(rest)):
        if word not in _GLUE and word not in ",=→->:.":
            return None
    source = mentions[0]  # «курс доллара к рублю», «доллар в рублях»
    if amounts:  # the currency written right after (or before: «$100») the amount
        a_start, a_end = amounts[0].span()
        after = [m for m in mentions if m.start >= a_end and not low[a_end : m.start].strip()]
        before = [m for m in mentions if m.end <= a_start and not low[m.end : a_start].strip()]
        if not after and not before:
            return None
        source = (after or before)[0]
    targets: list[str] = []
    for m in mentions:
        if m is not source and m.code != source.code and m.code not in targets:
            targets.append(m.code)
    return ConversionRequest(amount, source.code, tuple(targets))


# ISO 4217 exponents that are not 2 (the rest have cents).
_EXPONENT = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "UGX": 0, "KWD": 3, "BHD": 3, "OMR": 3, "TND": 3}


def exponent(currency: str) -> int:
    return _EXPONENT.get(currency, 2)


def money_text(value: Decimal, code: str) -> str:
    """Fiat: the currency's own decimals («118,99», «1,234 KWD», «1 500 JPY»), below 1 — 4 significant digits
    («0,8952»); crypto: 6 («0,00120245»). L10 (review 17.10): three-decimal dinars lost their last digit."""
    with localcontext() as ctx:
        ctx.prec = 40
        if code in CRYPTO_IDS or abs(value) < 1:
            if value == 0:
                return "0"
            significant = 6 if code in CRYPTO_IDS else 4
            places = min(10, max(2, significant - 1 - value.copy_abs().adjusted()))
            return decimal_text(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
        places = exponent(code)
        rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    whole, _, cents = f"{abs(rounded):f}".partition(".")
    return f"{sign}{group_digits(whole)}" + (f",{cents.ljust(places, '0')}" if places else "")


class FxService:
    def __init__(self, settings: Settings, api: RatesApi | None) -> None:
        self.settings = settings
        self.api = api
        self._fiat: tuple[float, FiatRates] | None = None
        self._crypto: dict[str, tuple[float, CryptoQuote]] = {}

    async def fiat(self) -> tuple[FiatRates, bool]:
        """(rates, stale). Fresh for 30 min; on an outage the last known rates are used and marked stale."""
        if self.api is None:
            raise TransientError("курсы валют отключены (FX_ENABLED=false)")
        if self._fiat and time.monotonic() - self._fiat[0] < FIAT_TTL:
            return self._fiat[1], False
        try:
            rates = await self.api.fiat()
        except Exception as exc:
            log.warning("fiat rates unavailable: %s", exc)
            if self._fiat:
                return self._fiat[1], True
            raise TransientError("нет связи с open.er-api.com") from exc
        self._fiat = (time.monotonic(), rates)
        return rates, False

    async def crypto(self, codes: set[str]) -> dict[str, CryptoQuote]:
        """Quotes for the coins asked; a coin with no quote (outage, never fetched) is left out."""
        if self.api is None or not codes:
            return {}
        now = time.monotonic()
        stale = {code for code in codes if code not in self._crypto or now - self._crypto[code][0] >= CRYPTO_TTL}
        if stale:
            try:
                fresh = await self.api.crypto({code: CRYPTO_IDS[code] for code in stale})
            except Exception as exc:
                log.warning("crypto rates unavailable: %s", exc)
                fresh = {}
            for code, quote in fresh.items():
                self._crypto[code] = (now, quote)
        return {code: self._crypto[code][1] for code in codes if code in self._crypto}

    def _when(self, value: datetime) -> str:
        return value.astimezone(self.settings.tz).strftime("%d.%m %H:%M")

    async def convert(self, request: ConversionRequest) -> str:
        targets = list(request.targets) or [c for c in self.settings.fx_default_currencies if c != request.source]
        explicit = set(request.targets) | {request.source}
        fiat, stale = await self.fiat()
        quotes = await self.crypto({c for c in [request.source, *targets] if c in CRYPTO_IDS})
        per_usd = dict(fiat.per_usd)
        with localcontext() as ctx:
            ctx.prec = 40
            per_usd.update({code: 1 / quote.usd for code, quote in quotes.items()})
            if request.source not in per_usd:
                return self._unknown(request.source)
            amount = request.amount if request.amount is not None else Decimal(1)
            lines: list[str] = []
            values: list[tuple[str, Decimal]] = []
            for code in targets:
                if code not in per_usd:
                    if code in explicit:
                        lines.append(f"• {code} — курс недоступен")
                    continue
                values.append((code, amount / per_usd[request.source] * per_usd[code]))
            head = f"{decimal_text(amount)} {request.source}"
            if len(values) == 1 and not lines:
                code, value = values[0]
                text = [
                    f"💱 {head} ≈ {money_text(value, code)} {code}",
                    f"Курс: {self._rate(request.source, code, per_usd)}",
                ]
            elif values or lines:
                text = [f"💱 {head} ≈"] + [f"• {money_text(value, code)} {code}" for code, value in values] + lines
            else:
                return self._unknown(targets[0] if targets else request.source)
        sources = f"open.er-api.com, {self._when(fiat.updated_at)}"
        coins = sorted(code for code in quotes if code in {request.source, *(c for c, _ in values)})
        if coins:
            sources += f" · {', '.join(coins)} — CoinGecko, {self._when(max(quotes[c].updated_at for c in coins))}"
        text.append(("⚠️ Нет связи — последние известные курсы: " if stale else "Курсы: ") + sources)
        return "\n".join(text)

    @staticmethod
    def _rate(source: str, target: str, per_usd: dict[str, Decimal]) -> str:
        ratio = per_usd[target] / per_usd[source]  # target units for 1 source
        if ratio >= 1:
            return f"1 {source} = {money_text(ratio, target)} {target}"
        return f"1 {target} = {money_text(1 / ratio, source)} {source}"

    @staticmethod
    def _unknown(code: str) -> str:
        return f"Не знаю курс {code} — такой валюты нет у open.er-api.com и в списке криптовалют."
