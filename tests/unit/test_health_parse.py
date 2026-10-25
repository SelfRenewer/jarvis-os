"""Life Dashboard Companion payloads (Health Connect on the phone) → readings, by code. Format: its docs/webhook.md."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from jarvis.health.parse import PayloadError, Reading, night_of, parse_payload, parse_time, signature_ok


def test_signature_is_hmac_of_the_raw_body() -> None:
    body = b'{"source": "health_connect"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert signature_ok("s3cret", body, good)
    assert signature_ok(" s3cret ", body, good)  # the app trims the secret it saved
    assert not signature_ok("s3cret", body + b" ", good)  # re-serialised JSON is not the body
    assert not signature_ok("other", body, good)
    assert not signature_ok("s3cret", body, None)
    assert not signature_ok("", body, good)


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-04T06:12:00Z",
        "2026-10-04T06:12:00.203Z",
        "2026-10-04T06:12:00.203456Z",
        "2026-10-04T06:12:00.203456789Z",
    ],
)
def test_times_with_any_fraction(text: str) -> None:
    assert parse_time(text).replace(microsecond=0) == datetime(2026, 10, 4, 6, 12, tzinfo=UTC)


def test_payload_with_sleep_heart_rate_weight_and_totals() -> None:
    parsed = parse_payload(
        {
            "source": "health_connect",
            "sequence": 7,
            "daily_totals": [{"date": "2026-09-23", "steps": 8421, "distance_meters": 6210.4}],
            "heart_rate": [
                {"bpm": 58, "time": "2026-09-23T01:00:00Z", "uuid": "hr1#1790000000000", "source": "com.sec.android"}
            ],
            "weight": [{"kilograms": 69.8, "time": "2026-09-23T04:10:00Z", "uuid": "w1"}],
            "blood_pressure": [{"systolic": 120.0, "diastolic": 80.0, "time": "2026-09-23T04:00:00Z", "uuid": "bp1"}],
            "sleep": [
                {
                    "uuid": "s1",
                    "session_end_time": "2026-09-23T03:30:00Z",
                    "duration_seconds": 28800,
                    "stages": [
                        {"stage": "deep", "start_time": "2026-09-22T20:00:00Z", "end_time": "2026-09-22T21:00:00Z",
                         "duration_seconds": 3600},
                        {"stage": "awake", "start_time": "2026-09-22T21:00:00Z", "end_time": "2026-09-22T21:20:00Z",
                         "duration_seconds": 1200},
                    ],
                }
            ],
            "menstruation_flow": [{"flow": "medium", "time": "2026-09-23T00:00:00Z", "uuid": "c1"}],  # not kept
            "deleted_records": [{"type": "weight", "uuid": "w0"}],
        }
    )  # fmt: skip
    kinds = {r.kind: r for r in parsed.readings}
    assert set(kinds) == {"heart_rate", "weight", "blood_pressure", "sleep"}
    assert (kinds["weight"].value, kinds["blood_pressure"].value2) == (69.8, 80.0)
    sleep = kinds["sleep"]
    assert sleep.start_at == datetime(2026, 9, 22, 19, 30, tzinfo=UTC)  # end minus duration
    assert [s["stage"] for s in sleep.data["stages"]] == ["deep", "awake"]  # type: ignore[index]
    assert parsed.days[0].steps == 8421
    assert parsed.deleted == [("weight", "w0")]
    assert parsed.sequence == 7


def test_test_ping_and_buckets() -> None:
    assert parse_payload({"source": "health_connect", "test": True}).test
    buckets = parse_payload(
        {
            "source": "health_connect",
            "heart_rate": [
                {"bucket_start": "2026-09-23T01:00:00Z", "bucket_end": "2026-09-23T01:01:00Z", "sample_count": 58,
                 "avg": 72.4, "min": 66.0, "max": 81.0}
            ],
        }
    )  # fmt: skip
    reading = buckets.readings[0]
    assert (reading.record_id, reading.value) == ("heart_rate@2026-09-23T01:00:00+00:00", 72.4)
    with pytest.raises(PayloadError):
        parse_payload({"hello": "world"})


def test_night_counts_only_sleep_stages() -> None:
    parsed = parse_payload(
        {
            "source": "health_connect",
            "sleep": [
                {"uuid": "a", "session_end_time": "2026-09-23T03:30:00Z", "duration_seconds": 7200, "stages": [
                    {"stage": "light", "start_time": "2026-09-23T01:30:00Z", "end_time": "2026-09-23T02:30:00Z",
                     "duration_seconds": 3600},
                    {"stage": "rem", "start_time": "2026-09-23T02:30:00Z", "end_time": "2026-09-23T03:00:00Z",
                     "duration_seconds": 1800},
                    {"stage": "awake_in_bed", "start_time": "2026-09-23T03:00:00Z", "end_time": "2026-09-23T03:30:00Z",
                     "duration_seconds": 1800},
                ]},
                {"uuid": "b", "session_end_time": "2026-09-23T01:00:00Z", "duration_seconds": 3600, "stages": []},
            ],
        }
    )  # fmt: skip
    night = night_of(parsed.readings)
    assert night is not None
    assert (night.asleep_min, night.light_min, night.rem_min, night.awake_min) == (150, 60, 30, 30)


def test_sleep_minutes_are_rounded_once_from_the_sums() -> None:
    # M9 (review 17.10): each 31-second stage was rounded to a minute — 100 of them made 100 minutes, not 52.
    stages = [{"stage": "light", "seconds": 31}] * 100 + [{"stage": "awake", "seconds": 29}] * 10
    session = Reading("s", "sleep", datetime(2026, 9, 22, 20, tzinfo=UTC), datetime(2026, 9, 23, 3, tzinfo=UTC),
                      3390, data={"stages": stages})  # fmt: skip
    night = night_of([session])
    assert night is not None
    assert (night.asleep_min, night.light_min, night.awake_min) == (52, 52, 5)
