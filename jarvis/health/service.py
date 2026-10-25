"""Watch and scale data in Jarvis: what the phone sends is stored here; the night's sleep, weighings and short texts
are built from it by code (no LLM). Offers to write sleep and weight to TLT are made by the `health.ingest` job.

Galaxy Watch → Samsung Health → Health Connect; Xiaomi scale → Zepp Life → (Google Fit →) Health Connect; Health
Connect → Life Dashboard Companion → POST /webhooks/health (home Wi-Fi only). Samsung does not share resting heart rate
or HRV with Health Connect (October 2026), so «пульс во сне» is computed here from the night's heart-rate samples.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import delete, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.bot import fmt
from jarvis.core.clock import Clock, local_day_bounds, to_local
from jarvis.core.config import Settings
from jarvis.core.dates import human_day
from jarvis.db.models import HealthDay, HealthPayload, HealthSample, ScreenDay, UserPreference
from jarvis.health.parse import Night, PayloadError, Reading, night_of, parse_payload, signature_ok
from jarvis.health.quality import ExportNight, QualityModel, estimate

log = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 12_000_000  # the app may send a few MB with a backfill; it asks receivers to allow 10 MB
NIGHT_FROM = time(18, 0)  # a night of day D: sessions that end between D-1 18:00 and D 14:00 (local)
NIGHT_TO = time(14, 0)
_BATCH = 500
DELETE_BATCH = 1000  # record ids per DELETE (each is one pass over the samples)
PING_KEY = "health_last_ping"
MODEL_KEY = "health_sleep_quality_model"  # learnt from the user's Samsung Health export (`jarvis health-sleep-quality`)


@dataclass
class Ingested:
    payloads: int = 0
    readings: int = 0
    deleted: int = 0
    weights: list[Reading] = field(default_factory=list)  # weighings stored for the first time
    nights: set[date] = field(default_factory=set)  # wake days whose sleep changed


@dataclass(frozen=True)
class Heart:
    avg: int | None
    low: int | None
    count: int


def _decode(body: bytes) -> tuple[Any, Any]:
    data = json.loads(body)
    return data, parse_payload(data)


def _sessions(rows: Any) -> list[Reading]:
    """Sleep rows of one night → sessions; naps under 30 minutes are not part of the night."""
    return [
        Reading(r.record_id, r.kind, r.start_at, r.end_at, r.value, data=r.data)
        for r in rows
        if (r.value or 0) >= 30 * 60
    ]


def hm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def _n(value: float, digits: int = 1) -> str:
    number = round(value, digits)
    return (str(int(number)) if float(number).is_integer() else str(number)).replace(".", ",")


def _steps(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _signed(value: float, digits: int) -> str:
    sign = "+" if value > 0 else ("−" if value < 0 else "±")
    shown = _steps(round(abs(value))) if digits == 0 else _n(abs(value), digits)
    return f"{sign}{shown}"


def _signed_hm(minutes: float) -> str:
    sign = "+" if minutes > 0 else ("−" if minutes < 0 else "±")
    return f"{sign}{hm(round(abs(minutes)))}"


class HealthService:
    def __init__(self, settings: Settings, clock: Clock) -> None:
        self.settings = settings
        self.clock = clock

    def configured(self) -> bool:
        # An empty value is not a secret (compose passes HEALTH_WEBHOOK_SECRET="" when .env has none): an empty HMAC
        # key would let anyone sign.
        secret = self.settings.health_webhook_secret
        return secret is not None and bool(secret.get_secret_value())

    def from_scale(self, source: str | None) -> bool:
        """A weighing worth offering and comparing: from `HEALTH_WEIGHT_SOURCES` (empty = any source)."""
        sources = self.settings.health_weight_sources
        return not sources or (source or "") in sources

    def _scale_weights(self) -> Any:
        """SQL condition: weight rows from the scale sources only."""
        sources = self.settings.health_weight_sources
        condition = HealthSample.kind == "weight"
        return condition & HealthSample.source.in_(sources) if sources else condition

    # --- intake (the `health` service) -------------------------------------------------------------------

    async def accept(self, session: AsyncSession, body: bytes, signature: str | None) -> tuple[int, str]:
        """A request from the phone → (HTTP status, reason). Stored as received; the job reads it later.
        401 on a bad signature: the app keeps the payload and the ones after it until the secret is fixed."""
        secret = self.settings.health_webhook_secret
        if secret is None or not self.configured():
            return 503, "health webhook disabled (HEALTH_WEBHOOK_SECRET)"
        if len(body) > MAX_PAYLOAD_BYTES:
            return 413, "payload too large"
        if not signature_ok(secret.get_secret_value(), body, signature):
            return 401, "bad signature"
        try:  # up to 12 MB: parsed off the event loop, the other requests keep being served (L13)
            data, parsed = await asyncio.to_thread(_decode, body)
        except (ValueError, PayloadError, KeyError, TypeError) as exc:
            log.warning("health payload refused: %s", exc)
            return 400, "not a Life Dashboard payload"
        # The signature covers the body only, so a captured request can be sent again: every accepted request must
        # be idempotent. Test pings update one marker; sync payloads are unique by `sequence` (required).
        if parsed.test:
            await self._mark_ping(session)
            return 204, "test"
        if not isinstance(parsed.sequence, int) or isinstance(parsed.sequence, bool):
            return 400, "payload without sequence"
        await session.execute(
            insert(HealthPayload)
            .values(sequence=parsed.sequence, body=data)
            .on_conflict_do_nothing(index_elements=[HealthPayload.sequence])
        )
        return 204, "stored"

    async def _mark_ping(self, session: AsyncSession) -> None:
        now = self.clock.now().isoformat()
        stmt = insert(UserPreference).values(key=PING_KEY, value={"at": now})
        await session.execute(
            stmt.on_conflict_do_update(index_elements=[UserPreference.key], set_={"value": {"at": now}})
        )

    # --- processing (the `health.ingest` job) -----------------------------------------------------------

    async def ingest(self, session: AsyncSession, now: datetime, limit: int = 50) -> Ingested:
        """Pending payloads → readings (one row per Health Connect record), daily totals, deletions."""
        rows = (
            await session.scalars(
                select(HealthPayload)
                .where(HealthPayload.processed_at.is_(None))
                .order_by(HealthPayload.sequence.nulls_first(), HealthPayload.received_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        out = Ingested()
        for row in rows:
            try:
                parsed = parse_payload(row.body)
            except (PayloadError, KeyError, TypeError, ValueError) as exc:
                log.warning("health payload %s skipped: %s", row.id, exc)
                row.processed_at = now
                continue
            await self._store(session, parsed.readings, out)
            await self._delete(session, parsed.deleted, out)
            for screen in parsed.screen:  # every sync sends 7 days again: the highest sequence wins per date
                stmt_s = insert(ScreenDay).values(
                    day=screen.day, minutes=screen.minutes, apps=screen.apps, sequence=parsed.sequence
                )
                await session.execute(
                    stmt_s.on_conflict_do_update(
                        index_elements=[ScreenDay.day],
                        set_={
                            "minutes": stmt_s.excluded.minutes,
                            "apps": stmt_s.excluded.apps,
                            "sequence": stmt_s.excluded.sequence,
                            "updated_at": func.now(),
                        },
                        where=or_(ScreenDay.sequence.is_(None), ScreenDay.sequence <= stmt_s.excluded.sequence),
                    )
                )
            for day in parsed.days:
                stmt = insert(HealthDay).values(
                    day=day.day,
                    steps=day.steps,
                    distance_m=day.distance_m,
                    active_kcal=day.active_kcal,
                    total_kcal=day.total_kcal,
                    sequence=parsed.sequence,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[HealthDay.day],
                        set_={
                            "steps": stmt.excluded.steps,
                            "distance_m": stmt.excluded.distance_m,
                            "active_kcal": stmt.excluded.active_kcal,
                            "total_kcal": stmt.excluded.total_kcal,
                            "sequence": stmt.excluded.sequence,
                            "updated_at": func.now(),
                        },
                        # a late payload must not overwrite a newer total of the same day
                        where=or_(HealthDay.sequence.is_(None), HealthDay.sequence <= stmt.excluded.sequence),
                    )
                )
            row.processed_at = now
            out.payloads += 1
        return out

    async def _delete(self, session: AsyncSession, deleted: list[tuple[str, str]], out: Ingested) -> None:
        """Records deleted in Health Connect (with their `<id>#<ms>` samples), a batch per statement — one record
        per statement meant a scan of the whole table each (L13, review 17.10). A deleted night changes its own
        wake day, not today (L1)."""
        ids = list(dict.fromkeys(record_id for _, record_id in deleted))
        sleep = [record_id for kind, record_id in deleted if kind == "sleep"]
        if sleep:
            for end_at, start_at in (
                await session.execute(
                    select(HealthSample.end_at, HealthSample.start_at).where(
                        HealthSample.kind == "sleep", HealthSample.record_id.in_(sleep)
                    )
                )
            ).all():
                out.nights.add(self.wake_day(end_at or start_at))
        for i in range(0, len(ids), DELETE_BATCH):
            chunk = ids[i : i + DELETE_BATCH]
            result = await session.execute(
                delete(HealthSample).where(func.split_part(HealthSample.record_id, "#", 1).in_(chunk))
            )
            out.deleted += int(getattr(result, "rowcount", 0) or 0)

    async def _store(self, session: AsyncSession, readings: list[Reading], out: Ingested) -> None:
        for start in range(0, len(readings), _BATCH):
            chunk = readings[start : start + _BATCH]
            values = [
                {
                    "record_id": r.record_id[:160],
                    "kind": r.kind,
                    "source": (r.source or None) and str(r.source)[:160],
                    "start_at": r.start_at,
                    "end_at": r.end_at,
                    "value": r.value,
                    "value2": r.value2,
                    "data": r.data,
                }
                for r in {r.record_id: r for r in chunk}.values()  # one row per record in a statement
            ]
            stmt = insert(HealthSample).values(values)
            result: Any = await session.execute(
                stmt.on_conflict_do_update(  # a record sent again after an edit at its source replaces the old one
                    index_elements=[HealthSample.record_id],
                    set_={
                        "kind": stmt.excluded.kind,
                        "source": stmt.excluded.source,
                        "start_at": stmt.excluded.start_at,
                        "end_at": stmt.excluded.end_at,
                        "value": stmt.excluded.value,
                        "value2": stmt.excluded.value2,
                        "data": stmt.excluded.data,
                    },
                ).returning(HealthSample.record_id, HealthSample.kind, literal_column("(xmax = 0)").label("inserted"))
            )
            by_id = {r.record_id[:160]: r for r in chunk}
            for record_id, kind, inserted in result.all():
                out.readings += 1
                reading = by_id[record_id]
                if kind == "weight" and inserted:
                    out.weights.append(reading)
                if kind == "sleep":
                    out.nights.add(self.wake_day(reading.end_at or reading.start_at))

    # --- days and nights ----------------------------------------------------------------------------------

    def today(self, now: datetime) -> date:
        return to_local(now, self.settings.tz).date()

    def wake_day(self, moment: datetime) -> date:
        local = to_local(moment, self.settings.tz)
        return local.date() + timedelta(days=1) if local.time() >= NIGHT_FROM else local.date()

    def _night_bounds(self, day: date) -> tuple[datetime, datetime]:
        tz = self.settings.tz
        return (
            datetime.combine(day - timedelta(days=1), NIGHT_FROM, tzinfo=tz),
            datetime.combine(day, NIGHT_TO, tzinfo=tz),
        )

    async def night(self, session: AsyncSession, day: date) -> tuple[Night | None, Heart]:
        """The night that ended on `day` (all its sessions: Samsung splits a night when you get up)."""
        start, end = self._night_bounds(day)
        rows = (
            await session.scalars(
                select(HealthSample).where(
                    HealthSample.kind == "sleep", HealthSample.end_at >= start, HealthSample.end_at < end
                )
            )
        ).all()
        night = night_of(_sessions(rows))
        if night is None:
            return None, Heart(None, None, 0)
        return night, await self._heart(session, night.start, night.end)

    async def _heart(self, session: AsyncSession, start: datetime, end: datetime) -> Heart:
        values = [
            v
            for v in (
                await session.scalars(
                    select(HealthSample.value).where(
                        HealthSample.kind == "heart_rate",
                        HealthSample.start_at >= start,
                        HealthSample.start_at <= end,
                        HealthSample.value.is_not(None),
                    )
                )
            ).all()
            if v is not None and 25 <= v <= 230
        ]
        if not values:
            return Heart(None, None, 0)
        return Heart(round(mean(values)), round(min(values)), len(values))

    # --- sleep quality: a personal estimate of the Samsung score (jarvis/health/quality.py) -------------------

    async def quality_model(self, session: AsyncSession) -> QualityModel | None:
        row = await session.get(UserPreference, MODEL_KEY)
        return QualityModel.model_validate(row.value) if row is not None else None

    async def save_quality_model(self, session: AsyncSession, model: QualityModel) -> None:
        value = model.model_dump(mode="json")
        stmt = insert(UserPreference).values(key=MODEL_KEY, value=value)
        await session.execute(stmt.on_conflict_do_update(index_elements=[UserPreference.key], set_={"value": value}))

    @staticmethod
    def night_quality(model: QualityModel | None, night: Night) -> int | None:
        if model is None or not night.staged:
            return None
        return estimate(model, asleep=night.asleep_min, deep=night.deep_min, rem=night.rem_min, awake=night.awake_min)

    def export_nights(self, nights: list[ExportNight]) -> dict[date, tuple[int, int]]:
        """(asleep minutes of the whole night, Samsung's score of its main session) per wake day — the same nights
        as `night()`: sessions ending D-1 18:00 … D 14:00, at least 30 min; nights under the minimum are left out."""
        asleep: dict[date, float] = defaultdict(float)
        for n in nights:
            local_end = to_local(n.end, self.settings.tz).time()
            if NIGHT_TO <= local_end < NIGHT_FROM or n.asleep < 30:
                continue
            asleep[self.wake_day(n.end)] += n.asleep
        scores = self.export_scores(nights)
        return {
            day: (round(total), scores[day])
            for day, total in asleep.items()
            if day in scores and total >= self.settings.health_min_sleep_hours * 60
        }

    def export_scores(self, nights: list[ExportNight]) -> dict[date, int]:
        """Samsung's own score per wake day: the longest session of the night (naps ending 14:00–18:00 are not)."""
        best: dict[date, ExportNight] = {}
        for n in nights:
            local_end = to_local(n.end, self.settings.tz).time()
            if NIGHT_TO <= local_end < NIGHT_FROM or n.asleep < self.settings.health_min_sleep_hours * 60:
                continue
            day = self.wake_day(n.end)
            if day not in best or n.asleep > best[day].asleep:
                best[day] = n
        return {day: n.score for day, n in best.items()}

    def night_line(self, night: Night, heart: Heart, quality: int | None = None) -> str:
        """«7:30 (23:30–07:30) · глубокий 1:00 · REM 1:30 · пульс во сне ~55 (мин. 50)» — plain text: it is also kept
        in a TLT draft header."""
        tz = self.settings.tz
        bits = [f"{hm(night.asleep_min)} ({to_local(night.start, tz):%H:%M}–{to_local(night.end, tz):%H:%M})"]
        if night.staged:
            bits.append(f"глубокий {hm(night.deep_min)}")
            bits.append(f"REM {hm(night.rem_min)}")
            if night.awake_min >= 5:
                bits.append(f"просыпались {hm(night.awake_min)}")
        if heart.avg is not None:
            bits.append(f"пульс во сне ~{heart.avg} (мин. {heart.low})")
        if quality is not None:
            bits.append(f"качество ≈{quality}")
        return " · ".join(bits)

    # --- weighings ------------------------------------------------------------------------------------------

    async def body_of(self, session: AsyncSession, weighing: Reading) -> dict[str, Any]:
        """A weighing → LMF «тело»: weight, and what the scale measured at the same moment (±15 min)."""
        weight = float(weighing.value or 0)
        body: dict[str, Any] = {"weight": round(weight, 2)}
        near = (
            await session.scalars(
                select(HealthSample)
                .where(
                    HealthSample.kind.in_(("body_fat", "body_water_mass", "height")),
                    HealthSample.start_at >= weighing.start_at - timedelta(minutes=15),
                    HealthSample.start_at <= weighing.start_at + timedelta(minutes=15),
                )
                .order_by(HealthSample.start_at)
            )
        ).all()
        # what the same scale measured (another app's body fat at that minute is not this weighing's)
        same = [r for r in near if r.kind == "height" or not weighing.source or r.source == weighing.source]
        found = {r.kind: r.value for r in same if r.value is not None}
        if found.get("body_fat") is not None:
            body["fat_pct"] = round(float(found["body_fat"]), 1)
            body["fat_kg"] = round(weight * float(found["body_fat"]) / 100, 2)
        if found.get("body_water_mass") is not None and weight:
            body["water_kg"] = round(float(found["body_water_mass"]), 2)
            body["water_pct"] = round(float(found["body_water_mass"]) / weight * 100, 1)
        height = found.get("height") or await session.scalar(
            select(HealthSample.value)
            .where(HealthSample.kind == "height", HealthSample.value.is_not(None))
            .order_by(HealthSample.start_at.desc())
            .limit(1)
        )
        if not height and self.settings.health_height_cm:
            height = self.settings.health_height_cm / 100
        if height and 1.0 < float(height) < 2.5:
            body["bmi"] = round(weight / float(height) ** 2, 1)
        raw = [f"Вес: {_n(weight)} кг"]
        if "fat_pct" in body:
            raw.append(f"Жир: {_n(body['fat_pct'])}%")
        if "water_pct" in body:
            raw.append(f"Вода: {_n(body['water_pct'])}%")
        body["raw"] = "\n".join(raw) + "\n(весы → Health Connect)"
        return body

    async def previous_weight(self, session: AsyncSession, before: datetime) -> tuple[float, datetime] | None:
        row = (
            await session.execute(
                select(HealthSample.value, HealthSample.start_at)
                .where(
                    self._scale_weights(),
                    HealthSample.start_at < before - timedelta(hours=6),
                    HealthSample.value.is_not(None),
                )
                .order_by(HealthSample.start_at.desc())
                .limit(1)
            )
        ).first()
        return (float(row[0]), row[1]) if row and row[0] is not None else None

    async def weighing_line(self, session: AsyncSession, weighing: Reading, body: dict[str, Any]) -> str:
        """«⌚ Весы: 69,8 кг · жир 14,9 % (−0,4 кг с 20.09) в 08:10» — plain text (a TLT draft header)."""
        tz = self.settings.tz
        text = f"⌚ Весы: {_n(float(body['weight']))} кг"
        if "fat_pct" in body:
            text += f" · жир {_n(body['fat_pct'])} %"
        before = await self.previous_weight(session, weighing.start_at)
        if before is not None:
            delta = float(body["weight"]) - before[0]
            sign = "+" if delta > 0 else ("−" if delta < 0 else "±")
            text += f" ({sign}{_n(abs(delta))} кг с {to_local(before[1], tz):%d.%m})"
        return f"{text} в {to_local(weighing.start_at, tz):%H:%M}"

    # --- what the user asks («пульс», «шаги», /health) and the evening line ---------------------------------

    async def last_received(self, session: AsyncSession) -> datetime | None:
        """The last request from the phone: a data payload or a test ping (pairing)."""
        last = await session.scalar(select(func.max(HealthPayload.received_at)))
        ping = await session.get(UserPreference, PING_KEY)
        pinged = datetime.fromisoformat(str(ping.value["at"])) if ping is not None and ping.value.get("at") else None
        return max((t for t in (last, pinged) if t is not None), default=None)

    async def day_text(self, session: AsyncSession, day: date, now: datetime) -> fmt.Html:
        today = self.today(now)
        last = await self.last_received(session)
        if last is None:
            return fmt.lines(
                fmt.title("⌚", "Часы и весы"),
                "С телефона пока ничего не приходило.",
                fmt.hint(
                    "Подключение: приложение Life Dashboard Companion на телефоне → отсканировать QR-код Джарвиса; "
                    "данные идут, когда телефон в домашнем Wi-Fi."
                ),
            )
        lines: list[object] = [fmt.title("⌚", "Часы и весы", human_day(day, today))]
        night, heart = await self.night(session, day)
        if night is not None:
            quality = self.night_quality(await self.quality_model(session), night)
            lines.append(fmt.line("• 🛌 Сон:", self.night_line(night, heart, quality)))
        start, end = local_day_bounds(day, self.settings.tz)
        totals = await session.get(HealthDay, day)
        if totals is not None and totals.steps is not None:
            extra = f" · {_n((totals.distance_m or 0) / 1000)} км" if totals.distance_m else ""
            lines.append(fmt.line("• 👣 Шаги:", fmt.b(_steps(totals.steps)), extra.strip() or None))
        last_hr = (
            await session.execute(
                select(HealthSample.value, HealthSample.start_at)
                .where(HealthSample.kind == "heart_rate", HealthSample.start_at >= start, HealthSample.start_at < end)
                .order_by(HealthSample.start_at.desc())
                .limit(1)
            )
        ).first()
        if last_hr is not None and last_hr[0] is not None:
            at = to_local(last_hr[1], self.settings.tz)
            lines.append(fmt.line("• ❤️ Пульс:", fmt.b(str(round(last_hr[0]))), fmt.i(f"в {at:%H:%M}")))
        for kind, label, unit in (("oxygen_saturation", "🩸 SpO₂", " %"), ("blood_pressure", "🩺 Давление", "")):
            row = await session.scalar(
                select(HealthSample)
                .where(HealthSample.kind == kind, HealthSample.start_at >= start, HealthSample.start_at < end)
                .order_by(HealthSample.start_at.desc())
                .limit(1)
            )
            if row is not None and row.value is not None:
                value = f"{round(row.value)}/{round(row.value2 or 0)}" if kind == "blood_pressure" else _n(row.value)
                lines.append(fmt.line(f"• {label}:", fmt.b(value + unit)))
        if (screen := await session.get(ScreenDay, day)) is not None and screen.minutes:
            lines.append(fmt.line("• 📱 Экран:", fmt.b(self.screen_text(screen))))
        weight = await self.previous_weight(session, end + timedelta(hours=6))
        if weight is not None:
            when = fmt.i(f"({to_local(weight[1], self.settings.tz):%d.%m})")
            lines.append(fmt.line("• ⚖️ Вес:", fmt.b(f"{_n(weight[0])} кг"), when))
        if len(lines) == 1:
            lines.append("За этот день с часов и весов ничего нет.")
        week = await self.week_line(session, day)
        if week:
            lines.append(fmt.hint(week))
        if now - last > timedelta(hours=12):
            hours = int((now - last).total_seconds() // 3600)
            lines.append(fmt.hint(f"Телефон не присылал данные {hours} ч — он в домашнем Wi-Fi?"))
        return fmt.lines(*lines)

    async def week_line(self, session: AsyncSession, day: date) -> str | None:
        """«За 7 дней: сон в среднем 7:05 · 7 900 шагов в день». The week's sleep is read at once, not night by night
        with their heart rates (L13)."""
        first, _ = self._night_bounds(day - timedelta(days=6))
        _, last = self._night_bounds(day)
        rows = (
            await session.scalars(
                select(HealthSample).where(
                    HealthSample.kind == "sleep", HealthSample.end_at >= first, HealthSample.end_at < last
                )
            )
        ).all()
        sleeps = []
        for offset in range(7):
            start, end = self._night_bounds(day - timedelta(days=offset))
            night = night_of(_sessions([r for r in rows if r.end_at is not None and start <= r.end_at < end]))
            if night is not None and night.asleep_min >= self.settings.health_min_sleep_hours * 60:
                sleeps.append(night.asleep_min)
        steps = [
            s
            for s in (
                await session.scalars(
                    select(HealthDay.steps).where(HealthDay.day > day - timedelta(days=7), HealthDay.day <= day)
                )
            ).all()
            if s
        ]
        bits = []
        if len(sleeps) >= 2:
            bits.append(f"сон в среднем {hm(round(mean(sleeps)))}")
        if len(steps) >= 2:
            bits.append(f"{_steps(round(mean(steps)))} шагов в день")
        return ("За 7 дней: " + " · ".join(bits)) if bits else None

    async def weighed_since(self, session: AsyncSession, since: datetime) -> bool:
        """A weighing from the scale (not Google Fit's profile weight) after `since`."""
        found = await session.scalar(
            select(HealthSample.id).where(self._scale_weights(), HealthSample.start_at >= since).limit(1)
        )
        return found is not None

    async def _week(self, session: AsyncSession, last: date) -> dict[str, float | None]:
        """Averages of the 7 days ending on `last`: sleep (nights ≥ the minimum), steps, resting pulse; last weight."""
        days = [last - timedelta(days=i) for i in range(7)]
        sleeps = []
        for day in days:
            night, _ = await self.night(session, day)
            if night is not None and night.asleep_min >= self.settings.health_min_sleep_hours * 60:
                sleeps.append(night.asleep_min)
        steps = [
            s
            for s in (
                await session.scalars(select(HealthDay.steps).where(HealthDay.day >= days[-1], HealthDay.day <= last))
            ).all()
            if s
        ]
        start, _ = local_day_bounds(days[-1], self.settings.tz)
        _, end = local_day_bounds(last, self.settings.tz)
        rest = [
            v
            for v in (
                await session.scalars(
                    select(HealthSample.value).where(
                        HealthSample.kind == "resting_heart_rate",
                        HealthSample.start_at >= start,
                        HealthSample.start_at < end,
                    )
                )
            ).all()
            if v
        ]
        weight = await self.previous_weight(session, end + timedelta(hours=6))
        in_week = weight is not None and weight[1] >= start
        return {
            "sleep": mean(sleeps) if sleeps else None,
            "steps": mean(steps) if steps else None,
            "rest": mean(rest) if rest else None,
            "weight": weight[0] if weight and in_week else None,
        }

    async def week_summary(self, session: AsyncSession, today: date) -> str | None:
        """Sunday morning: «📈 Неделя: сон 6:57 (−0:10) · шаги 2 400/день (+300) · вес 93 кг (+0,3) · пульс в покое 64»
        — the 7 days before today against the 7 before them."""
        now_w = await self._week(session, today - timedelta(days=1))
        was = await self._week(session, today - timedelta(days=8))
        bits = []
        if now_w["sleep"] is not None:
            delta = f" ({_signed_hm(now_w['sleep'] - was['sleep'])})" if was["sleep"] is not None else ""
            bits.append(f"сон {hm(round(now_w['sleep']))}{delta}")
        if now_w["steps"] is not None:
            delta = f" ({_signed(now_w['steps'] - was['steps'], 0)})" if was["steps"] is not None else ""
            bits.append(f"шаги {_steps(round(now_w['steps']))}/день{delta}")
        if now_w["weight"] is not None:
            delta = f" ({_signed(now_w['weight'] - was['weight'], 1)})" if was["weight"] is not None else ""
            bits.append(f"вес {_n(now_w['weight'])} кг{delta}")
        if now_w["rest"] is not None:
            bits.append(f"пульс в покое {round(now_w['rest'])}")
        return ("📈 Неделя: " + " · ".join(bits)) if bits else None

    async def evening_line(self, session: AsyncSession, now: datetime) -> fmt.Html | None:
        """The evening review: today's steps and last night, when the phone sent them."""
        day = self.today(now)
        totals = await session.get(HealthDay, day)
        night, heart = await self.night(session, day)
        bits: list[object] = []
        if totals is not None and totals.steps:
            bits.append(f"👣 {_steps(totals.steps)} шагов")
        if night is not None:
            bits.append(f"🛌 сон {hm(night.asleep_min)}")
        if heart.avg is not None:
            bits.append(f"❤️ во сне ~{heart.avg}")
        if (screen := await session.get(ScreenDay, day)) is not None and screen.minutes:
            bits.append(f"📱 экран {self.screen_text(screen)}")
        return fmt.line("⌚", fmt.line(*bits, sep=" · ")) if bits else None

    @staticmethod
    def screen_text(screen: ScreenDay) -> str:
        """«4:12 (YouTube 1:30, Telegram 0:50)»"""
        top = ", ".join(f"{a['name']} {hm(int(a['minutes']))}" for a in (screen.apps or [])[:3] if a.get("minutes"))
        return hm(screen.minutes) + (f" ({top})" if top else "")
