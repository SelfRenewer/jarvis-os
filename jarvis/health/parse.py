"""Payloads of Life Dashboard Companion (Health Connect on the phone) → readings, by code.

Format: docs/webhook.md of github.com/owen282000/life-dashboard-companion-app (checked on 1.25.0). Every record has
Health Connect's `uuid` (heart-rate samples: `<record>#<epoch ms>`); times are UTC ISO 8601 with «Z»; a sleep session
has `session_end_time` + `duration_seconds` + `stages`. A bucketed series (Data Resolution in the app) has
`bucket_start` instead of a record id: it is stored per window and replaced when the window comes again.
Types the user does not need here (cycle tracking, nutrition — Jarvis has its own food log) are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

# payload key → (value field, second value field)
INSTANT: dict[str, tuple[str, str | None]] = {
    "weight": ("kilograms", None),
    "height": ("meters", None),
    "body_fat": ("percentage", None),
    "lean_body_mass": ("kilograms", None),
    "bone_mass": ("kilograms", None),
    "body_water_mass": ("kilograms", None),
    "heart_rate": ("bpm", None),
    "resting_heart_rate": ("bpm", None),
    "heart_rate_variability": ("heart_rate_variability_millis", None),
    "oxygen_saturation": ("percentage", None),
    "respiratory_rate": ("rate", None),
    "blood_pressure": ("systolic", "diastolic"),
    "blood_glucose": ("mmol_per_liter", None),
    "body_temperature": ("celsius", None),
    "skin_temperature": ("delta_celsius", "baseline_celsius"),
    "vo2_max": ("vo2_ml_per_min_per_kg", None),
    "basal_metabolic_rate": ("kilocalories_per_day", None),
}
INTERVAL: dict[str, str] = {
    "steps": "count",
    "distance": "meters",
    "active_calories": "calories",
    "total_calories": "calories",
    "exercise": "duration_seconds",
    "hydration": "liters",
}
ASLEEP = frozenset({"light", "deep", "rem", "sleeping", "unknown"})
AWAKE = frozenset({"awake", "awake_in_bed", "out_of_bed"})


class PayloadError(ValueError):
    """Not a payload of the phone bridge (wrong shape)."""


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """`X-Signature: sha256=<hex HMAC-SHA256(secret as UTF-8 text, raw body)>`, compared in constant time."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.strip().encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.strip().encode())


def parse_time(value: Any) -> datetime:
    """`2026-10-04T06:12:00Z`, with 0/3/6/9 fraction digits → aware UTC."""
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if m := re.match(r"^(?P<head>[^.]+)\.(?P<digits>\d+)(?P<zone>.*)$", text):  # Python reads at most 6 digits
        text = f"{m['head']}.{m['digits'][:6].ljust(6, '0')}{m['zone']}"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise PayloadError(f"time without zone: {value!r}")
    return moment


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Reading:
    record_id: str
    kind: str
    start_at: datetime
    end_at: datetime | None = None
    value: float | None = None
    value2: float | None = None
    source: str | None = None
    data: dict[str, Any] | None = None


@dataclass
class DayTotals:
    day: date
    steps: int | None
    distance_m: float | None
    active_kcal: float | None
    total_kcal: float | None


@dataclass
class Payload:
    test: bool = False
    sequence: int | None = None
    readings: list[Reading] = field(default_factory=list)
    days: list[DayTotals] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)  # (kind, record id)
    screen: list[ScreenTotals] = field(default_factory=list)  # «Screen Time» payloads


@dataclass
class ScreenTotals:
    day: date
    minutes: int
    apps: list[dict[str, Any]]  # [{name, minutes}] — the top ones


def _bucket(kind: str, item: dict[str, Any]) -> Reading:
    start = parse_time(item["bucket_start"])
    value = _num(item.get("avg") if "avg" in item else item.get("total"))
    data: dict[str, Any] = {k: item[k] for k in ("min", "max", "sample_count") if k in item}
    return Reading(f"{kind}@{start.isoformat()}", kind, start, parse_time(item["bucket_end"]), value, data=data or None)


def _sleep(item: dict[str, Any]) -> Reading:
    end = parse_time(item["session_end_time"])
    seconds = int(item.get("duration_seconds") or 0)
    stages = [
        {
            "stage": str(s.get("stage") or "unknown"),
            "start": parse_time(s["start_time"]).isoformat(),
            "end": parse_time(s["end_time"]).isoformat(),
            "seconds": int(s.get("duration_seconds") or 0),
        }
        for s in item.get("stages") or []
        if s.get("start_time") and s.get("end_time")
    ]
    return Reading(
        str(item["uuid"]),
        "sleep",
        end - timedelta(seconds=seconds),
        end,
        float(seconds),
        source=item.get("source"),
        data={"stages": stages},
    )


