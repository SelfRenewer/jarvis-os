from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from jarvis.core.clock import (
    NaiveDatetimeError,
    ensure_aware,
    local_datetime,
    local_day_bounds,
    parse_rfc3339,
    to_local,
    to_utc,
)
from jarvis.core.errors import AuthError, PermanentError, RateLimitedError, TransientError
from jarvis.core.http import CircuitBreaker, ResilientHttp
from jarvis.core.logging import Redactor
from jarvis.core.money import Money, MoneyError
from jarvis.core.retry import RetryPolicy
from jarvis.core.security import (
    TokenCipher,
    hmac_sha256_b64,
    sign_payload,
    twilio_signature,
    verify_payload_signature,
    verify_todoist_signature,
    verify_twilio_signature,
)
from jarvis.core.units import Dimension, Quantity, UnitError, unit_price

TBILISI = ZoneInfo("Asia/Tbilisi")
BERLIN = ZoneInfo("Europe/Berlin")


# --- time ----------------------------------------------------------------------------------------


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_aware(datetime(2026, 1, 1))
    with pytest.raises(NaiveDatetimeError):
        to_utc(datetime(2026, 1, 1))


def test_tbilisi_conversion() -> None:
    utc = datetime(2026, 9, 23, 11, 0, tzinfo=UTC)
    assert to_local(utc, TBILISI).hour == 15  # UTC+4, no DST


def test_parse_rfc3339_requires_offset() -> None:
    assert parse_rfc3339("2026-09-23T11:00:00Z") == datetime(2026, 9, 23, 11, tzinfo=UTC)
    with pytest.raises(NaiveDatetimeError):
        parse_rfc3339("2026-09-23T11:00:00")


def test_dst_day_lengths_berlin() -> None:
    start, end = local_day_bounds(date(2026, 3, 29), BERLIN)  # spring forward
    assert end - start == timedelta(hours=23)
    start, end = local_day_bounds(date(2026, 10, 25), BERLIN)  # fall back
    assert end - start == timedelta(hours=25)


def test_nonexistent_local_time_shifted_forward() -> None:
    value = local_datetime(date(2026, 3, 29), time(2, 30), BERLIN)
    assert value.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # == 03:30 CEST


@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2090, 1, 1), timezones=st.just(UTC)))
def test_timezone_round_trip(value: datetime) -> None:
    for tz in (TBILISI, BERLIN, ZoneInfo("America/New_York")):
        assert to_utc(to_local(value, tz)) == value


# --- money / units -----------------------------------------------------------------------------


def test_money_rejects_float() -> None:
    with pytest.raises(MoneyError):
        Money.of(1.1)  # type: ignore[arg-type]


def test_money_rounding_half_up() -> None:
    assert Money.of("1.005").minor == 101
    assert Money.of("2.50").times("3").amount == Decimal("7.50")
    assert Money.of("0.10") + Money.of("0.20") == Money.of("0.30")


def test_money_currency_mismatch() -> None:
    with pytest.raises(MoneyError):
        Money.of("1", "GEL") + Money.of("1", "USD")


@given(st.lists(st.integers(min_value=0, max_value=10_000_000), max_size=50))
def test_money_sum_non_negative(values: list[int]) -> None:
    total = Money.zero()
    for v in values:
        total = total + Money(v)
    assert not total.is_negative()
    assert total.minor == sum(values)


def test_units_conversion() -> None:
    assert Quantity.of("1", "kg").value == Decimal(1000)
    assert Quantity.of("900", "мл").to("l") == Decimal("0.9")
    assert Quantity.of("2", "кг").dimension == Dimension.MASS
    with pytest.raises(UnitError):
        Quantity.of("1", "kg").to("l")


def test_unit_price_distinguishes_package_sizes() -> None:
    per_l_1000, _ = unit_price(Decimal("4.50"), Quantity.of("1", "L"))
    per_l_900, _ = unit_price(Decimal("4.20"), Quantity.of("900", "ml"))
    per_l_500, label = unit_price(Decimal("2.60"), Quantity.of("500", "ml"))
    assert label == "L"
    assert per_l_1000 == Decimal("4.5000")
    assert per_l_900 == Decimal("4.6667")
    assert per_l_500 == Decimal("5.2000")


@given(
    st.decimals(min_value=Decimal("0.001"), max_value=Decimal("100000"), places=3),
    st.sampled_from([("kg", "g", Decimal(1000)), ("l", "ml", Decimal(1000)), ("dozen", "pcs", Decimal(12))]),
)
def test_unit_conversion_invariant(amount: Decimal, units: tuple[str, str, Decimal]) -> None:
    big, small, factor = units
    assert Quantity.of(amount, big).to(small) == Quantity.of(amount * factor, small).to(small)


