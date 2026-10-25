"""Personal sleep-quality estimate: a Samsung Health export (CSV) → curves of the score parts → «≈» for new nights."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.health.quality import ExportNight, QualityModel, estimate, fit, read_export
from jarvis.health.service import HealthService
from tests.conftest import make_settings

SLEEP_HEADER = (
    "com.samsung.health.sleep.start_time,com.samsung.health.sleep.end_time,sleep_score,total_sleep_time_score,"
    "deep_score,rem_score,wake_score,latency_score,com.samsung.health.sleep.datauuid"
)
STAGE_HEADER = (
    "com.samsung.health.sleep_stage.start_time,com.samsung.health.sleep_stage.end_time,"
    "com.samsung.health.sleep_stage.sleep_id,com.samsung.health.sleep_stage.stage"
)


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.000")


def write_export(folder: Path, nights: list[tuple[datetime, int, int, int, int, bool]]) -> None:
    """(start, light, deep, rem, awake minutes, with parts) per night; parts follow a simple rule."""
    sleep = ["com.samsung.shealth.sleep,7006011,12", SLEEP_HEADER]
    stages = ["com.samsung.health.sleep_stage,7006011,7", STAGE_HEADER]
    for i, (start, light, deep, rem, awake, parts) in enumerate(nights):
        t, uid = start, f"s{i}"
        for code, minutes in (("40002", light), ("40003", deep), ("40004", rem), ("40001", awake)):
            if minutes:
                stages.append(f"{stamp(t)},{stamp(t + timedelta(minutes=minutes))},{uid},{code}")
                t += timedelta(minutes=minutes)
        tst, ds, rs, ws, ls = min((light + deep + rem) // 15, 34), min(deep // 6, 16), min(rem // 4, 24), 7, 16
        score = tst + ds + rs + ws + ls
        cells = [str(x) for x in (tst, ds, rs, ws, ls)] if parts else ["", "", "", "", ""]
        sleep.append(",".join([stamp(start), stamp(t), str(score), *cells, uid]))
    (folder / "com.samsung.shealth.sleep.20261014141688.csv").write_text("\n".join(sleep) + "\n", encoding="utf-8-sig")
    (folder / "com.samsung.health.sleep_stage.20261014141688.csv").write_text(
        "\n".join(stages) + "\n", encoding="utf-8-sig"
    )


def test_export_is_read_with_minutes_by_stage(tmp_path: Path) -> None:
    write_export(tmp_path, [(datetime(2026, 10, 13, 22, 0), 240, 60, 90, 20, True)])
    night = read_export(tmp_path)[0]
    assert (night.start, night.asleep, night.deep, night.rem, night.awake) == (
        datetime(2026, 10, 13, 22, 0, tzinfo=UTC), 390.0, 60.0, 90.0, 20.0,
    )  # fmt: skip
    assert night.parts == {"tst": 26, "deep": 10, "rem": 22, "wake": 7, "latency": 16}
    assert night.score == sum(night.parts.values())


def test_the_model_learns_the_users_scores(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, 22, 0)
    nights = [
        (base + timedelta(days=i), 150 + (i * 37) % 200, 20 + (i * 13) % 90, 30 + (i * 17) % 100, 10 + i % 40, True)
        for i in range(60)
    ]
    write_export(tmp_path, nights)
    model = fit(read_export(tmp_path), datetime(2026, 10, 14, tzinfo=UTC))
    assert model.nights == 60 and model.latency == 16
    assert model.mae is not None and model.mae <= 3  # the rule is learnable from the curves
    good = estimate(model, asleep=450, deep=90, rem=100, awake=15)
    short = estimate(model, asleep=200, deep=20, rem=20, awake=60)
    assert good > short and 0 <= short <= 100
    with pytest.raises(ValueError, match="too few nights"):
        fit(read_export(tmp_path)[:10], datetime(2026, 10, 14, tzinfo=UTC))


def test_scores_per_wake_day_use_the_main_night_and_skip_naps() -> None:
    health = HealthService(make_settings(), clock=None)  # type: ignore[arg-type]

    def night(start: str, hours: float, score: int) -> ExportNight:
        begin = datetime.fromisoformat(start).replace(tzinfo=UTC)
        return ExportNight(begin, begin + timedelta(hours=hours), score, hours * 60, 60, 60, 10, None)

    scores = health.export_scores(
        [
            night("2026-10-13T19:00:00", 4, 50),  # 23:00–03:00 Tbilisi: a first part of the night
            night("2026-10-13T23:30:00", 5, 70),  # 03:30–08:30: the longer one wins for 14.10
            night("2026-10-14T10:00:00", 2.5, 90),  # 14:00–16:30: a nap, not a night
        ]
    )
    assert scores == {datetime(2026, 10, 14).date(): 70}


def test_an_estimate_is_clipped_and_needs_stages() -> None:
    model = QualityModel(
        curves={p: [(0.0, 0.0), (500.0, 40.0)] for p in ("tst", "deep", "rem", "wake")}, latency=16, nights=40
    )
    assert estimate(model, asleep=1000, deep=1000, rem=1000, awake=1000) == 100


def test_export_nights_sum_the_parts_of_a_night() -> None:
    health = HealthService(make_settings(), clock=None)  # type: ignore[arg-type]

    def night(start: str, hours: float, score: int) -> ExportNight:
        begin = datetime.fromisoformat(start).replace(tzinfo=UTC)
        return ExportNight(begin, begin + timedelta(hours=hours), score, hours * 60, 60, 60, 10, None)

    nights = health.export_nights(
        [
            night("2026-10-13T19:00:00", 2, 40),  # 23:00–01:00 Tbilisi
            night("2026-10-13T22:00:00", 5, 70),  # 02:00–07:00: the main session gives the score
            night("2026-10-14T10:00:00", 2.5, 90),  # a nap
            night("2026-10-15T00:00:00", 2, 30),  # 2 h alone: too short for a night
        ]
    )
    assert nights == {datetime(2026, 10, 14).date(): (420, 70)}
