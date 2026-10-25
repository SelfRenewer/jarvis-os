"""TLT / TPL tracker agent: a report → a draft → «✅ Записать» → LMF; day, period and TPL summaries; reminders.

Values come from code (`parse.py`); tags and TPL items are resolved here against the LMF dictionary by code or by
label words. The LLM is asked only for free speech the code did not understand, and only picks codes / item names
from closed lists (never ids, never numbers it was not told). Nothing is written before the user confirms.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.bot import fmt
from jarvis.core.clock import parse_hhmm, to_local
from jarvis.core.config import Settings
from jarvis.core.dates import WEEKDAY_SHORT, human_day, parse_day
from jarvis.core.errors import LLMOutputError, NotConfiguredError, TransientError
from jarvis.integrations.llm.client import LLMClient
from jarvis.integrations.lmf.client import FoodNotSupportedError, LifeTrackerApi
from jarvis.photos.reader import PhotoKind, PhotoReading
from jarvis.tracker.food import (
    FOOD_SYSTEM,
    MACROS,
    EatenItem,
    FoodEstimate,
    FoodEstimateItem,
    eaten_amount,
    find_product,
    kbju,
    meal_of,
    parse_food_remove,
    parse_nutrition,
    pick_entry,
    plausible,
    product_key,
    split_food,
)
from jarvis.tracker.intake import Intake, Kind, split_intake
from jarvis.tracker.parse import GLUE, parse_day_report, parse_month, parse_sleep, parse_tpl_marks, same_word
from jarvis.tracker.supplements import Supplement, Taken, find_supplement, resolve, split_items, supplements_from

log = logging.getLogger(__name__)

DICT_TTL = 600.0
MOODS = {"good": "🟢 хорошее", "neutral": "🟡 нейтральное", "bad": "🔴 плохое"}
# «заряд» and «работа» are no longer recorded (2026-10-14: never used); old values are still shown and can be cleared.
FIELD_NAMES = {
    "sleep_minutes": "сон",
    "sleep_quality": "качество сна",
    "score": "оценка",
    "mood": "настроение",
    "charge": "заряд",
    "work_hours": "работа",
    "note": "заметка",
}
# «убери оценку за вчера» → which fields to clear
_CLEARABLE = [
    ("оценк", ("score",)),
    ("сон", ("sleep_minutes", "sleep_quality")),
    ("качеств", ("sleep_quality",)),
    ("настроени", ("mood",)),
    ("заряд", ("charge",)),
    ("заметк", ("note",)),
]
TRACKER_SYSTEM = (
    "You extract a daily self-tracking report (Russian) for the user's TLT tracker. Use ONLY codes listed in <tags> "
    "(copy the code exactly) and items listed in <tpl> (copy 'Сфера: пункт' exactly). Copy day_text and sleep_text "
    "verbatim from the message; never invent values that were not said, leave a field null when it is absent. "
    "tags = activities, habits and states the user reports for that day (a workout, a cold shower, a walk, tiredness, "
    "routines…); remove_tags only when the user asks to remove/unmark something. tpl_items only when the user talks "
    "about the 12 spheres / TPL. note only when the user explicitly asks to add a note to the day."
)


class TrackerExtraction(BaseModel):
    day_text: str | None = Field(default=None, description="Day phrase as said: 'вчера', '12.10'; null = today")
    sleep_text: str | None = Field(default=None, description="Sleep duration as said: '7:30', '7 с половиной часов'")
    sleep_quality: int | None = Field(default=None, ge=0, le=100)
    score: float | None = Field(default=None, ge=0, le=10, description="Day score 0..10 if said")
    mood: Literal["good", "neutral", "bad"] | None = None
    tags: list[str] = Field(default_factory=list, description="Codes from <tags> the user reports")
    remove_tags: list[str] = Field(default_factory=list, description="Codes from <tags> to remove")
    tpl_items: list[str] = Field(default_factory=list, description="'Сфера: пункт' from <tpl> to check")
    note: str | None = Field(default=None, description="Only an explicitly requested note for the day")


class TrackerDraft(BaseModel):
    """What will be written after «✅ Записать» (stored in the approval payload)."""

    day: str | None = None  # ISO date of the TLT day
    fields: dict[str, Any] = Field(default_factory=dict)  # None = clear the field
    note: str | None = None  # appended to the day's note
    tags_add: list[int] = Field(default_factory=list)
    tags_remove: list[int] = Field(default_factory=list)
    # [{"id", "dose", "dose_text", "label"}] — doses are ADDED to what the day already has (two energy drinks)
    supplements_add: list[dict[str, Any]] = Field(default_factory=list)
    supplements_remove: list[int] = Field(default_factory=list)
    # [{"name", "grams", "kcal", "protein", "fat", "carbs", "product_id", "estimate", "new_product", "meal", "time"}]
    food_add: list[dict[str, Any]] = Field(default_factory=list)
    # [{"id", "date", "name", "grams", "kcal", …, "time", "created_at"}] — re-checked in LMF before deleting
    food_remove: list[dict[str, Any]] = Field(default_factory=list)
    products_save: list[dict[str, Any]] = Field(default_factory=list)  # a photographed label → the directory
    header: list[str] = Field(default_factory=list)  # shown first in the confirmation (what was read on a photo)
    ref: str = Field(default_factory=lambda: uuid.uuid4().hex)  # LMF ext_id prefix: a retried write is a no-op
    tpl_period: str | None = None
    tpl_check: list[int] = Field(default_factory=list)
    tpl_uncheck: list[int] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)  # shown, never written
    # LMF «тело» of the day (weight, fat_pct, fat_kg, water_kg, bmi, raw …): laid over what the day already has
    body: dict[str, Any] = Field(default_factory=dict)

    def has_day_changes(self) -> bool:
        return bool(
            self.fields
            or self.note
            or self.tags_add
            or self.tags_remove
            or self.supplements_add
            or self.supplements_remove
            or self.body
        )

    def has_tpl_changes(self) -> bool:
        return bool(self.tpl_check or self.tpl_uncheck)

    def empty(self) -> bool:
        return not (
            self.has_day_changes() or self.has_tpl_changes() or self.food_add or self.food_remove or self.products_save
        )

    def only_removal(self) -> bool:
        return bool(self.food_remove) and not (self.has_day_changes() or self.has_tpl_changes() or self.food_add)


@dataclass(frozen=True)
class Tag:
    id: int
    code: str
    label: str
    group: str
    uses: int

    @property
    def words(self) -> list[str]:
        """Russian words of the label («Ice Shower — холодный душ» → холодный, душ)."""
        part = self.label.split("—")[-1]
        return [w for w in re.findall(r"[а-яё]+", part.lower().replace("ё", "е")) if len(w) >= 3]

    @property
    def short(self) -> str:
        part = self.label.split("—")[-1].strip()
        return f"{self.code} ({part})" if part and part != self.code else self.code


@dataclass(frozen=True)
class TplItem:
    id: int
    name: str
    category: str
    category_idx: int

    @property
    def title(self) -> str:
        return f"{self.category} · {self.name}"

    @property
    def words(self) -> list[str]:
        return [w for w in re.findall(r"[a-zа-яё+]+", self.name.lower().replace("ё", "е")) if len(w) >= 2]


@dataclass
class Dictionary:
    tags: list[Tag]
    items: list[TplItem]
    categories: list[dict[str, Any]]
    supplements: dict[int, dict[str, Any]]
    supplement_list: list[Supplement] = field(default_factory=list)

    def supplement(self, supplement_id: int) -> Supplement | None:
        return next((x for x in self.supplement_list if x.id == supplement_id), None)

    def tag(self, code: str) -> Tag | None:
        found = [t for t in self.tags if t.code.lower() == code.lower()]
        exact = [t for t in found if t.code == code]
        pool = exact or found
        return max(pool, key=lambda t: t.uses) if pool else None  # «FC» is in two groups: the one in use

    def tag_by_id(self, tag_id: int) -> Tag | None:
        return next((t for t in self.tags if t.id == tag_id), None)

    def item(self, item_id: int) -> TplItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def match_labels(self, words: list[str]) -> tuple[list[Tag], list[str]]:
        """Tags whose label words are all in the text (the longest labels first); the unused words are returned."""
        left = list(words)
        found: list[Tag] = []
        for tag in sorted(self.tags, key=lambda t: (-len(t.words), -t.uses)):
            if not tag.words or any(t.code == tag.code for t in found):
                continue
            used: list[int] = []
            for word in tag.words:
                index = next((i for i, w in enumerate(left) if i not in used and same_word(w, word)), None)
                if index is None:
                    break
                used.append(index)
            else:
                found.append(tag)
                left = [w for i, w in enumerate(left) if i not in used]
        return found, left

    def tpl_candidates(self, phrase: str) -> list[TplItem]:
        """Items named by a phrase («утреннюю рутину», «медитацию в отдыхе»): full name matches first."""
        words = [w for w in re.findall(r"[a-zа-яё+]+", phrase.lower().replace("ё", "е")) if len(w) >= 2]
        words = [w for w in words if w not in GLUE or w in ("сон",)]
        if not words:
            return []
        pool = self.items
        categories = {c["name"] for c in self.categories}
        named = [c for c in categories if any(same_word(w, c.lower()) for w in words)]
        rest = [w for w in words if not any(same_word(w, c.lower()) for c in named)]
        if named and rest:
            pool = [i for i in self.items if i.category in named]
            words = rest

        def covers(item: TplItem) -> bool:
            return all(any(same_word(w, iw) for iw in item.words) for w in words)

        full = [i for i in pool if covers(i) and all(any(same_word(iw, w) for w in words) for iw in i.words)]
        return full or [i for i in pool if covers(i)]


_SHOW_NOISE = re.compile(
    r"^(?:что|покажи|показать|как|сколько|какая|какой|какие|средн\w*)\b|\b(?:tlt|тлт|тилт|tilt|трекер\w*|дневник\w*)\b"
    r"|\b(?:в|я|у\s+меня|меня|спал\w*|сон|сна|оценк\w*|настроени\w*|было|записано|там|мой|мои)\b|[?!]|\.(?!\d)",
    re.IGNORECASE,
)


def parse_past(text: str, today: date) -> tuple[date, date] | None:
    """A day or a span in the past for summaries: «вчера», «неделю», «этот месяц», «сентябрь», «с 01.10 по 10.10»."""
    value = re.sub(r"\s+", " ", _SHOW_NOISE.sub(" ", text.lower().replace("ё", "е"))).strip()
    value = re.sub(r"^(?:за|на|в)\s+", "", value).strip()
    if not value:
        return None
    monday = today - timedelta(days=today.weekday())
    if re.fullmatch(r"(?:эт\w+|текущ\w+)\s+недел\w*", value):
        return monday, today
    if re.fullmatch(r"(?:последн\w+\s+)?(?:недел\w*|7 дней|семь дней)", value):  # «как я спал на неделе»
        return today - timedelta(days=6), today
    if re.fullmatch(r"прошл\w+\s+недел\w*", value):
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if re.fullmatch(r"(?:эт\w+|текущ\w+)\s+месяц\w*", value):
        return today.replace(day=1), today
    if re.fullmatch(r"(?:последн\w+\s+)?(?:месяц|30 дней|тридцать дней)", value):
        return today - timedelta(days=29), today
    if re.fullmatch(r"прошл\w+\s+месяц\w*", value):
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if m := re.fullmatch(r"(?:последн\w+\s+)?(\d{1,3})\s+(?:дн\w+|день)", value):
        return today - timedelta(days=int(m.group(1)) - 1), today
    period, month = parse_month(f"за {value}", today.year, today.month)
    if month is not None:
        start = date(int(period[:4]), int(period[5:7]), 1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return start, min(end, today)
    if m := re.fullmatch(r"(?:с|от)\s+(.+?)\s+(?:по|до)\s+(.+)", value):
        start_day, end_day = parse_day(m.group(1), today), parse_day(m.group(2), today)
        if start_day and end_day and not _YEAR.search(m.group(1)):
            # A date without a year is read forward (for tasks); a span asked about is in the past (L8, review
            # 17.10): «с 25.12 по 01.01» on 2 January is last December, not the coming one.
            if start_day > end_day or start_day > today:
                start_day = _year_back(start_day)
            if end_day > today and not _YEAR.search(m.group(2)) and _year_back(end_day) >= start_day:
                end_day = _year_back(end_day)
        if start_day and end_day and start_day <= end_day:
            return start_day, min(end_day, today)
    day = parse_day(value, today)
    if day is not None and day > today and not re.search(r"\d", value):
        day -= timedelta(days=7)
    elif day is not None and day > today and not _YEAR.search(value):
        day = _year_back(day)  # «25.12» asked on 2 January
    if day is not None and day <= today:
        return day, day
    return None


_YEAR = re.compile(r"\d{4}")


def _year_back(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)


BODY_FIELDS = (
    "weight", "fat_pct", "fat_kg", "muscle_kg", "muscle_clean_kg", "bone_muscle_kg",
    "water_kg", "water_pct", "bmi", "raw", "waist_cm",
)  # fmt: skip


def body_text(body: dict[str, Any]) -> str:
    """{"weight": 69.8, "fat_pct": 14.9} → «69,8 кг · жир 14,9 %»."""
    bits = []
    if body.get("weight") is not None:
        bits.append(f"{_num(round(float(body['weight']), 1))} кг")
    if body.get("fat_pct") is not None:
        bits.append(f"жир {_num(round(float(body['fat_pct']), 1))} %")
    if body.get("water_pct") is not None:
        bits.append(f"вода {_num(round(float(body['water_pct']), 1))} %")
    if body.get("bmi") is not None:
        bits.append(f"ИМТ {_num(round(float(body['bmi']), 1))}")
    if body.get("waist_cm") is not None:
        bits.append(f"талия {_num(round(float(body['waist_cm']), 1))} см")
    return " · ".join(bits) or "—"


def supplement_entry(taken: Taken) -> dict[str, Any]:
    assert taken.supplement is not None
    label = taken.supplement.title + (f" ({taken.portion})" if taken.portion else "")
    return {"id": taken.supplement.id, "dose": taken.dose, "dose_text": taken.dose_text, "label": label}


def supplement_text(entry: dict[str, Any], d: Dictionary) -> str:
    supplement = d.supplement(int(entry["id"]))
    unit = f" {supplement.unit}" if supplement and supplement.unit and entry.get("dose") is not None else ""
    dose = f" {_num(entry['dose'])}{unit}" if entry.get("dose") is not None else ""
    extra = f" ({entry['dose_text']})" if entry.get("dose_text") else ""
    return f"{entry.get('label') or (supplement.title if supplement else '?')}{dose}{extra}"


def merge_supplements(
    current: list[dict[str, Any]],
    add: list[dict[str, Any]],
    remove: list[int],
    since: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """LMF replaces the whole list: keep what is there, ADD doses (a second coffee), drop the removed ones.

    On a retry `current` is the list as it was before the first attempt (so a write whose reply was lost is not
    added twice) and `since` the day as re-read now: rows the user added there in between are kept."""
    rows = {int(x["supplement_id"]): dict(x) for x in current}
    for entry in add:
        sid = int(entry["id"])
        row = rows.get(sid)
        if row is None:
            rows[sid] = {"supplement_id": sid, "dose": entry.get("dose"), "dose_text": entry.get("dose_text")}
            continue
        if entry.get("dose") is not None:
            row["dose"] = round(float(row.get("dose") or 0) + float(entry["dose"]), 6)
        if entry.get("dose_text") and not row.get("dose_text"):
            row["dose_text"] = entry["dose_text"]
    for sid in remove:
        rows.pop(int(sid), None)
    touched = {int(e["id"]) for e in add} | {int(sid) for sid in remove}
    for x in since or []:
        if int(x["supplement_id"]) not in touched:
            rows.setdefault(int(x["supplement_id"]), dict(x))
    return [
        {"supplement_id": r["supplement_id"], "dose": r.get("dose"), "dose_text": r.get("dose_text")}
        for r in rows.values()
    ]


def hm(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    return f"{minutes // 60}:{minutes % 60:02d}"


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace(".", ",")


MOOD_WORDS = {"good": "хорошее", "neutral": "нейтральное", "bad": "плохое"}


def day_rows(
    data: dict[str, Any], dictionary: Dictionary | None, food: dict[str, Any] | None = None
) -> tuple[list[fmt.Html], list[str]]:
    """What a TLT day has, one line per kind («🛌 Сон: <b>7:30</b> · качество 85»), and what it lacks."""
    rows: list[fmt.Html] = []
    gaps: list[str] = []
    if data.get("sleep_minutes") is not None:
        quality = data.get("sleep_quality")
        rows.append(
            fmt.line("🛌", fmt.kv("Сон", hm(data["sleep_minutes"]), f"качество {quality}" if quality else None))
        )
    else:
        gaps.append("сон")
    if data.get("score") is not None:
        rows.append(fmt.line("⭐", fmt.kv("Оценка", _num(data["score"]))))
    else:
        gaps.append("оценка")
    if data.get("mood"):
        mood = str(data["mood"])
        rows.append(fmt.line(MOODS.get(mood, "🙂 ").split()[0], fmt.kv("Настроение", MOOD_WORDS.get(mood, mood))))
    else:
        gaps.append("настроение")
    tags = [dictionary.tag_by_id(int(x)) for x in data.get("tags") or []] if dictionary else []
    if any(tags):
        rows.append(fmt.line("🏷", ", ".join(x.short for x in tags if x)))
    else:
        gaps.append("отметки")
    supps = []
    for item in data.get("supplements") or []:
        info = (dictionary.supplements.get(int(item.get("supplement_id") or 0)) if dictionary else None) or {}
        dose = f" {_num(item['dose'])} {info.get('unit') or ''}".rstrip() if item.get("dose") is not None else ""
        supps.append(f"{info.get('label') or info.get('name') or '?'}{dose}")
    if supps:
        rows.append(fmt.line("💊", " · ".join(supps)))
    if data.get("charge"):
        rows.append(fmt.line("🤸", fmt.kv("Заряд", data["charge"])))
    if data.get("work_hours") is not None:
        rows.append(fmt.line("💼", fmt.kv("Работа", f"{_num(data['work_hours'])} ч")))
    if food and food.get("entries"):
        kcal = round(float((food.get("totals") or {}).get("kcal") or 0))
        goal = (food.get("targets") or {}).get("kcal")
        rows.append(fmt.line("🍽", fmt.b(str(kcal)), "ккал", f"из {round(float(goal))}" if goal else None))
    if data.get("note"):
        rows.append(fmt.line("📝", fmt.i(str(data["note"])[:300])))
    return rows, gaps


@dataclass
class Prepared:
    draft: TrackerDraft
    ambiguous: list[tuple[str, str, list[int]]] = field(default_factory=list)  # (phrase, check|uncheck, item ids)
    error: str | None = None


class TrackerService:
    def __init__(self, settings: Settings, api: LifeTrackerApi | None, llm: LLMClient | None) -> None:
        self.settings = settings
        self.api = api
        self.llm = llm
        self._dict: tuple[float, Dictionary] | None = None

    def _api(self) -> LifeTrackerApi:
        if self.api is None:
            raise NotConfiguredError("трекер LMF отключён (LMF_ENABLED=false)")
        return self.api

    # --- dictionary ----------------------------------------------------------------------------------------

    async def dictionary(self) -> Dictionary:
        if self._dict and time.monotonic() - self._dict[0] < DICT_TTL:
            return self._dict[1]
        api = self._api()
        boot = await api.bootstrap()
        uses: Counter[int] = Counter()
        for day in await api.days(date(1970, 1, 1), date(2999, 12, 31)):
            uses.update(int(t) for t in day.get("tags") or [])
        tags = [
            Tag(int(t["id"]), str(t["code"]), str(t.get("label") or t["code"]), str(g["label"]), uses[int(t["id"])])
            for g in boot["groups"]
            for t in g.get("tags") or []
            if t.get("active", 1)
        ]
        items = [
            TplItem(int(i["id"]), str(i["name"]), str(c["name"]), int(c["idx"]))
            for c in boot["categories"]
            if c.get("active", 1)
            for i in c.get("items") or []
            if i.get("active", 1)
        ]
        supplements = {int(s["id"]): s for s in boot.get("supplements") or []}
        value = Dictionary(tags, items, list(boot["categories"]), supplements, supplements_from(boot))
        self._dict = (time.monotonic(), value)
        return value

    # --- days ----------------------------------------------------------------------------------------------

    async def snapshot(self, day: date) -> tuple[dict[str, Any], Dictionary, dict[str, Any] | None]:
        """A TLT day for the 📓 card: (the day, the dictionary, its food — None when LMF has no food tracking)."""
        d = await self.dictionary()
        data = await self._api().day(day)
        try:
            food: dict[str, Any] | None = await self._api().food_day(day)
        except (TransientError, NotConfiguredError):
            raise
        except Exception as exc:  # an LMF without the food API
            log.info("tracker: no food for the card: %s", exc)
            food = None
        return data, d, food

    def today(self, now: datetime) -> date:
        """The TLT day a report is about: before `TRACKER_DAY_STARTS_AT` (04:00) it is still yesterday."""
        local = to_local(now, self.settings.tz)
        if local.time() < parse_hhmm(self.settings.tracker_day_starts_at):
            return local.date() - timedelta(days=1)
        return local.date()

    def resolve_day(self, text: str | None, now: datetime) -> date | None:
        today = self.today(now)
        if not text:
            return today
        day = parse_day(text, today)
        if day is not None and day > today and day - today < timedelta(days=7) and not re.search(r"\d", text):
            day -= timedelta(days=7)  # «за пятницу» in a report is the last Friday
        return day if day is not None and day <= today else None

    # --- drafts --------------------------------------------------------------------------------------------

    async def prepare_day(self, text: str, now: datetime, portions: dict[str, Any] | None = None) -> Prepared:
        parsed = parse_day_report(text)
        d = await self.dictionary()
        day = self.resolve_day(parsed.day_text, now)
        if day is None:
            return Prepared(TrackerDraft(), error=f"Не понял день «{parsed.day_text}» (будущие дни не записываю).")
        draft = TrackerDraft(day=day.isoformat(), fields=dict(parsed.fields), note=parsed.note)
        if parsed.waist is not None:
            draft.body = {"waist_cm": parsed.waist}
        parsed.rest = self._take_supplements(parsed.rest, d, portions or {}, draft, remove=parsed.remove)
        tags: list[Tag] = []
        for code in parsed.codes:
            tag = d.tag(code)
            if tag is None:
                if not self._code_supplement(code, text, d, portions or {}, draft, remove=parsed.remove):
                    draft.unknown.append(code)
            elif tag not in tags:
                tags.append(tag)
        label_tags, leftover = d.match_labels(parsed.words())
        tags += [t for t in label_tags if t not in tags]
        if parsed.remove:
            leftover = self._clear_fields(draft, leftover)
        prepared = Prepared(draft)
        if leftover:
            if self.llm is not None:
                await self._extract(text, now, d, prepared, tags, explicit_day=parsed.day_text is not None)
            else:
                draft.unknown.append(" ".join(leftover))
        if parsed.remove:
            draft.tags_remove = [t.id for t in tags]
        else:
            draft.tags_add = [t.id for t in tags if t.id not in draft.tags_remove]
        if draft.empty() and not prepared.ambiguous:
            prepared.error = (
                "Не понял, что записать в TLT. Например: «спал 7:30, качество 85, оценка 7, настроение хорошее, CD LT»."
                + (f"\nНе знаю: {', '.join(draft.unknown)}." if draft.unknown else "")
            )
        return prepared

    @staticmethod
    def _take_supplements(
        rest: str, d: Dictionary, portions: dict[str, Any], draft: TrackerDraft, *, remove: bool
    ) -> str:
        """Supplements and drinks mentioned in a day report («кофеин 200», «2 энергетика»); the rest is returned."""
        left: list[str] = []
        for phrase in split_items(rest):
            taken = resolve(phrase, d.supplement_list, portions)
            if taken.supplement is not None:
                if remove:
                    draft.supplements_remove.append(taken.supplement.id)
                else:
                    draft.supplements_add.append(supplement_entry(taken))
            elif taken.needs_portion:
                draft.unknown.append(
                    f"{taken.portion}: сколько в нём кофеина? Скажите «{taken.portion} — 160 мг кофеина»"
                )
            else:
                left.append(phrase.said)
        return " , ".join(left)

    @staticmethod
    def _code_supplement(
        code: str, text: str, d: Dictionary, portions: dict[str, Any], draft: TrackerDraft, *, remove: bool
    ) -> bool:
        """A Latin token that is not a TLT tag may be a supplement («D3 6000», «B12»)."""
        m = re.search(rf"(?<![\w]){re.escape(code)}(?![\w+])(?:\s*(\d+(?:[.,]\d+)?)\s*(мкг|мг|г|ме|iu)?\b)?", text)
        phrase = " ".join(x for x in (code, m.group(1) if m else None, m.group(2) if m else None) if x)
        items = split_items(phrase)
        taken = resolve(items[0], d.supplement_list, portions) if items else None
        if taken is None or taken.supplement is None:
            return False
        if remove:
            draft.supplements_remove.append(taken.supplement.id)
        else:
            draft.supplements_add.append(supplement_entry(taken))
        return True

    @staticmethod
    def _clear_fields(draft: TrackerDraft, words: list[str]) -> list[str]:
        left = []
        for word in words:
            names = next((names for stem, names in _CLEARABLE if word.startswith(stem)), None)
            if names is None:
                left.append(word)
                continue
            for name in names:
                draft.fields[name] = None
        return left

    async def _extract(
        self, text: str, now: datetime, d: Dictionary, prepared: Prepared, tags: list[Tag], *, explicit_day: bool
    ) -> None:
        """Free speech the code did not understand → the LLM picks codes / items from the closed lists."""
        assert self.llm is not None
        draft = prepared.draft
        tag_list = "\n".join(f"{t.code} — {t.label}" for t in d.tags)
        tpl_list = "\n".join(f"{i.category}: {i.name}" for i in d.items)
        try:
            ext = await self.llm.structured(
                capability="tracker_extract",
                system=TRACKER_SYSTEM,
                user=f"<tags>\n{tag_list}\n</tags>\n<tpl>\n{tpl_list}\n</tpl>\n<message>{text[:1500]}</message>",
                schema=TrackerExtraction,
                max_tokens=600,
            )
        except (LLMOutputError, NotConfiguredError, TransientError) as exc:
            log.info("tracker extraction unavailable: %s", exc)
            draft.unknown.append(text.strip()[:80])
            return
        if ext.day_text and not explicit_day:
            day = self.resolve_day(ext.day_text, now)
            if day is not None:
                draft.day = day.isoformat()
        f = draft.fields
        if ext.sleep_text and "sleep_minutes" not in f:
            minutes, quality = parse_sleep(ext.sleep_text)  # the duration is computed by code, not the LLM
            if minutes is not None:
                f["sleep_minutes"] = minutes
                if quality is not None and "sleep_quality" not in f:
                    f["sleep_quality"] = quality
        for name, value in (
            ("sleep_quality", ext.sleep_quality),
            ("score", ext.score),
            ("mood", ext.mood),
        ):
            if value is not None and name not in f:
                f[name] = int(value) if isinstance(value, float) and value.is_integer() else value
        if ext.note and not draft.note:
            draft.note = ext.note.strip()
        for code in ext.tags:
            tag = d.tag(code)
            if tag is None:
                draft.unknown.append(code)
            elif tag not in tags:
                tags.append(tag)
        for code in ext.remove_tags:
            tag = d.tag(code)
            if tag is not None and tag.id not in draft.tags_remove:
                draft.tags_remove.append(tag.id)
        by_title = {f"{i.category}: {i.name}".lower(): i for i in d.items}
        for title in ext.tpl_items:
            item = by_title.get(title.strip().lower())
            if item is None:
                draft.unknown.append(title)
                continue
            draft.tpl_period = draft.tpl_period or self.today(now).strftime("%Y-%m")
            if item.id not in draft.tpl_check:
                draft.tpl_check.append(item.id)

    async def prepare_tpl(self, text: str, now: datetime) -> Prepared:
        today = self.today(now)
        period, month_match = parse_month(text, today.year, today.month)
        marks = parse_tpl_marks(text, month_match)
        d = await self.dictionary()
        prepared = Prepared(TrackerDraft(tpl_period=period))
        for mode, phrases in (("check", marks.check), ("uncheck", marks.uncheck)):
            for phrase in phrases:
                found = d.tpl_candidates(phrase)
                if not found:
                    prepared.draft.unknown.append(phrase)
                elif len(found) == 1:
                    target = prepared.draft.tpl_check if mode == "check" else prepared.draft.tpl_uncheck
                    if found[0].id not in target:
                        target.append(found[0].id)
                else:
                    prepared.ambiguous.append((phrase, mode, [i.id for i in found]))
        if prepared.draft.empty() and not prepared.ambiguous:
            names = ", ".join(prepared.draft.unknown)
            prepared.error = (
                f"В TPL нет пункта «{names}». " if names else ""
            ) + "Например: «TPL: отметь утреннюю рутину и чтение», «TPL: сними медитацию в отдыхе». Пункты — /tpl."
        return prepared

    # --- food ---------------------------------------------------------------------------------------------------

    async def prepare_food(
        self, text: str, now: datetime, *, day: date | None = None, meal: str | None = None
    ) -> Prepared:
        """«съел 200 г гречки и тарелку борща» → a draft: directory values by code, unknown foods estimated by the LLM
        (marked ≈, saved to the directory only after «✅ Записать»). `day` / `meal`: already known (pills and food
        said together)."""
        api = self._api()
        products = await api.food_products()
        day = day or self.resolve_day(parse_day_report(text).day_text, now)
        if day is None:
            return Prepared(TrackerDraft(), error="Будущие дни не записываю.")
        draft = TrackerDraft(day=day.isoformat())
        meal = meal or meal_of(text)
        at = to_local(now, self.settings.tz).strftime("%H:%M") if day == self.today(now) else None
        items: list[EatenItem] = []
        queue = []
        for phrase in split_food(text):
            product = find_product(phrase.words, products)
            grams = phrase.grams
            if grams is None and product and product.get("portion_g"):
                grams = float(product["portion_g"]) * (phrase.count or 1)
            if product is not None and grams is not None:
                items.append(EatenItem.of(product["name"], grams, product, product_id=int(product["id"])))
            else:
                queue.append((phrase, product))
        if queue:
            items += await self._estimate(queue, products, draft)
        for item in items:
            draft.food_add.append({**item.as_dict(), "meal": meal, "time": at})
        prepared = Prepared(draft)
        if not draft.food_add and not prepared.ambiguous:
            prepared.error = "Не понял, что записать в еду. Например: «съел 200 г гречки и 2 яйца»." + (
                f"\nНе знаю: {', '.join(draft.unknown)}." if draft.unknown else ""
            )
        return prepared

    async def prepare_food_remove(self, text: str, now: datetime) -> Prepared:
        """«удали гречку из еды» / «убери последнюю запись еды за вчера» → a draft that deletes ONE entry after
        «🗑 Удалить» (the latest of several matching ones; the button text says which)."""
        day = self.resolve_day(parse_day_report(text).day_text, now)
        if day is None:
            return Prepared(TrackerDraft(), error="Будущих дней в еде нет.")
        removal = parse_food_remove(text)
        entries = (await self._api().food_day(day)).get("entries") or []
        entry, matched = pick_entry(entries, removal)
        today = self.today(now)
        if entry is None:
            named = f"«{' '.join(removal.words)}»" if removal.words else "записей"
            listing = ", ".join(str(e.get("name")) for e in entries[-6:])
            return Prepared(
                TrackerDraft(),
                error=f"🍽 {human_day(day, today)}: в еде нет {named}."
                + (f" Записано: {listing}." if listing else " Еда за этот день не записана."),
            )
        keep = ("id", "name", "grams", "time", "created_at", *MACROS)
        item = {k: entry.get(k) for k in keep} | {"date": day.isoformat(), "matched": matched}
        return Prepared(TrackerDraft(day=day.isoformat(), food_remove=[item]))

    async def _food_remove_lines(self, draft: TrackerDraft, today: date, *, header: bool) -> fmt.Html:
        day = date.fromisoformat(str(draft.food_remove[0]["date"]))
        head = (
            fmt.title("🗑", "Удалить из еды", f"{human_day(day, today)}?") if header else fmt.head("Удалить из еды", "🗑")
        )
        lines: list[object] = [head]
        for item in draft.food_remove:
            at = fmt.i(item["time"]) if item.get("time") else None
            grams = f"{round(float(item['grams']))} г" if item.get("grams") else None
            many = fmt.i(f"(последняя из {item['matched']} похожих)") if int(item.get("matched") or 1) > 1 else None
            lines.append(fmt.line("•", at, fmt.b(item["name"]), grams, f"— {kbju(item)}", many))
        current = await self._api().food_day(day)
        left = float((current.get("totals") or {}).get("kcal") or 0) - sum(
            float(i.get("kcal") or 0) for i in draft.food_remove
        )
        lines.append(fmt.hint(f"За день станет {max(round(left), 0)} ккал"))
        return fmt.lines(*lines)

    async def _apply_food_remove(self, draft: TrackerDraft, today: date) -> fmt.Html:
        """Each entry is re-read first: LMF may give a deleted id to a new entry, and a retried job must not delete
        someone else's food — the id, name and creation time must all still match."""
        api = self._api()
        removed: list[dict[str, Any]] = []
        missing: list[str] = []
        totals: dict[str, Any] = {}
        for item in draft.food_remove:
            day = date.fromisoformat(str(item["date"]))
            entries = (await api.food_day(day)).get("entries") or []
            entry = next((e for e in entries if int(e.get("id") or 0) == int(item["id"])), None)
            if (
                entry is None
                or entry.get("name") != item.get("name")
                or entry.get("created_at") != item.get("created_at")
            ):
                missing.append(str(item.get("name")))
                continue
            result = await api.delete_food(int(item["id"]))
            totals = result.get("totals") or {}
            removed.append(item)
        if not removed:
            return fmt.esc(f"🍽 Записи «{', '.join(missing)}» уже нет — ничего не удалял.")
        day = date.fromisoformat(str(removed[0]["date"]))
        names = ", ".join(
            f"{i['name']} {round(float(i['grams']))} г" if i.get("grams") else str(i["name"]) for i in removed
        )
        return fmt.lines(
            fmt.line(fmt.title("🗑", "Удалил из еды", f"{human_day(day, today)}:"), names),
            fmt.line("За день:", fmt.b(kbju(totals))),
        )

    async def _estimate(self, queue: list[Any], products: list[dict[str, Any]], draft: TrackerDraft) -> list[EatenItem]:
        if self.llm is None:
            draft.unknown += [
                f"{p.said} — скажите КБЖУ на 100 г: «{p.said} — 120 ккал, Б 5, Ж 3, У 18»" for p, _ in queue
            ]
            return []
        listing = "\n".join(f"- {phrase.said}" for phrase, _ in queue)
        try:
            estimate = await self.llm.structured(
                capability="food_estimate",
                system=FOOD_SYSTEM,
                user=f"<items>\n{listing}\n</items>",
                schema=FoodEstimate,
                max_tokens=900,
            )
        except (LLMOutputError, NotConfiguredError, TransientError) as exc:
            log.info("food estimate unavailable: %s", exc)
            draft.unknown += [p.said for p, _ in queue]
            return []
        out: list[EatenItem] = []
        for (phrase, product), est in zip(queue, estimate.items, strict=False):
            item = self._from_estimate(
                est, products, draft, said=phrase.said, grams=phrase.grams, count=phrase.count, known=product,
                portion_word=phrase.portion_word,
            )  # fmt: skip
            if item is not None:
                out.append(item)
        if len(estimate.items) < len(queue):
            draft.unknown += [p.said for p, _ in queue[len(estimate.items) :]]
        return out

    @staticmethod
    def _from_estimate(
        est: FoodEstimateItem,
        products: list[dict[str, Any]],
        draft: TrackerDraft,
        *,
        said: str,
        grams: float | None = None,
        count: float | None = None,
        known: dict[str, Any] | None = None,
        portion_word: str | None = None,
    ) -> EatenItem | None:
        """An LLM estimate → an eaten item: the directory wins (the same food, the same numbers); an implausible
        estimate is dropped; a new food is saved to the directory after «✅ Записать»."""
        eaten = grams if grams is not None else est.grams
        known = known or find_product(re.findall(r"[а-яёa-z]+", est.name.lower()), products)
        if known is not None and known.get("kcal") is not None:
            item = EatenItem.of(known["name"], eaten, known, product_id=int(known["id"]))
            item.grams_estimate = grams is None
            return item
        if not plausible(est):
            draft.unknown.append(f"{said} (оценка не сошлась — скажите КБЖУ на 100 г)")
            return None
        per100 = {"kcal": est.kcal_100, "protein": est.protein_100, "fat": est.fat_100, "carbs": est.carbs_100}
        portion = est.grams / count if count and grams is None else None
        return EatenItem.of(
            est.name.strip()[:60],
            eaten,
            per100,
            estimate=True,
            grams_estimate=grams is None,
            new_product={
                "name": est.name.strip()[:60],
                **per100,
                "portion_g": round(portion, 1) if portion else None,
                "portion_name": "шт" if portion and not portion_word else None,
                "source": "estimate",
            },
        )

    async def prepare_food_photo(self, reading: PhotoReading, caption: str, now: datetime) -> Prepared:
        """A photographed label (values copied as printed) or dish (≈ typical values) → a draft for «✅ Записать».
        The caption says how much was eaten («съел половину», «200 г»); without it a label means the whole package."""
        api = self._api()
        products = await api.food_products()
        day = self.resolve_day(parse_day_report(caption).day_text, now)
        if day is None:
            return Prepared(TrackerDraft(), error="Будущие дни не записываю.")
        draft = TrackerDraft(day=day.isoformat())
        amount = eaten_amount(caption)
        meal = meal_of(caption)
        at = to_local(now, self.settings.tz).strftime("%H:%M") if day == self.today(now) else None
        items: list[EatenItem] = []
        if reading.kind == PhotoKind.FOOD_LABEL and reading.label is not None:
            label = reading.label
            per100 = {"kcal": label.kcal_100, "protein": label.protein_100, "fat": label.fat_100,
                      "carbs": label.carbs_100}  # fmt: skip
            name = label.name.strip()[:60] or "Продукт"
            if per100["kcal"] is None:
                return Prepared(
                    TrackerDraft(),
                    error=f"📷 «{name}»: не разобрал калорийность на этикетке. Скажите значения на 100 г: "
                    f"«{name} — 380 ккал, Б 30, Ж 12, У 35».",
                )
            pack = label.package_g or label.portion_g
            product = {"name": name, **per100, "portion_g": pack,
                       "portion_name": "упаковка" if label.package_g else ("порция" if label.portion_g else None),
                       "source": "label"}  # fmt: skip
            where = f", упаковка {round(label.package_g)} г" if label.package_g else ""
            draft.header.append(f"📷 Этикетка: {name} — {kbju(per100)} на 100 г{where}")
            grams = amount.grams
            if grams is None and pack:
                grams = pack * (amount.share or 1.0)
            if grams is None:
                draft.products_save.append(product)
                draft.header.append("Сколько съели — не знаю: сохраню продукт, потом скажите «съел 50 г …».")
            else:
                item = EatenItem.of(name, grams, per100, new_product=product)
                item.grams_estimate = amount.grams is None
                items.append(item)
                if amount.grams is None and amount.share is None:
                    draft.header.append("Считаю, что съедена вся упаковка; если нет — подпишите фото: «съел 30 г».")
        elif reading.kind == PhotoKind.DISH and reading.dishes:
            single = len(reading.dishes) == 1
            for est in reading.dishes:
                grams = amount.grams if single else None
                if grams is None and single and amount.share is not None:
                    grams = est.grams * amount.share
                dish = self._from_estimate(est, products, draft, said=est.said, grams=grams)
                if dish is not None:
                    dish.grams_estimate = amount.grams is None
                    items.append(dish)
            draft.header.append("📷 На фото: " + ", ".join(e.name for e in reading.dishes))
        else:
            return Prepared(TrackerDraft(), error="📷 На фото не вижу еды или этикетки.")
        for item in items:
            draft.food_add.append({**item.as_dict(), "meal": meal, "time": at})
        prepared = Prepared(draft)
        if draft.empty():
            prepared.error = "📷 Не смог оценить еду на фото." + (
                f"\nНе знаю: {', '.join(draft.unknown)}." if draft.unknown else ""
            )
        return prepared

    async def _food_lines(self, draft: TrackerDraft, today: date, *, header: bool) -> fmt.Html:
        day = date.fromisoformat(str(draft.day))
        lines: list[object] = [
            fmt.title("🍽", "Записать еду", f"{human_day(day, today)}?") if header else fmt.head("Еда", "🍽")
        ]
        for item in draft.food_add:
            grams = (
                f"{'≈' if item.get('grams_estimate') else ''}{round(float(item['grams']))} г"
                if item.get("grams")
                else None
            )
            mark = fmt.i("≈ оценка ИИ — сохраню в справочник") if item.get("estimate") else None
            lines.append(fmt.line("•", fmt.b(item["name"]), grams, f"— {kbju(item)}", mark))
        added = {k: sum(float(i.get(k) or 0) for i in draft.food_add) for k in MACROS}
        current = await self._api().food_day(day)
        targets = current.get("targets") or {}
        total = float((current.get("totals") or {}).get("kcal") or 0) + added["kcal"]
        goal = f" из {round(float(targets['kcal']))}" if targets.get("kcal") else ""
        tail = fmt.i(f"· за день будет {round(total)}{goal} ккал") if current.get("entries") or goal else None
        lines.append(fmt.line("Итого:", fmt.b(kbju(added)), tail))
        return fmt.lines(*lines)

    async def _apply_food(self, draft: TrackerDraft, today: date) -> fmt.Html:
        api = self._api()
        day = date.fromisoformat(str(draft.day))
        entries = []
        for n, item in enumerate(draft.food_add):
            product_id = item.get("product_id")
            if item.get("new_product"):  # a confirmed estimate becomes a directory entry
                saved = await api.save_product(item["new_product"])
                product_id = saved.get("id")
            entries.append(
                {
                    "name": item["name"],
                    "grams": item.get("grams"),
                    **{k: item.get(k) for k in MACROS},
                    "product_id": product_id,
                    "meal": item.get("meal"),
                    "time": item.get("time"),
                    "ext_id": f"jarvis:{draft.ref}:{n}",
                }
            )
        result = await api.add_food(day, entries)
        totals, targets = result.get("totals") or {}, result.get("targets") or {}
        goal = f" из {round(float(targets['kcal']))}" if targets.get("kcal") else ""
        names = ", ".join(
            f"{i['name']} {round(float(i['grams']))} г" if i.get("grams") else i["name"] for i in draft.food_add
        )
        added = {k: sum(float(i.get(k) or 0) for i in draft.food_add) for k in MACROS}
        norm = f"(норма {goal.replace(' из ', '')} ккал)" if goal else None
        return fmt.lines(
            fmt.line(fmt.title("🍽", "Записал", f"{human_day(day, today)}:"), f"{names} — {kbju(added)}"),
            fmt.line("За день:", fmt.b(kbju(totals)), fmt.i(norm) if norm else None),
        )

    async def food_text(self, day: date, now: datetime) -> fmt.Html:
        data = await self._api().food_day(day)
        today = self.today(now)
        entries, totals, targets = data.get("entries") or [], data.get("totals") or {}, data.get("targets") or {}
        title = human_day(day, today)
        title = title[:1].upper() + title[1:]
        if not entries:
            return fmt.lines(
                fmt.title("🍽", title, "еда не записана"), fmt.hint("Скажите, например: «съел 200 г гречки и 2 яйца».")
            )
        summary = [fmt.line("Всего:", fmt.b(kbju(totals)))]
        if targets:
            parts = []
            for key, label in (("kcal", "ккал"), ("protein", "Б"), ("fat", "Ж"), ("carbs", "У")):
                if targets.get(key):
                    left = float(targets[key]) - float(totals.get(key) or 0)
                    parts.append(
                        f"{label} {'осталось ' + str(round(left)) if left >= 0 else 'перебор ' + str(round(-left))}"
                    )
            summary.append(fmt.hint("Норма: " + " · ".join(parts)))
        rows = []
        for e in entries:
            at = fmt.i(e["time"]) if e.get("time") else None
            grams = f"{round(float(e['grams']))} г" if e.get("grams") else None
            rows.append(fmt.line("•", at, e["name"], grams, f"— {round(float(e.get('kcal') or 0))} ккал"))
        return fmt.blocks(fmt.title("🍽", title), summary, rows)

    async def save_product_text(self, text: str) -> str:
        """«гречка — 330 ккал, Б 12, Ж 3, У 62» · «яйцо — 1 шт 55 г» · «энергетик — банка 500 мл, 225 ккал на банку»."""
        m = re.match(r"^\s*(?P<name>[^—–:=\-]+?)\s*(?:—|–|-|:|=)\s*(?P<rest>.+)$", text)
        if not m:
            return "Скажите так: «гречка — 330 ккал, Б 12, Ж 3, У 62» (на 100 г)."
        name = m.group("name").strip()
        values = parse_nutrition(m.group("rest"))
        if values.kcal is None and values.portion_g is None:
            return "Не нашёл чисел. Например: «гречка — 330 ккал, Б 12, Ж 3, У 62» или «яйцо — 1 шт 55 г»."
        products = await self._api().food_products()
        existing = next((p for p in products if p.get("key") == product_key(name)), None)
        saved = await self._api().save_product(
            {
                "name": existing["name"] if existing else name[:1].upper() + name[1:],
                **values.per100(),
                "portion_g": values.portion_g,
                "portion_name": values.portion_name,
                "source": "user",
            }
        )
        portion = (
            f" · {saved['portion_name'] or 'порция'} = {round(float(saved['portion_g']))} г"
            if saved.get("portion_g")
            else ""
        )
        return f"🍽 Запомнил: {saved['name']} на 100 г — {kbju(saved)}{portion}."

    async def set_targets_text(self, text: str) -> str:
        values = parse_nutrition(text)
        if values.kcal is None and (m := re.search(r"\d{3,5}", text)):
            values.kcal = float(m.group())
        targets = {k: v for k, v in values.per100().items() if v is not None}
        if not targets:
            return "Скажите так: «норма 2200 ккал, белок 140, жиры 70, углеводы 250»."
        api = self._api()
        # Only the named norms change: an LMF before 17.10 replaced the whole set on save.
        current = {k: v for k, v in (await api.food_targets() or {}).items() if v is not None}
        saved = await api.save_food_targets({**current, **targets})
        return "🎯 Норма на день: " + kbju(saved).replace("—", "не задано")

    async def drink_food(self, taken: list[Taken]) -> list[dict[str, Any]]:
        """«выпил энергетик»: besides caffeine, its calories when the drink is in the food directory."""
        portions = [t for t in taken if t.portion]
        if not portions:
            return []
        try:
            products = await self._api().food_products()
        except Exception:
            return []
        out = []
        for t in portions:
            product = find_product([str(t.portion)], products)
            if product is None or not product.get("portion_g"):
                continue
            grams = float(product["portion_g"]) * t.count
            out.append({**EatenItem.of(product["name"], grams, product, product_id=int(product["id"])).as_dict()})
        return out

    # --- supplements ----------------------------------------------------------------------------------------

    async def intake(self, text: str, portions: dict[str, Any], kind: Kind) -> Intake:
        """Pills and food said together → supplements (resolved) and food phrases (for `prepare_food`)."""
        d = await self.dictionary()
        try:
            products = await self._api().food_products()
        except FoodNotSupportedError:
            products = []
        return split_intake(text, d.supplement_list, portions, products, kind)

    async def taken(self, text: str, portions: dict[str, Any] | None = None) -> list[Taken]:
        """«D3 6000, B12 500, цинк 25», «2 энергетика» → resolved items (ambiguous / unknown ones are marked)."""
        d = await self.dictionary()
        return [resolve(p, d.supplement_list, portions or {}) for p in split_items(text)]

    async def log_items(
        self,
        items: list[dict[str, Any]],
        day: date,
        now: datetime,
        memo: dict[str, Any] | None = None,
        remember: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> fmt.Html:
        """Write taken pack items (`Medication.items`) to the TLT day; doses are added to what the day has."""
        d = await self.dictionary()
        draft = TrackerDraft(day=day.isoformat())
        missing = []
        for item in items:
            supplement = next((x for x in d.supplement_list if x.name == item.get("supplement")), None)
            if supplement is None:  # renamed in LMF or never matched: try the words as said
                phrases = split_items(str(item.get("said") or item.get("label") or ""))
                found = find_supplement(phrases[0].words, d.supplement_list) if phrases else []
                supplement = found[0] if len(found) == 1 else None
            if supplement is None:
                missing.append(str(item.get("label") or item.get("said")))
                continue
            draft.supplements_add.append(
                {
                    "id": supplement.id,
                    "dose": item.get("dose"),
                    "dose_text": item.get("dose_text"),
                    "label": supplement.title,
                }
            )
        written = await self.apply(draft, now, memo, remember) if draft.supplements_add else None
        absent = fmt.hint("Нет в справочнике LMF (не записал): " + ", ".join(missing)) if missing else None
        return fmt.lines(written, absent)

    async def add_supplement(self, name: str, unit: str = "", default_dose: float | None = None) -> None:
        """The user pressed «➕ Добавить в LMF»: a new dictionary entry (the only dictionary write)."""
        await self._api().add_supplement(name, name, unit, default_dose)
        self._dict = None

    # --- confirmation and writing ---------------------------------------------------------------------------

    async def describe(self, draft: TrackerDraft, now: datetime) -> fmt.Html:
        """The confirmation of a draft: one section per kind (TLT day, food, products, TPL), values in bold."""
        d = await self.dictionary()
        today = self.today(now)
        sections: list[object] = [fmt.lines(*draft.header)] if draft.header else []
        food = draft.food_add or draft.food_remove or draft.products_save
        if draft.day and (draft.has_day_changes() or not (draft.has_tpl_changes() or food)):
            day = date.fromisoformat(draft.day)
            current = await self._api().day(day)
            lines: list[object] = [fmt.title("📓", "Записать в TLT", f"{human_day(day, today)}?")]
            lines += self._field_lines(draft, current)
            if draft.tags_add:
                have = set(int(t) for t in current.get("tags") or [])
                shown = []
                for tag_id in draft.tags_add:
                    tag = d.tag_by_id(tag_id)
                    if tag is not None:
                        shown.append(tag.short + (" (уже есть)" if tag_id in have else ""))
                lines.append(fmt.line("• Отметки:", fmt.b("+ " + ", ".join(shown))))
            if draft.tags_remove:
                codes = [t.code for t in (d.tag_by_id(i) for i in draft.tags_remove) if t is not None]
                lines.append(fmt.line("• Снять отметки:", fmt.b("− " + ", ".join(codes))))
            lines += self._supplement_lines(draft, current, d)
            if draft.body:
                lines.append(fmt.line("• ⚖️ Тело:", fmt.b(body_text(draft.body)), self._was_weight(draft, current)))
            if draft.note:
                lines.append(fmt.line("• Заметка:", fmt.i(draft.note)))
            sections.append(fmt.lines(*lines))
        if draft.food_add and draft.day:
            sections.append(await self._food_lines(draft, today, header=len(sections) == (1 if draft.header else 0)))
        products = [
            fmt.line(
                "💾 В справочник продуктов:",
                fmt.b(product["name"]),
                f"— {kbju(product)} на 100 г"
                + (f", {product['portion_name']} {round(product['portion_g'])} г" if product.get("portion_g") else ""),
            )
            for product in draft.products_save
        ]
        sections.append(products)
        if draft.food_remove:
            sections.append(await self._food_remove_lines(draft, today, header=not any(sections)))
        if draft.has_tpl_changes() and draft.tpl_period:
            month = self.month_name(draft.tpl_period)
            head = (
                fmt.title("📊", "Отметить в TPL", f"{month}?")
                if not any(sections)
                else fmt.head(f"TPL · {month}", "📊")
            )
            items = [fmt.line("• ☑", item.title) for item in (d.item(i) for i in draft.tpl_check) if item]
            items += [fmt.line("• ☐ снять:", item.title) for item in (d.item(i) for i in draft.tpl_uncheck) if item]
            sections.append(fmt.lines(head, items))
        if draft.unknown:
            sections.append(fmt.hint("❔ Не понял (не запишу): " + ", ".join(f"«{u}»" for u in draft.unknown)))
        return fmt.blocks(*sections)

    @staticmethod
    def _was_weight(draft: TrackerDraft, current: dict[str, Any]) -> fmt.Html | None:
        old = (current.get("body") or {}).get("weight")
        new = draft.body.get("weight")
        return fmt.i(f"(было {_num(old)} кг)") if old not in (None, "") and new is not None and old != new else None

    @staticmethod
    def _supplement_lines(draft: TrackerDraft, current: dict[str, Any], d: Dictionary) -> list[fmt.Html]:
        lines = []
        have = {int(x["supplement_id"]): x for x in current.get("supplements") or []}
        if draft.supplements_add:
            shown = []
            for entry in draft.supplements_add:
                text = supplement_text(entry, d)
                before = have.get(int(entry["id"]))
                if before is not None and before.get("dose") is not None and entry.get("dose") is not None:
                    unit = (d.supplement(int(entry["id"])) or Supplement(0, "", "", "", None)).unit
                    total = float(before["dose"]) + float(entry["dose"])
                    text += f" (за день будет {_num(round(total, 3))}{' ' + unit if unit else ''})"
                shown.append(text)
            lines.append(fmt.line("• 💊 Добавки:", fmt.b("+ " + ", ".join(shown))))
        if draft.supplements_remove:
            names = [x.title for x in (d.supplement(i) for i in draft.supplements_remove) if x is not None]
            lines.append(fmt.line("• 💊 Убрать:", fmt.b("− " + ", ".join(names))))
        return lines

    def _field_lines(self, draft: TrackerDraft, current: dict[str, Any]) -> list[fmt.Html]:
        f = draft.fields
        lines: list[fmt.Html] = []

        def was(name: str, shown: str) -> fmt.Html | None:
            old = current.get(name)
            return fmt.i(f"(было {shown})") if old not in (None, "") and old != f.get(name) else None

        if "sleep_minutes" in f or "sleep_quality" in f:
            if f.get("sleep_minutes", "-") is None:
                lines.append(fmt.line("• Сон:", fmt.b("очистить")))
            else:
                sleep = hm(f.get("sleep_minutes", current.get("sleep_minutes")))
                quality = f.get("sleep_quality")
                old = was("sleep_minutes", hm(current.get("sleep_minutes")))
                value = fmt.Html(f"{fmt.b(sleep)}, качество {quality}") if quality is not None else fmt.b(sleep)
                lines.append(fmt.line("• Сон:", value, old))
        for name, label in (
            ("score", "Оценка дня"),
            ("mood", "Настроение"),
            ("charge", "Заряд"),
            ("work_hours", "Работа"),
        ):
            if name not in f:
                continue
            value = f[name]
            if value is None:
                lines.append(fmt.line(f"• {label}:", fmt.b("очистить")))
                continue
            shown = MOODS.get(str(value), _num(value)) if name == "mood" else _num(value)
            shown += " ч" if name == "work_hours" else ""
            previous = current.get(name)
            if name == "mood" and previous:
                old_shown = MOODS.get(str(previous), _num(previous))
            else:
                old_shown = _num(previous) if previous else ""
            changed = previous not in (None, "") and previous != value
            lines.append(fmt.line(f"• {label}:", fmt.b(shown), fmt.i(f"(было {old_shown})") if changed else None))
        if "note" in f and f["note"] is None:
            lines.append(fmt.line("• Заметка:", fmt.b("очистить")))
        return lines

    async def apply(
        self,
        draft: TrackerDraft,
        now: datetime,
        memo: dict[str, Any] | None = None,
        remember: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> fmt.Html:
        """Write an approved draft. Re-reads the day first; tags are merged, never replaced blindly. Idempotent.

        Doses are ADDED, so the day's supplements before the first attempt are kept (`remember` → the job's payload,
        back as `memo` on a retry): a write whose reply was lost must not add the dose again (R38)."""
        api = self._api()
        d = await self.dictionary()
        today = self.today(now)
        parts: list[object] = []
        if draft.day and draft.has_day_changes():
            day = date.fromisoformat(draft.day)
            current = await api.day(day)
            payload: dict[str, Any] = dict(draft.fields)
            if draft.note:
                existing = str(current.get("note") or "")
                if draft.note not in existing:
                    payload["note"] = f"{existing}\n{draft.note}" if existing else draft.note
            if draft.tags_add or draft.tags_remove:
                tags = {int(t) for t in current.get("tags") or []}
                tags |= set(draft.tags_add)
                tags -= set(draft.tags_remove)
                payload["tags"] = sorted(tags)
            if draft.supplements_add or draft.supplements_remove:
                now_rows = list(current.get("supplements") or [])
                base = (memo or {}).get("supplements_base")
                if base is None:
                    base = now_rows
                    if remember is not None:
                        await remember("supplements_base", base)
                payload["supplements"] = merge_supplements(
                    base, draft.supplements_add, draft.supplements_remove, since=now_rows
                )
            if draft.body:  # LMF replaces the whole «тело» row: keep what the day had, lay the new values over it
                kept = {k: v for k, v in (current.get("body") or {}).items() if k in BODY_FIELDS}
                payload["body"] = kept | {k: v for k, v in draft.body.items() if k in BODY_FIELDS}
            await api.save_day(day, payload)
            parts.append(
                fmt.line(fmt.title("📓", "Записал в TLT", f"{human_day(day, today)}:"), self._summary(draft, d))
            )
        if draft.food_add and draft.day:
            parts.append(await self._apply_food(draft, today))
        if draft.food_remove:
            parts.append(await self._apply_food_remove(draft, today))
        for product in draft.products_save:
            saved = await api.save_product(product)
            parts.append(fmt.line("💾 Сохранил в справочник продуктов:", fmt.b(saved.get("name") or product["name"])))
        if draft.has_tpl_changes() and draft.tpl_period:
            before = await api.tpl(draft.tpl_period)
            checks = {i: True for i in draft.tpl_check} | {i: False for i in draft.tpl_uncheck}
            after = await api.save_tpl(draft.tpl_period, checks)
            parts.append(self._tpl_change(before, after, draft))
        return fmt.lines(*parts) or fmt.esc("Нечего записывать.")

    def _summary(self, draft: TrackerDraft, d: Dictionary) -> str:
        f = draft.fields
        bits: list[str] = []
        if f.get("sleep_minutes"):
            quality = f.get("sleep_quality")
            bits.append(f"сон {hm(f['sleep_minutes'])}" + (f" ({quality})" if quality is not None else ""))
        elif f.get("sleep_quality") is not None:
            bits.append(f"качество сна {f['sleep_quality']}")
        if f.get("score") is not None:
            bits.append(f"оценка {_num(f['score'])}")
        if f.get("mood"):
            bits.append(f"настроение {MOODS[f['mood']].split()[-1]}")
        if f.get("charge"):
            bits.append(f"заряд {f['charge']}")
        if f.get("work_hours") is not None:
            bits.append(f"работа {_num(f['work_hours'])} ч")
        cleared = [FIELD_NAMES[k] for k, v in f.items() if v is None and k in FIELD_NAMES]
        if cleared:
            bits.append("очищено: " + ", ".join(dict.fromkeys(cleared)))
        if draft.tags_add:
            bits.append("+ " + ", ".join(t.code for t in (d.tag_by_id(i) for i in draft.tags_add) if t))
        if draft.tags_remove:
            bits.append("− " + ", ".join(t.code for t in (d.tag_by_id(i) for i in draft.tags_remove) if t))
        if draft.supplements_add:
            bits.append("💊 " + ", ".join(supplement_text(e, d) for e in draft.supplements_add))
        if draft.supplements_remove:
            names = [x.title for x in (d.supplement(i) for i in draft.supplements_remove) if x is not None]
            bits.append("💊 − " + ", ".join(names))
        if draft.body:
            bits.append("⚖️ " + body_text(draft.body))
        if draft.note:
            bits.append("заметка")
        return " · ".join(bits) or "без изменений"

    def _tpl_change(self, before: dict[str, Any], after: dict[str, Any], draft: TrackerDraft) -> fmt.Html:
        touched = set(draft.tpl_check) | set(draft.tpl_uncheck)
        old = {c["id"]: c for c in before.get("categories") or []}
        changes = []
        for c in after.get("categories") or []:
            if any(i["id"] in touched for i in c.get("items") or []):
                prev = old.get(c["id"], {}).get("pct", 0.0)
                changes.append(f"{c['name']} {round(prev * 100)}% → {round(c['pct'] * 100)}%")
        total = f"Total {round(before.get('total', 0) * 100)}% → {round(after.get('total', 0) * 100)}%"
        month = self.month_name(str(after.get("period") or draft.tpl_period))
        return fmt.line(fmt.title("📊", "TPL", f"{month}:"), ", ".join([*changes, total]))

    # --- summaries -----------------------------------------------------------------------------------------

    @staticmethod
    def month_name(period: str) -> str:
        names = [
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        ]  # fmt: skip
        year, month = int(period[:4]), int(period[5:7])
        return f"{names[month - 1]} {year}"

    @staticmethod
    def missing(day: dict[str, Any]) -> list[str]:
        gaps = []
        if day.get("sleep_minutes") is None:
            gaps.append("сон")
        if day.get("score") is None:
            gaps.append("оценка")
        if not day.get("mood"):
            gaps.append("настроение")
        if not day.get("tags"):
            gaps.append("отметки")
        return gaps

    @staticmethod
    def is_empty(day: dict[str, Any]) -> bool:
        fields = ("sleep_minutes", "sleep_quality", "score", "mood", "charge", "work_hours", "note")
        return (
            not any(day.get(k) not in (None, "") for k in fields) and not day.get("tags") and not day.get("supplements")
        )

    async def day_text(self, day: date, now: datetime) -> fmt.Html:
        d = await self.dictionary()
        data = await self._api().day(day)
        today = self.today(now)
        title = fmt.title("📓", "TLT", human_day(day, today))
        if self.is_empty(data):
            return fmt.lines(
                title,
                fmt.hint("Пусто. Скажите, например: «спал 7:30, качество 85, оценка 7, настроение хорошее, CD LT»."),
            )
        rows, _gaps = day_rows(data, d)
        gaps = self.missing(data)
        return fmt.blocks(title, rows, fmt.hint("Не заполнено: " + ", ".join(gaps)) if gaps else None)

    async def period_text(self, start: date, end: date, now: datetime) -> fmt.Html:
        d = await self.dictionary()
        days = [x for x in await self._api().days(start, end) if not self.is_empty(x)]
        total = (end - start).days + 1
        title = fmt.title(
            "📓", "TLT", f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}: заполнено {len(days)} из {total} дн."
        )
        if not days:
            return title

        def avg(values: Iterable[float]) -> float | None:
            items = list(values)
            return sum(items) / len(items) if items else None

        sleep = avg(x["sleep_minutes"] for x in days if x.get("sleep_minutes") is not None)
        quality = avg(x["sleep_quality"] for x in days if x.get("sleep_quality") is not None)
        score = avg(x["score"] for x in days if x.get("score") is not None)
        stats = []
        if sleep is not None:
            stats.append(
                fmt.line(
                    "🛌 сон в среднем", fmt.b(hm(round(sleep))), f"(качество {round(quality)})" if quality else None
                )
            )
        if score is not None:
            stats.append(fmt.line("⭐ оценка", fmt.b(_num(round(score, 1)))))
        rows = []
        for x in days[-10:]:
            day = date.fromisoformat(x["date"])
            bits = []
            if x.get("sleep_minutes") is not None:
                bits.append(
                    f"сон {hm(x['sleep_minutes'])}" + (f" ({x['sleep_quality']})" if x.get("sleep_quality") else "")
                )
            if x.get("score") is not None:
                bits.append(f"оценка {_num(x['score'])}")
            codes = [t.code for t in (d.tag_by_id(int(i)) for i in x.get("tags") or []) if t]
            if codes:
                bits.append(" ".join(codes))
            when = fmt.b(f"{WEEKDAY_SHORT[day.weekday()]} {day.strftime('%d.%m')}")
            rows.append(fmt.line("•", fmt.Html(f"{when}:"), " · ".join(bits) or "—"))
        counts = Counter(int(t) for x in days for t in x.get("tags") or [])
        top = [f"{t.code} ×{n}" for tag_id, n in counts.most_common(5) if (t := d.tag_by_id(tag_id))]
        return fmt.blocks(
            title,
            fmt.line(*stats, sep=" · ") if stats else None,
            rows,
            fmt.line("🏷", fmt.b("Чаще всего:"), ", ".join(top)) if top else None,
        )

    async def tpl_text(self, period: str) -> fmt.Html:
        data = await self._api().tpl(period)
        title = fmt.title(
            "📊", f"TPL · {self.month_name(period)}", f"Total {round(float(data.get('total') or 0) * 100)}%"
        )
        rows = []
        for c in data.get("categories") or []:
            pct = float(c.get("pct") or 0)
            if pct >= float(c.get("wow") or 0.9):
                mark = "⭐"
            elif pct >= float(c.get("target") or 0.7):
                mark = "🟢"
            elif pct >= float(c.get("minimum") or 0.5):
                mark = "🟡"
            else:
                mark = "🔴"
            items = c.get("items") or []
            done = [i["name"] for i in items if i.get("checked")]
            rows.append(
                fmt.line(
                    mark,
                    fmt.b(c["name"]),
                    f"{round(pct * 100)}% ({len(done)}/{len(items)})" + (":" if done else ""),
                    fmt.i(", ".join(done)) if done else None,
                )
            )
        return fmt.blocks(
            title, rows, fmt.hint("Отметить: «TPL: отметь утреннюю рутину и чтение»; снять: «TPL: сними чтение».")
        )

    # --- reminders -----------------------------------------------------------------------------------------

    async def morning_line(self, now: datetime) -> str | None:
        """08:00: ask about the night if the sleep of today is not in TLT yet. None when LMF is unavailable."""
        try:
            day = await self._api().day(to_local(now, self.settings.tz).date())
        except Exception as exc:
            log.info("tracker: morning check skipped: %s", exc)
            return None
        if day.get("sleep_minutes") is not None:
            return None
        return "🛌 Как спалось? Скажите, например: «спал 7:30, качество 85» — запишу в TLT."

    async def evening_lines(self, now: datetime) -> list[str]:
        """21:00: the day's score and mood are asked by the evening check-in (buttons), not listed here. On the last
        day of the month — TPL; food only when something was eaten (no daily nag: it was never used)."""
        local = to_local(now, self.settings.tz).date()
        lines: list[str] = []
        try:
            if (local + timedelta(days=1)).day == 1:
                period = local.strftime("%Y-%m")
                tpl = await self._api().tpl(period)
                lines.append(
                    f"📊 Завтра новый месяц — отметьте TPL за {self.month_name(period).split()[0]} "
                    f"(сейчас Total {round(float(tpl.get('total') or 0) * 100)}%): «TPL: отметь …» или /tpl."
                )
        except Exception as exc:
            log.info("tracker: evening check skipped: %s", exc)
        try:
            food = await self._api().food_day(local)
            totals, targets = food.get("totals") or {}, food.get("targets") or {}
            if food.get("entries"):
                goal = f" (норма {round(float(targets['kcal']))} ккал)" if targets.get("kcal") else ""
                lines.append(f"🍽 Сегодня: {kbju(totals)}{goal}")
        except Exception as exc:  # an LMF without food tracking, or LMF down
            log.info("tracker: food line skipped: %s", exc)
        return lines

    async def day_values(self, day: date) -> dict[str, Any]:
        """The TLT day as LMF has it (sleep, score, tags, supplements, body …)."""
        return await self._api().day(day)

    async def day_is_empty(self, now: datetime) -> bool | None:
        try:
            return self.is_empty(await self._api().day(to_local(now, self.settings.tz).date()))
        except Exception as exc:
            log.info("tracker: empty-day check skipped: %s", exc)
            return None
