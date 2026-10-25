"""A personal estimate of the Samsung sleep score from what Health Connect carries (stages), learnt from the owner's own
Samsung Health export («Загрузка личных данных»: com.samsung.shealth.sleep + com.samsung.health.sleep_stage CSVs).

Samsung's score is the sum of five parts (checked on a real export: always exact): total sleep time (≤34),
deep (≤16), REM (≤24), awakenings (≤12) and falling asleep (≤16). Health Connect gets the stages but not the time it
took to fall asleep nor the movements, so the first four parts are learnt as curves of asleep / deep / REM / awake
minutes (mean part per quantile bin, linear in between) and the fifth is the owner's typical value. On a year of real
nights: 5-fold MAE ≈ 6 points, about 3 of 4 nights within ±10 — shown with «≈», never as Samsung's own number.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from statistics import mean, median
from typing import Any

from pydantic import BaseModel

STAGES = {"40001": "awake", "40002": "light", "40003": "deep", "40004": "rem"}
PARTS = ("tst", "deep", "rem", "wake")  # learnt as curves; «latency» is a constant
BINS = 12


@dataclass(frozen=True)
class ExportNight:
    """One scored session of the export (UTC times, minutes by stage, Samsung's score and its parts)."""

    start: datetime
    end: datetime
    score: int
    asleep: float
    deep: float
    rem: float
    awake: float
    parts: dict[str, int] | None  # tst, deep, rem, wake, latency — only in newer exports


class QualityModel(BaseModel):
    curves: dict[str, list[tuple[float, float]]]
    latency: float
    nights: int
    mae: float | None = None  # out of sample, points
    within10: float | None = None  # share of nights within ±10
    fitted_at: str = ""


def _rows(path: Path) -> list[dict[str, str]]:
    """Samsung CSV: a line with the table name, then the header (columns may carry a «com.samsung…» prefix)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        f.readline()
        out = []
        for row in csv.DictReader(f):
            out.append({k.split(".")[-1]: (v or "").strip() for k, v in row.items() if k and isinstance(v, str)})
        return out


def _utc(text: str) -> datetime:
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _one(directory: Path, prefix: str) -> Path:
    found = sorted(directory.glob(f"{prefix}.*.csv"))
    if not found:
        raise FileNotFoundError(f"{prefix}.*.csv not in {directory}")
    return found[-1]


def read_export(directory: Path) -> list[ExportNight]:
    """Scored sleep sessions of a Samsung Health export folder, with minutes by stage from the stage file."""
    stages: dict[str, list[dict[str, str]]] = defaultdict(list)
    for s in _rows(_one(directory, "com.samsung.health.sleep_stage")):
        stages[s.get("sleep_id", "")].append(s)
    nights = []
    for r in _rows(_one(directory, "com.samsung.shealth.sleep")):
        if not r.get("sleep_score") or not r.get("start_time") or not r.get("end_time"):
            continue
        minutes: dict[str, float] = defaultdict(float)
        for s in stages.get(r.get("datauuid", ""), []):
            span = (_utc(s["end_time"]) - _utc(s["start_time"])).total_seconds() / 60
            minutes[STAGES.get(s.get("stage", ""), "other")] += span
        keys = ("total_sleep_time_score", "deep_score", "rem_score", "wake_score", "latency_score")
        parts = None
        if all(r.get(k) for k in keys):
            parts = dict(zip(("tst", "deep", "rem", "wake", "latency"), (int(float(r[k])) for k in keys), strict=True))
        nights.append(
            ExportNight(
                _utc(r["start_time"]),
                _utc(r["end_time"]),
                int(float(r["sleep_score"])),
                minutes["light"] + minutes["deep"] + minutes["rem"],
                minutes["deep"],
                minutes["rem"],
                minutes["awake"],
                parts,
            )
        )
    return sorted(nights, key=lambda n: n.start)


def _feature(night: Any, part: str) -> float:
    return float({"tst": night.asleep, "deep": night.deep, "rem": night.rem, "wake": night.awake}[part])


def _curve(points: list[tuple[float, float]], bins: int = BINS) -> list[tuple[float, float]]:
    points = sorted(points)
    size = max(len(points) // bins, 1)
    return [
        (round(mean(p[0] for p in points[i : i + size]), 2), round(mean(p[1] for p in points[i : i + size]), 3))
        for i in range(0, len(points), size)
    ]


def _at(curve: list[tuple[float, float]], x: float) -> float:
    if x <= curve[0][0]:
        return curve[0][1]
    for (x1, y1), (x2, y2) in pairwise(curve):
        if x <= x2:
            return y1 + (y2 - y1) * (x - x1) / (x2 - x1) if x2 > x1 else y2
    return curve[-1][1]


def _build(nights: list[ExportNight]) -> tuple[dict[str, list[tuple[float, float]]], float]:
    curves = {p: _curve([(_feature(n, p), float(n.parts[p])) for n in nights if n.parts]) for p in PARTS}
    return curves, float(median(n.parts["latency"] for n in nights if n.parts))


def estimate(model: QualityModel, *, asleep: float, deep: float, rem: float, awake: float) -> int:
    features = {"tst": asleep, "deep": deep, "rem": rem, "wake": awake}
    total = sum(_at(model.curves[p], float(features[p])) for p in PARTS) + model.latency
    return round(max(0.0, min(100.0, total)))


def fit(nights: list[ExportNight], now: datetime, folds: int = 5) -> QualityModel:
    """Curves from every night that has Samsung's parts; the error is measured on held-out stretches of time."""
    known = [n for n in nights if n.parts]
    if len(known) < 30:
        raise ValueError(f"too few nights with score parts in the export: {len(known)} (need 30)")
    errors: list[float] = []
    size = len(known) // folds
    for i in range(folds):
        test = known[i * size : (i + 1) * size]
        curves, latency = _build(known[: i * size] + known[(i + 1) * size :])
        trial = QualityModel(curves=curves, latency=latency, nights=0)
        errors += [abs(estimate(trial, asleep=n.asleep, deep=n.deep, rem=n.rem, awake=n.awake) - n.score) for n in test]
    curves, latency = _build(known)
    return QualityModel(
        curves=curves,
        latency=latency,
        nights=len(known),
        mae=round(mean(errors), 1),
        within10=round(sum(e <= 10 for e in errors) / len(errors), 2),
        fitted_at=now.isoformat(),
    )
