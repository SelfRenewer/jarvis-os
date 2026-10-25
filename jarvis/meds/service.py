"""Medication and supplement reminders («напоминай пить омегу-3 в 9:00 и 21:00»).

Jarvis only reminds about what the user set — it never suggests doses or medicines. Times are parsed by code.
A course may have an end («курсом 8 недель», «до 15 января» — a doctor's plan): no intakes after its last day, then
the course is closed with a message and can be extended («продли магний на 2 недели»).
Each due intake: a reminder with buttons (✅ принял / ⏰ через 30 мин / ✖️ пропустить), one nudge after 45 min,
«missed» after 6 h. An intake found more than 2 h late (Mac asleep) is not "take it now" — the user is asked
whether it was already taken.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.clock import local_datetime, local_day_bounds, to_local
from jarvis.core.config import Settings
from jarvis.core.dates import add_months, parse_day
from jarvis.core.numwords import spoken_numbers
from jarvis.core.text import matches_stems, normalize_title
from jarvis.db.models import Medication, MedicationIntake

REMIND_WINDOW = timedelta(hours=2)
NUDGE_AFTER = timedelta(minutes=45)
MISS_AFTER = timedelta(hours=6)
SNOOZE = timedelta(minutes=30)

_DAY_PARTS = [
    (r"утр\w*|за\s+завтраком|натощак", time(9, 0)),
    (r"(?:в\s+)?обед\w*|дн[её]м", time(13, 0)),
    (r"вечер\w*|за\s+ужином", time(20, 0)),
    (r"перед\s+сном|на\s+ночь", time(22, 30)),
]
_CLOCK = re.compile(r"(?<![\d:])(\d{1,2})(?:[:.](\d{2}))?\s*(утра|дня|вечера|ночи)?(?![\d:])")


def parse_times(text: str) -> list[time]:
    """«в 9:00 и 21:00» → [09:00, 21:00]; «утром и вечером» → [09:00, 20:00]; «в 9 вечера» → [21:00]."""
    low = text.lower().replace("ё", "е")
    found: set[time] = set()
    for match in _CLOCK.finditer(low):
        hour, minute, part = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if part in ("вечера", "дня") and 1 <= hour < 12:
            hour += 12
        if part == "ночи" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            found.add(time(hour, minute))
    rest = _CLOCK.sub(" ", low)  # «9 вечера» is 21:00, not also «вечером»
    for pattern, at in _DAY_PARTS:
        if re.search(rf"(?:^|\s|,){pattern}", rest):
            found.add(at)
    return sorted(found)


_TIME_START = re.compile(
    r"\s+(?:(?:каждый|ежедневно|по)\s|в\s+\d|к\s+\d|\d{1,2}[:.]\d{2}|утр|дн[её]м|в\s+обед|вечер|перед\s+сном|на\s+ночь"
    r"|за\s+(?:завтраком|ужином)|натощак)"
)


def split_name_and_times(text: str) -> tuple[str, str]:
    """«омегу-3 1 капсулу в 9:00 и 21:00» → («омегу-3 1 капсулу», «в 9:00 и 21:00»)."""
    match = _TIME_START.search(" " + text)
    if not match:
        return text.strip(" ,."), ""
    cut = max(0, match.start() - 1)
    return text[:cut].strip(" ,."), text[cut:].strip(" ,.")


_DOSE = re.compile(
    r"\s+((?:по\s+)?\d+(?:[.,]\d+)?\s*(?:мг|г|мкг|ме|iu|мл|капсул\w*|таблет\w*|шт\w*|капел\w*|ложк\w*))\b", re.I
)


def split_dose(name: str) -> tuple[str, str | None]:
    """«магний 400 мг» → («Магний», «400 мг»)."""
    match = _DOSE.search(name)
    base, dose = (name[: match.start()] + name[match.end() :], match.group(1)) if match else (name, None)
    base = re.sub(r"\s+", " ", base).strip(" ,.")
    first, _, tail = base.partition(" ")
    stem, _, suffix = first.partition("-")
    # «пить омегу-3 / таблетку / магнезию» → «Омега-3 / Таблетка / Магнезия» (feminine accusative → nominative)
    if re.search(r"[бвгджзклмнпрстфхцчшщ]у$", stem):
        stem = stem[:-1] + "а"
    elif stem.endswith("ию"):
        stem = stem[:-1] + "я"
    first = stem + ("-" + suffix if suffix else "")
    base = f"{first} {tail}".strip()
    return (base[:1].upper() + base[1:]), dose


# «курсом 8 недель», «на 2 недели», «в течение месяца», «10 дней»; «до 15.01» / «до 15 января».
_COURSE_HINT = re.compile(r"курс|недел|месяц|\bд(?:ень|ня|ней)\b|\bсут(?:ок|ки)\b|\bдо\s+\d", re.I)
_COURSE = re.compile(
    r"(?:^|(?<=[\s,]))(?P<pre>курс(?:ом)?(?:\s+на)?|на|в\s+течение|ещ[её]\s+на)?\s*(?P<n>\d+(?:[.,]\d+)?)?\s*"
    r"(?P<unit>дн(?:ей|я)|день|суток|сутки|недел[юиья]|неделя|месяц(?:а|ев)?)(?=$|[\s,.;!])",
    re.I,
)
_UNTIL = re.compile(
    r"(?:^|(?<=[\s,]))(?:курс(?:ом)?\s+)?до\s+(?P<when>\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|\d{1,2}\s+[а-яё]+)"
    r"(?=$|[\s,.;!])",
    re.I,
)
MAX_COURSE_DAYS = 3 * 365


@dataclass
class Course:
    """The end of a course said with the medication: its last day (inclusive) and the words for the reply."""

    ends_on: date
    said: str


def course_end(n: float, unit: str, start: date) -> date:
    """N days / weeks / months counted from `start` (day 1) → the last day of the course."""
    unit = unit.lower()
    if unit.startswith("месяц") and float(n).is_integer():
        return add_months(start, int(n)) - timedelta(days=1)
    days = n * (30 if unit.startswith("месяц") else 7 if unit.startswith("недел") else 1)
    return start + timedelta(days=max(1, round(days)) - 1)


def parse_course(text: str, today: date) -> tuple[str, Course | None, bool]:
    """Cut the course out of a medication phrase: («магний 400 мг в 21:00», Course(…), ok).
    `ok` is False when an end was said but cannot be used (a past date, longer than 3 years) — the bot asks again."""
    if not _COURSE_HINT.search(text):
        return text, None, True
    said, text = text, spoken_numbers(text)
    for m in _UNTIL.finditer(text):
        end = parse_day(m["when"], today)
        if end is None:
            continue
        rest = re.sub(r"\s+", " ", text[: m.start()] + " " + text[m.end() :]).strip(" ,.")
        if end < today or (end - today).days > MAX_COURSE_DAYS:
            return rest, None, False
        return rest, Course(end, f"до {end:%d.%m.%Y}"), True
    for m in _COURSE.finditer(text):
        if not m["pre"] and not m["n"]:
            continue  # «2 раза в день»: a day without a number is not a course
        n = float(m["n"].replace(",", ".")) if m["n"] else 1.0
        rest = re.sub(r"\s+", " ", text[: m.start()] + " " + text[m.end() :]).strip(" ,.")
        rest = re.sub(r"\s+курс(?:ом)?$", "", rest)
        if n <= 0:
            return rest, None, False
        end = course_end(n, m["unit"], today)
        if (end - today).days > MAX_COURSE_DAYS:
            return rest, None, False
        return rest, Course(end, duration_text(n, m["unit"])), True
    return re.sub(r"\s+", " ", re.sub(r"(?i)\bкурс(?:ом)?\b", " ", said)).strip(" ,."), None, True


def duration_text(n: float, unit: str) -> str:
    """(8, «недель») → «8 недель»; (1, «месяца») → «1 месяц»; (1.5, «месяца») → «1,5 месяца»."""
    forms = {"д": ("день", "дня", "дней"), "с": ("день", "дня", "дней"), "н": ("неделя", "недели", "недель")}
    one, few, many = forms.get(unit.lower()[0], ("месяц", "месяца", "месяцев"))
    if not float(n).is_integer():
        return f"{str(n).replace('.', ',')} {few}"
    count = int(n)
    tail = count % 100
    form = many if 11 <= tail <= 14 else one if count % 10 == 1 else few if 2 <= count % 10 <= 4 else many
    return f"{count} {form}"


# A pack = several supplements in one reminder («Утренние: D3, B12, цинк»). Name → default time.
PACKS = [
    (r"утр\w*", "Утренние", time(9, 0), "🌅"),
    (r"вечер\w*", "Вечерние", time(22, 30), "🌙"),
    (r"ночн\w*|на\s+ночь|перед\s+сном", "На ночь", time(22, 30), "🌙"),
    (r"днев\w*|обед\w*|дн[её]м|днем", "Дневные", time(13, 0), "☀️"),
]


def pack_spec(said: str) -> tuple[str, time] | None:
    """«утренние» / «на утро» / «утром» → («Утренние», 09:00)."""
    for pattern, name, at, _ in PACKS:
        if re.fullmatch(rf"(?:на\s+)?(?:{pattern})", said.strip().lower()):
            return name, at
    return None


def pack_emoji(name: str) -> str:
    for pattern, _, _, emoji in PACKS:
        if re.match(pattern, name.lower()):
            return emoji
    return "💊"


_PACK_WORD = r"утренн\w*|вечерн\w*|ночн\w*|дневн\w*"
_PACK_NOUN = r"(?:таблет\w*|добав\w*|бад\w*|витамин\w*|набор\w*|при[её]м\w*|пилюл\w*)"
_PACK_SET = re.compile(
    rf"^(?P<p>{_PACK_WORD})\s+{_PACK_NOUN}\s*(?P<rest>.*)$"
    rf"|^{_PACK_NOUN}\s+(?P<p2>на\s+(?:утро|вечер|ночь|день))\s*(?P<rest2>.*)$"
    r"|^(?P<p3>утром|вечером|на\s+ночь|перед\s+сном|дн[её]м)\s+(?:пью|принимаю)\s*(?P<rest3>.*)$",
    re.IGNORECASE | re.S,
)
_PACK_ADD = re.compile(
    rf"^(?:добавь|добавить|включи)\s+в\s+(?P<p>{_PACK_WORD})(?:\s+{_PACK_NOUN})?\s+(?P<rest>.+)$", re.IGNORECASE
)
_PACK_REMOVE = re.compile(
    rf"^(?:убери|удали|исключи)\s+из\s+(?P<p>{_PACK_WORD})(?:\s+{_PACK_NOUN})?\s+(?P<rest>.+)$", re.IGNORECASE
)


@dataclass
class PackCommand:
    action: str  # set | add | remove | time
    name: str  # «Утренние»
    default_at: time
    items: str
    times: list[time]


def parse_pack_command(text: str) -> PackCommand | None:
    """«утренние таблетки: D3 6000, B12 500 в 9:00» · «добавь в утренние омегу» · «убери из утренних цинк» ·
    «утренние таблетки в 8:30»."""
    t = text.strip().rstrip(".!")
    for pattern, action in ((_PACK_ADD, "add"), (_PACK_REMOVE, "remove")):
        if m := pattern.match(t):
            spec = pack_spec(m.group("p"))
            if spec is None:
                return None
            return PackCommand(action, spec[0], spec[1], m.group("rest").strip(" :—-"), [])
    m = _PACK_SET.match(t)
    if not m:
        return None
    spec = pack_spec(m.group("p") or m.group("p2") or m.group("p3"))
    if spec is None:
        return None
    rest = (m.group("rest") or m.group("rest2") or m.group("rest3") or "").strip(" :—-")
    items, when = split_name_and_times(" " + rest) if rest else ("", "")
    times = parse_times(when) if when else []
    if not items:
        return PackCommand("time" if times else "show", spec[0], spec[1], "", times)
    return PackCommand("set", spec[0], spec[1], items.strip(" :—-"), times)


def is_pack(med: Medication) -> bool:
    items = med.items or []
    return len(items) > 1 or (len(items) == 1 and normalize_title(items[0].get("label")) != normalize_title(med.name))


def item_label(item: dict[str, Any]) -> str:
    """«Витамин D3 6000 МЕ», «Омега-3 (2 шт)»."""
    dose = item.get("dose")
    text = str(item.get("label") or item.get("said") or "?")
    if dose is not None:
        number = f"{float(dose):g}".replace(".", ",")
        text += f" {number}" + (f" {item['unit']}" if item.get("unit") else "")
    if item.get("dose_text"):
        text += f" ({item['dose_text']})"
    return text


def items_text(med: Medication, indexes: list[int] | None = None) -> str:
    items = med.items or []
    chosen = items if indexes is None else [items[i] for i in indexes if 0 <= i < len(items)]
    return " · ".join(item_label(i) for i in chosen)


@dataclass
class DueAction:
    kind: str  # remind | nudge | late
    medication: Medication
    intake: MedicationIntake


class MedService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def active(self, session: AsyncSession) -> Sequence[Medication]:
        return (
            await session.scalars(select(Medication).where(Medication.active.is_(True)).order_by(Medication.name))
        ).all()

    async def find(
        self, session: AsyncSession, name: str | None, *, ended_since: date | None = None
    ) -> list[Medication]:
        """By name (exact, then 5- and 4-letter stems). `ended_since`: also courses that ended on or after that day
        (to extend them)."""
        if not name or not normalize_title(name):
            return []
        meds = list(await self.active(session))
        if ended_since is not None:
            meds += (
                await session.scalars(
                    select(Medication)
                    .where(Medication.active.is_(False), Medication.ends_on >= ended_since)
                    .order_by(Medication.ends_on.desc())
                )
            ).all()
        wanted = normalize_title(name)
        exact = [m for m in meds if normalize_title(m.name) == wanted]
        if exact:
            return exact
        for length in (5, 4):
            found = [m for m in meds if matches_stems(name, m.name, length)]
            if found:
                return found
        return []

    async def add(
        self,
        session: AsyncSession,
        name: str,
        dose: str | None,
        times: list[time],
        ends_on: date | None = None,
        now: datetime | None = None,
    ) -> tuple[Medication, bool]:
        """A new course, or new times for an existing one with the same name (no duplicates). An end said again
        replaces the old one; said without an end, the course keeps it."""
        stamps = sorted({t.strftime("%H:%M") for t in times})
        existing = [m for m in await self.active(session) if normalize_title(m.name) == normalize_title(name)]
        if existing:
            med = existing[0]
            med.times = stamps
            med.dose = dose or med.dose
            med.ends_on = ends_on or med.ends_on
            return med, False
        med = Medication(name=name, dose=dose, times=stamps, active=True, ends_on=ends_on)
        if now is not None:  # by Jarvis's clock: no intakes are made for days before the schedule began
            med.created_at = now
        session.add(med)
        await session.flush()
        return med, True

    async def set_pack(
        self,
        session: AsyncSession,
        name: str,
        items: list[dict[str, Any]],
        times: list[time],
        now: datetime | None = None,
    ) -> tuple[Medication, bool]:
        """«Утренние таблетки: D3 6000, B12 500 в 9:00» — the whole list is replaced (said again = updated)."""
        med, created = await self.add(session, name, None, times, now=now)
        med.items = list(items)
        return med, created

    async def edit_pack(
        self,
        session: AsyncSession,
        med: Medication,
        *,
        add: list[dict[str, Any]] | None = None,
        remove: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Add items / remove items named by words. Returns (added labels, removed labels)."""
        items = list(med.items or [])
        added, removed = [], []
        for item in add or []:
            same = [
                i for i, x in enumerate(items) if x.get("supplement") and x.get("supplement") == item.get("supplement")
            ]
            if same:
                items[same[0]] = item
            else:
                items.append(item)
            added.append(item_label(item))
        for said in remove or []:
            keep = []
            for item in items:
                names = " ".join(str(item.get(k) or "") for k in ("label", "said", "supplement"))
                if matches_stems(said, names, 4) or matches_stems(said, names, 3):
                    removed.append(item_label(item))
                else:
                    keep.append(item)
            items = keep
        med.items = items
        return added, removed

    async def close_ended(self, session: AsyncSession, now: datetime) -> list[Medication]:
        """Courses whose last day has passed → closed (not active). Returned once: the caller says so."""
        today = to_local(now, self.settings.tz).date()
        ended = [m for m in await self.active(session) if m.ends_on is not None and m.ends_on < today]
        for med in ended:
            med.active = False
        return ended

    def extend(self, med: Medication, n: float, unit: str, now: datetime) -> date:
        """«продли магний на 2 недели»: from the current end (or from today, when it has passed); reopens it."""
        today = to_local(now, self.settings.tz).date()
        start = max(med.ends_on + timedelta(days=1), today) if med.ends_on else today
        med.ends_on = course_end(n, unit, start)
        med.active = True
        return med.ends_on

    async def remove(self, session: AsyncSession, medication_id: uuid.UUID) -> Medication | None:
        med = await session.get(Medication, medication_id)
        if med is None or not med.active:
            return None
        med.active = False
        await session.execute(
            update(MedicationIntake)
            .where(MedicationIntake.medication_id == med.id, MedicationIntake.status == "pending")
            .values(status="skipped")
        )
        return med

    def due_times(self, med: Medication, day: date) -> list[datetime]:
        return [local_datetime(day, time.fromisoformat(t), self.settings.tz) for t in med.times]

    async def _intake(self, session: AsyncSession, med: Medication, due_at: datetime) -> MedicationIntake:
        await session.execute(
            insert(MedicationIntake)
            .values(id=uuid.uuid4(), medication_id=med.id, due_at=due_at, status="pending")
            .on_conflict_do_nothing(index_elements=[MedicationIntake.medication_id, MedicationIntake.due_at])
        )
        intake = await session.scalar(
            select(MedicationIntake)
            .where(MedicationIntake.medication_id == med.id, MedicationIntake.due_at == due_at)
            .with_for_update()
        )
        assert intake is not None
        return intake

    async def tick(self, session: AsyncSession, now: datetime) -> list[DueAction]:
        """What to send now. Marks reminders as sent in the same transaction (the caller sends afterwards).

        A dose more than `MISS_AFTER` late is marked missed without a message — also after the Mac slept through
        it (R37: those used to stay pending forever). No intakes are made for days before a schedule began."""
        today = to_local(now, self.settings.tz).date()
        since = local_datetime(today - timedelta(days=1), time(0, 0), self.settings.tz)
        await session.execute(
            update(MedicationIntake)
            .where(MedicationIntake.status == "pending", MedicationIntake.due_at < now - MISS_AFTER)
            .values(status="missed")
        )
        meds = await self.active(session)
        known = {
            (i.medication_id, i.due_at): i
            for i in (
                await session.scalars(
                    select(MedicationIntake)
                    .where(MedicationIntake.medication_id.in_([m.id for m in meds]), MedicationIntake.due_at >= since)
                    .with_for_update()
                )
            ).all()
        } if meds else {}  # fmt: skip
        actions: list[DueAction] = []
        for med in meds:
            began = to_local(med.created_at, self.settings.tz).date() if med.created_at <= now else None
            for day in (today - timedelta(days=1), today):
                if (med.ends_on is not None and day > med.ends_on) or (began is not None and day < began):
                    continue
                for due_at in self.due_times(med, day):
                    if due_at > now:
                        continue
                    late = now - due_at
                    intake = known.get((med.id, due_at))
                    if intake is None:
                        intake = await self._intake(session, med, due_at)
                    if intake.status != "pending":
                        continue
                    if late > MISS_AFTER:
                        intake.status = "missed"
                    elif intake.reminded_at is None:
                        intake.reminded_at = now
                        actions.append(DueAction("remind" if late <= REMIND_WINDOW else "late", med, intake))
                    elif intake.snoozed_until is not None and now >= intake.snoozed_until:
                        intake.snoozed_until = None
                        intake.nudged_at = now
                        actions.append(DueAction("remind", med, intake))
                    elif (
                        intake.snoozed_until is None
                        and intake.nudged_at is None
                        and now - intake.reminded_at >= NUDGE_AFTER
                    ):
                        intake.nudged_at = now
                        actions.append(DueAction("nudge", med, intake))
        return actions

    async def answer(
        self, session: AsyncSession, intake_id: uuid.UUID, choice: str, now: datetime, items: list[int] | None = None
    ) -> tuple[MedicationIntake, Medication] | None:
        """taken / skipped / snooze. Only a pending intake changes (double taps are no-ops).
        `items`: «☑ Не всё» — the indexes of the pack items taken."""
        intake = await session.get(MedicationIntake, intake_id, with_for_update=True)
        if intake is None or intake.status != "pending":
            return None
        if choice == "snooze":
            intake.snoozed_until = now + SNOOZE
        else:
            intake.status = choice
            intake.answered_at = now
            if choice == "taken":  # «✅ Выпил всё» after ticking a few in the picker still means everything
                intake.items_taken = sorted(set(items)) if items is not None else None
        med = await session.get(Medication, intake.medication_id)
        assert med is not None
        return intake, med

    async def mark_taken_now(
        self, session: AsyncSession, med: Medication, now: datetime, items: list[int] | None = None
    ) -> MedicationIntake | None:
        """«Выпил омегу»: the closest pending intake of today (due within the next hour or earlier) — at 20:30 the
        21:00 dose, not a forgotten 09:00 one."""
        due = [d for d in self.due_times(med, to_local(now, self.settings.tz).date()) if d <= now + timedelta(hours=1)]
        for due_at in sorted(due, key=lambda d: abs(d - now)):
            intake = await self._intake(session, med, due_at)
            if intake.status == "pending":
                intake.status = "taken"
                intake.answered_at = now
                intake.items_taken = sorted(set(items)) if items is not None else None
                return intake
        return None

    async def toggle_item(self, session: AsyncSession, intake_id: uuid.UUID, index: int) -> MedicationIntake | None:
        """«☑ Не всё» picker: tick / untick one item of a pending intake."""
        intake = await session.get(MedicationIntake, intake_id, with_for_update=True)
        if intake is None or intake.status != "pending":
            return None
        chosen = set(intake.items_taken or [])
        chosen.symmetric_difference_update({index})
        intake.items_taken = sorted(chosen)
        return intake

    async def today(self, session: AsyncSession, now: datetime) -> list[tuple[Medication, datetime, str]]:
        """(medication, due time, status) for today; future ones are «planned»."""
        day = to_local(now, self.settings.tz).date()
        start, end = local_day_bounds(day, self.settings.tz)
        intakes = {
            (i.medication_id, i.due_at): i.status
            for i in (
                await session.scalars(
                    select(MedicationIntake).where(MedicationIntake.due_at >= start, MedicationIntake.due_at < end)
                )
            ).all()
        }
        plan = []
        for med in await self.active(session):
            if med.ends_on is not None and day > med.ends_on:
                continue
            for due_at in self.due_times(med, day):
                status = intakes.get((med.id, due_at)) or ("planned" if due_at > now else "pending")
                plan.append((med, due_at, status))
        return sorted(plan, key=lambda item: item[1])

    async def adherence(self, session: AsyncSession, med: Medication, now: datetime, days: int = 7) -> tuple[int, int]:
        """(taken, due) over the last `days` days (only intakes that were due)."""
        rows = (
            await session.scalars(
                select(MedicationIntake.status).where(
                    MedicationIntake.medication_id == med.id,
                    MedicationIntake.due_at > now - timedelta(days=days),
                    MedicationIntake.due_at <= now,
                )
            )
        ).all()
        return sum(1 for status in rows if status == "taken"), len(rows)