# --- security ------------------------------------------------------------------------------------


def test_todoist_signature() -> None:
    body = b'{"event_name":"item:added"}'
    sig = hmac_sha256_b64("secret", body)
    assert verify_todoist_signature("secret", body, sig)
    assert not verify_todoist_signature("secret", body + b" ", sig)
    assert not verify_todoist_signature("secret", body, None)
    assert not verify_todoist_signature("", body, sig)


def test_twilio_signature_round_trip() -> None:
    params = {"CallSid": "CA1", "CallStatus": "completed"}
    sig = twilio_signature("tok", "https://x.test/cb", params)
    assert verify_twilio_signature("tok", "https://x.test/cb", params, sig)
    assert not verify_twilio_signature("tok", "https://x.test/cb2", params, sig)


def test_payload_signature() -> None:
    payload = {"job_id": "1", "repo_id": "golden-app"}
    sig = sign_payload("k", payload)
    assert verify_payload_signature("k", dict(reversed(list(payload.items()))), sig)
    assert not verify_payload_signature("k", {**payload, "repo_id": "other"}, sig)


def test_token_cipher() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    assert cipher.decrypt(cipher.encrypt("refresh-token")) == "refresh-token"
    with pytest.raises(ValueError, match="decrypt"):
        TokenCipher(TokenCipher.generate_key()).decrypt(cipher.encrypt("x"))


def test_redactor_masks_tokens() -> None:
    redactor = Redactor(["super-secret-value"])
    text = (
        "POST https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd/sendMessage "
        "Authorization: Bearer abcdefghijklmnop super-secret-value"
    )
    out = redactor.redact(text)
    assert "ABCDEFGHIJ" not in out
    assert "abcdefghijklmnop" not in out
    assert "super-secret-value" not in out


# --- retry / http -------------------------------------------------------------------------------


def test_retry_policy_bounded_and_deterministic() -> None:
    policy = RetryPolicy(base_seconds=10, cap_seconds=60, max_attempts=5, jitter_ratio=0.2)
    delays = [policy.delay(n, "job-1") for n in range(1, 8)]
    assert delays == [policy.delay(n, "job-1") for n in range(1, 8)]
    assert all(0 <= d <= 60 for d in delays)
    assert RetryPolicy(jitter_ratio=0, base_seconds=1, cap_seconds=100).delay(4) == 8
    assert policy.should_retry(4) and not policy.should_retry(5)


async def _no_sleep(_: float) -> None:
    return None


@respx.mock
async def test_http_retries_transient_then_succeeds() -> None:
    route = respx.get("https://api.test/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    client = ResilientHttp("test", sleep=_no_sleep)
    response = await client.get("https://api.test/x")
    assert response.json() == {"ok": True}
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_http_does_not_retry_auth_errors() -> None:
    route = respx.get("https://api.test/x").mock(return_value=httpx.Response(401))
    client = ResilientHttp("test", sleep=_no_sleep)
    with pytest.raises(AuthError):
        await client.get("https://api.test/x")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_http_post_not_retried_on_5xx() -> None:
    route = respx.post("https://api.test/x").mock(return_value=httpx.Response(502))
    client = ResilientHttp("test", sleep=_no_sleep)
    with pytest.raises(TransientError):
        await client.post("https://api.test/x")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_http_rate_limit_retry_after() -> None:
    sleeps: list[float] = []

    async def record(s: float) -> None:
        sleeps.append(s)

    respx.get("https://api.test/x").mock(return_value=httpx.Response(429, headers={"retry-after": "7"}))
    client = ResilientHttp("test", sleep=record)
    with pytest.raises(RateLimitedError):
        await client.get("https://api.test/x")
    assert sleeps == [7.0, 7.0]
    await client.aclose()


@respx.mock
async def test_http_validation_error_is_permanent() -> None:
    respx.post("https://api.test/x").mock(return_value=httpx.Response(400, json={"error": "bad"}))
    client = ResilientHttp("test", sleep=_no_sleep)
    with pytest.raises(PermanentError):
        await client.post("https://api.test/x", json={})
    await client.aclose()


def test_circuit_breaker_opens_and_half_opens() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=10, _monotonic=lambda: now[0])
    breaker.record_failure()
    breaker.before_call("p")
    breaker.record_failure()
    with pytest.raises(TransientError):
        breaker.before_call("p")
    now[0] = 11.0
    breaker.before_call("p")  # half-open
    breaker.record_success()
    breaker.before_call("p")