def parse_payload(body: dict[str, Any]) -> Payload:
    """One request body → readings, daily totals and deletions. Unknown keys are ignored."""
    if not isinstance(body, dict) or body.get("source") not in ("health_connect", "healthkit_ios", "screen_time"):
        raise PayloadError("not a Life Dashboard payload")
    out = Payload(test=bool(body.get("test")), sequence=body.get("sequence"))
    if out.test:
        return out
    if body.get("source") == "screen_time":
        for item in body.get("screen_time") or []:
            try:
                day = date.fromisoformat(str(item["date"]))
                minutes = int(item.get("total_screen_time_minutes") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            used = [
                (int(a.get("minutes") or 0), str(a.get("name") or a.get("package") or "?")[:60])
                for a in item.get("apps") or []
            ]
            apps = [{"name": name, "minutes": m} for m, name in sorted(used, key=lambda x: -x[0])[:10]]
            out.screen.append(ScreenTotals(day, minutes, apps))
        return out
    for kind, (field_name, second) in INSTANT.items():
        for item in body.get(kind) or []:
            if "bucket_start" in item:
                out.readings.append(_bucket(kind, item))
                continue
            if not item.get("uuid") or not item.get("time"):
                continue
            out.readings.append(
                Reading(
                    str(item["uuid"]),
                    kind,
                    parse_time(item["time"]),
                    value=_num(item.get(field_name)),
                    value2=_num(item.get(second)) if second else None,
                    source=item.get("source"),
                )
            )
    for kind, field_name in INTERVAL.items():
        for item in body.get(kind) or []:
            if "bucket_start" in item:
                out.readings.append(_bucket(kind, item))
                continue
            if not item.get("uuid") or not item.get("start_time"):
                continue
            out.readings.append(
                Reading(
                    str(item["uuid"]),
                    kind,
                    parse_time(item["start_time"]),
                    parse_time(item["end_time"]) if item.get("end_time") else None,
                    _num(item.get(field_name)),
                    source=item.get("source"),
                    data={"type": str(item["type"])} if kind == "exercise" and item.get("type") else None,
                )
            )
    for item in body.get("sleep") or []:
        if item.get("uuid") and item.get("session_end_time"):
            out.readings.append(_sleep(item))
    for total in body.get("daily_totals") or []:
        try:
            day = date.fromisoformat(str(total["date"]))
        except (KeyError, ValueError):
            continue
        steps = total.get("steps")
        out.days.append(
            DayTotals(
                day,
                int(steps) if isinstance(steps, int | float) else None,
                _num(total.get("distance_meters")),
                _num(total.get("active_calories")),
                _num(total.get("total_calories")),
            )
        )
    for gone in body.get("deleted_records") or []:
        if gone.get("uuid"):
            out.deleted.append((str(gone.get("type") or ""), str(gone["uuid"])))
    return out


@dataclass(frozen=True)
class Night:
    """One night (or the sessions of it) as Health Connect stages describe it."""

    start: datetime
    end: datetime
    asleep_min: int
    deep_min: int
    rem_min: int
    light_min: int
    awake_min: int
    staged: bool  # False: the source wrote a session without stages (asleep = the whole session)


def night_of(sessions: list[Reading]) -> Night | None:
    """Sleep sessions of one night → totals by stage (awake stages are not sleep)."""
    if not sessions:
        return None
    seconds = {"deep": 0.0, "rem": 0.0, "light": 0.0, "awake": 0.0, "asleep": 0.0}
    staged = False
    for session in sessions:
        stages = (session.data or {}).get("stages") or []
        if not stages:
            seconds["asleep"] += float(session.value or 0)
            continue
        staged = True
        for stage in stages:
            sec = int(stage.get("seconds") or 0)
            name = str(stage.get("stage"))
            if name in AWAKE:
                seconds["awake"] += sec
            elif name in ASLEEP:
                seconds["asleep"] += sec
                if name in ("deep", "rem", "light"):
                    seconds[name] += sec
    minutes = {k: round(v / 60) for k, v in seconds.items()}  # once, from the sums (M9: rounding per stage drifted)
    start = min(s.start_at for s in sessions)
    end = max(s.end_at or s.start_at for s in sessions)
    return Night(
        start, end, minutes["asleep"], minutes["deep"], minutes["rem"], minutes["light"], minutes["awake"], staged
    )
