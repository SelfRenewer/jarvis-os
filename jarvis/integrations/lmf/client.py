"""LMF (Life Management Framework, ~/Sites/TLT/app) — the user's TLT / TPL tracker on the Mac.

A local Node server with SQLite and a REST API (127.0.0.1:4321; from Docker: host.docker.internal:4321). Jarvis
authenticates with LMF's machine token (`Authorization: Bearer`, `LMF_API_TOKEN` from `jarvis lmf-setup`); the
browser UI has its own password login.
Jarvis uses only reads plus a few writes: `PUT /api/day/:date` (the day card), `PUT /api/tpl/:period` (TPL
checkboxes), food entries (add; delete one entry after a «🗑 Удалить» button). It never deletes days and never edits
the dictionaries (except «➕ Добавить в LMF» and products confirmed with «✅ Записать»). `PUT /api/day` replaces the
`tags` and `supplements` arrays wholesale, so callers read the day first and send the merged lists.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import httpx

from jarvis.core.errors import NotFoundError, PermanentError
from jarvis.core.http import ResilientHttp
from jarvis.core.retry import RetryPolicy

# The user waits for an answer: one quick retry. The approved write itself is a job with its own retries.
LMF_RETRY = RetryPolicy(base_seconds=0.5, cap_seconds=1.0, max_attempts=2, jitter_ratio=0)


class LifeTrackerApi(Protocol):
    async def bootstrap(self) -> dict[str, Any]:
        """Dictionaries: tag groups with tags, supplements, TPL categories with items."""
        ...

    async def day(self, day: date) -> dict[str, Any]: ...

    async def days(self, start: date, end: date) -> list[dict[str, Any]]: ...

    async def save_day(self, day: date, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def tpl(self, period: str) -> dict[str, Any]: ...

    async def save_tpl(self, period: str, checks: dict[int, bool]) -> dict[str, Any]: ...

    async def add_supplement(self, name: str, label: str, unit: str, default_dose: float | None) -> dict[str, Any]:
        """The only dictionary write — after the user's «➕ Добавить в LMF» button."""
        ...

    # --- food (LMF ≥ the «Питание» version; an older server answers 404 → FoodNotSupported) ---------------

    async def food_products(self) -> list[dict[str, Any]]: ...

    async def save_product(self, product: dict[str, Any]) -> dict[str, Any]:
        """Upsert by name (case-insensitive): KBJU per 100 g, portion size, source user | estimate."""
        ...

    async def food_day(self, day: date) -> dict[str, Any]: ...

    async def add_food(self, day: date, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Append eaten food; an entry with an already used `ext_id` is ignored (retries are safe)."""
        ...

    async def delete_food(self, entry_id: int) -> dict[str, Any]:
        """Delete one eaten-food entry (after «🗑 Удалить»); a missing id is a no-op."""
        ...

    async def food_days(self, start: date, end: date) -> list[dict[str, Any]]: ...

    async def food_targets(self) -> dict[str, Any]: ...

    async def save_food_targets(self, targets: dict[str, Any]) -> dict[str, Any]: ...

    async def labs(self) -> list[dict[str, Any]]: ...

    async def save_labs(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert blood-test rows by (date, name, source)."""
        ...


class FoodNotSupportedError(PermanentError):
    """The running LMF predates food tracking: restart the TLT site in LocalSites."""


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise PermanentError("lmf: not JSON") from exc


class HttpLifeTracker:
    def __init__(
        self, base_url: str, *, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # The LMF server directly (port 4321): the LocalSites proxy (5181) rejects the Host host.docker.internal.
        # No token = no header: an LMF without a password (or an old one) still answers; with a password it says 401.
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._http = ResilientHttp(
            "lmf",
            base_url=base_url.rstrip("/") + "/api/",
            timeout=5.0,
            retry=LMF_RETRY,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def auth_status(self) -> dict[str, Any]:
        """How LMF sees this client: {"password": bool, "access": "token" | "open" | "none"} (for `doctor`).
        An LMF without the login answers 404 (NotFoundError)."""
        data = _json(await self._http.get("auth"))
        if not isinstance(data, dict) or "access" not in data:
            raise PermanentError("lmf: unexpected auth status")
        return data

    async def bootstrap(self) -> dict[str, Any]:
        data = _json(await self._http.get("bootstrap"))
        if not isinstance(data, dict) or "groups" not in data or "categories" not in data:
            raise PermanentError("lmf: unexpected bootstrap")
        return data

    async def day(self, day: date) -> dict[str, Any]:
        return dict(_json(await self._http.get(f"day/{day.isoformat()}")))

    async def days(self, start: date, end: date) -> list[dict[str, Any]]:
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return list(_json(await self._http.get("days", params=params)))

    async def save_day(self, day: date, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(_json(await self._http.request("PUT", f"day/{day.isoformat()}", json=payload)))

    async def tpl(self, period: str) -> dict[str, Any]:
        return dict(_json(await self._http.get(f"tpl/{period}")))

    async def save_tpl(self, period: str, checks: dict[int, bool]) -> dict[str, Any]:
        body = {"checks": {str(item): bool(value) for item, value in checks.items()}}
        return dict(_json(await self._http.request("PUT", f"tpl/{period}", json=body)))

    async def add_supplement(self, name: str, label: str, unit: str, default_dose: float | None) -> dict[str, Any]:
        body = {"name": name, "label": label, "category": "other", "unit": unit, "default_dose": default_dose}
        return dict(_json(await self._http.request("POST", "supplements", json=body)))

    async def _food(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return _json(await self._http.request(method, f"food/{path}", **kwargs))
        except NotFoundError as exc:
            raise FoodNotSupportedError("LMF без раздела «Питание» — перезапустите сайт TLT в LocalSites") from exc

    async def food_products(self) -> list[dict[str, Any]]:
        return list(await self._food("GET", "products"))

    async def save_product(self, product: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._food("POST", "products", json=product))

    async def food_day(self, day: date) -> dict[str, Any]:
        return dict(await self._food("GET", f"day/{day.isoformat()}"))

    async def add_food(self, day: date, entries: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(await self._food("POST", f"day/{day.isoformat()}", json={"entries": entries}))

    async def delete_food(self, entry_id: int) -> dict[str, Any]:
        return dict(await self._food("DELETE", f"entry/{int(entry_id)}"))

    async def food_days(self, start: date, end: date) -> list[dict[str, Any]]:
        return list(await self._food("GET", "days", params={"from": start.isoformat(), "to": end.isoformat()}))

    async def food_targets(self) -> dict[str, Any]:
        return dict(await self._food("GET", "targets"))

    async def save_food_targets(self, targets: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._food("PUT", "targets", json=targets))

    async def labs(self) -> list[dict[str, Any]]:
        return list(_json(await self._http.get("labs")))

    async def save_labs(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(_json(await self._http.request("POST", "labs", json={"results": results})))


def _seed() -> dict[str, Any]:
    """A small copy of the LMF dictionaries (same ids/codes as the user's base) for tests and the demo."""
    groups = [
        (
            "short",
            "Short",
            [
                (1, "NS", "Nazal strips — носовые полоски"),
                (5, "Wlk", "Walking — прогулка"),
                (6, "GG", "Good Gaming — игры в удовольствие"),
                (11, "IS", "Ice Shower — холодный душ"),
                (36, "FC", "Facial Care — уход за лицом"),
            ],
        ),
        ("habits", "Habits", [(12, "CD", "Clean Desk — чистый стол"), (13, "LT", "Light therapy — светотерапия")]),
        (
            "routine",
            "Mr/Er",
            [
                (14, "Mr", "Morning routine — утренняя рутина"),
                (15, "Er", "Evening routine — вечерняя рутина"),
                (17, "FC", "Facial Care — уход за лицом"),
                (18, "BT", "Brush Teeth — чистка зубов"),
            ],
        ),
        ("work", "Work", [(19, "W", "Работа / смена"), (20, "W+", "Продуктивная работа")]),
        ("train", "Tr", [(23, "Tr", "Тренировка"), (24, "Tr+", "Тяжёлая тренировка")]),
        ("fatigue", "Fatigue", [(25, "F", "Лёгкое ощущение усталости"), (33, "ill", "Болезнь")]),
    ]
    categories = [
        (1, "Внешность", [(1, "Уход за лицом", 0.15), (6, "Тренировки", 0.3), (5, "Одежда(стиль)", 0.55)]),
        (2, "Здоровье", [(7, "Сон", 0.2), (12, "Медитация", 0.1), (13, "Отдых", 0.7)]),
        (
            3,
            "Привычки",
            [(14, "Утренняя рутина", 0.2), (17, "Тренировки", 0.15), (19, "Чтение", 0.15), (20, "Планирование", 0.5)],
        ),
        (4, "Интеллект", [(21, "Чтение книг/статей", 0.2), (25, "Изучение языков", 0.8)]),
        (6, "Отдых", [(34, "Качественный сон", 0.3), (37, "Медитация", 0.7)]),
    ]
    return {
        "groups": [
            {
                "id": gi,
                "key": key,
                "label": label,
                "sort": gi,
                "tags": [
                    {"id": tid, "group_id": gi, "code": code, "label": tl, "tone": "neutral", "active": 1}
                    for tid, code, tl in tags
                ],
            }
            for gi, (key, label, tags) in enumerate(groups, start=1)
        ],
        "supplements": [
            {"id": sid, "name": n, "label": lb, "category": cat, "unit": u, "default_dose": dd, "active": 1}
            for sid, n, lb, cat, u, dd in (
                (2, "Caffeine", "Кофеин", "stimulant", "мг", 200.0),
                (8, "Creatine", "Креатин", "adaptogen", "г", 5.0),
                (11, "Vit. B12", "Витамин B12", "longterm", "мкг", 500.0),
                (12, "Vit. D3", "Витамин D3", "longterm", "МЕ", 4000.0),
                (15, "Zinc", "Цинк", "longterm", "мг", 25.0),
                (16, "Omega-3", "Омега-3", "longterm", "", None),
                (23, "Magnésio", "Магний", "sedative", "мг", 300.0),
                (25, "Glycine", "Глицин", "sedative", "г", 0.2),
            )
        ],
        "categories": [
            {
                "id": idx,
                "idx": idx,
                "name": name,
                "minimum": 0.5,
                "target": 0.7,
                "wow": 0.9,
                "active": 1,
                "items": [
                    {"id": iid, "category_id": idx, "name": n, "weight": w, "sort": i, "active": 1}
                    for i, (iid, n, w) in enumerate(items)
                ],
            }
            for idx, name, items in categories
        ],
        "range": {"min": "2024-06-22", "max": "2026-04-02", "n": 186},
    }


@dataclass
class FakeLifeTracker:
    """In-memory LMF with the same merge semantics (tags/supplements replaced wholesale) and TPL percentages."""

    data: dict[str, Any] = field(default_factory=_seed)
    days_store: dict[str, dict[str, Any]] = field(default_factory=dict)
    checks: dict[str, dict[int, bool]] = field(default_factory=dict)
    unavailable: bool = False
    writes: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    food: list[dict[str, Any]] = field(default_factory=list)
    next_food_id: int = 1
    targets: dict[str, Any] = field(default_factory=dict)
    food_supported: bool = True  # False = an LMF started before the «Питание» version
    lab_rows: list[dict[str, Any]] = field(default_factory=list)
    lose_next_reply: bool = False  # the next save_day is written, then its reply is «lost» (TransientError)

    def _up(self) -> None:
        if self.unavailable:
            from jarvis.core.errors import TransientError

            raise TransientError("lmf: connect error (fake)")

    async def bootstrap(self) -> dict[str, Any]:
        self._up()
        return copy.deepcopy(self.data)

    def _day(self, key: str) -> dict[str, Any]:
        stored = self.days_store.get(key)
        base: dict[str, Any] = {"date": key, "tags": [], "supplements": [], "body": None}
        return copy.deepcopy({**base, **stored}) if stored else base

    async def day(self, day: date) -> dict[str, Any]:
        self._up()
        return self._day(day.isoformat())

    async def days(self, start: date, end: date) -> list[dict[str, Any]]:
        self._up()
        return [self._day(k) for k in sorted(self.days_store) if start.isoformat() <= k <= end.isoformat()]

    async def save_day(self, day: date, payload: dict[str, Any]) -> dict[str, Any]:
        self._up()
        key = day.isoformat()
        self.writes.append(("day", key, copy.deepcopy(payload)))
        current = self.days_store.setdefault(key, {"date": key, "tags": [], "supplements": [], "body": None})
        for name, value in payload.items():
            current[name] = copy.deepcopy(value)
        if self.lose_next_reply:
            from jarvis.core.errors import TransientError

            self.lose_next_reply = False
            raise TransientError("lmf: read timeout after the write (fake)")
        return self._day(key)

    async def tpl(self, period: str) -> dict[str, Any]:
        self._up()
        marks = self.checks.get(period, {})
        categories = []
        for c in self.data["categories"]:
            items = [{**i, "checked": 1 if marks.get(i["id"]) else 0} for i in c["items"]]
            total_weight = sum(i["weight"] for i in items)
            gained = sum(i["weight"] for i in items if i["checked"])
            pct = gained / total_weight if total_weight else 0.0
            categories.append({**c, "items": items, "pct": pct, "computed": pct, "source": "items"})
        total = sum(c["pct"] for c in categories) / len(categories) if categories else 0.0
        return {"period": period, "categories": categories, "total": total}

    async def add_supplement(self, name: str, label: str, unit: str, default_dose: float | None) -> dict[str, Any]:
        self._up()
        new_id = max([int(x["id"]) for x in self.data["supplements"]] + [100]) + 1
        row = {"id": new_id, "name": name, "label": label, "category": "other", "unit": unit,
               "default_dose": default_dose, "active": 1}  # fmt: skip
        self.data["supplements"].append(row)
        self.writes.append(("supplement", name, dict(row)))
        return dict(row)

    # --- food ------------------------------------------------------------------------------------------------

    def _food_up(self) -> None:
        self._up()
        if not self.food_supported:
            raise FoodNotSupportedError("LMF без раздела «Питание» — перезапустите сайт TLT в LocalSites")

    def _key(self, name: str) -> str:
        return " ".join(str(name).strip().lower().replace("ё", "е").split())

    async def food_products(self) -> list[dict[str, Any]]:
        self._food_up()
        return copy.deepcopy(sorted(self.products.values(), key=lambda p: p["name"]))

    async def save_product(self, product: dict[str, Any]) -> dict[str, Any]:
        self._food_up()
        key = self._key(product["name"])
        current = self.products.get(key)
        row = {"id": current["id"] if current else len(self.products) + 1, "key": key,
               "name": current["name"] if current else str(product["name"]).strip()}  # fmt: skip
        for field_name in ("kcal", "protein", "fat", "carbs", "portion_g", "portion_name"):
            value = product.get(field_name)
            row[field_name] = value if value is not None else (current or {}).get(field_name)
        row["source"] = product.get("source") or "user"
        self.products[key] = row
        self.writes.append(("product", key, dict(row)))
        return dict(row)

    def _food_day(self, key: str) -> dict[str, Any]:
        entries = [e for e in self.food if e["date"] == key]
        totals = {k: round(sum(float(e.get(k) or 0) for e in entries), 1) for k in ("kcal", "protein", "fat", "carbs")}
        return {"date": key, "entries": copy.deepcopy(entries), "totals": totals, "targets": self.targets or None}

    async def food_day(self, day: date) -> dict[str, Any]:
        self._food_up()
        return self._food_day(day.isoformat())

    async def add_food(self, day: date, entries: list[dict[str, Any]]) -> dict[str, Any]:
        self._food_up()
        key = day.isoformat()
        for entry in entries:
            if entry.get("ext_id") and any(e.get("ext_id") == entry["ext_id"] for e in self.food):
                continue
            product = next((p for p in self.products.values() if p["id"] == entry.get("product_id")), None)
            grams = entry.get("grams") if entry.get("grams") is not None else (product or {}).get("portion_g")
            row = {"id": self.next_food_id, "date": key, **entry, "grams": grams}
            self.next_food_id += 1
            for k in ("kcal", "protein", "fat", "carbs"):
                if row.get(k) is None and product and product.get(k) is not None and grams is not None:
                    row[k] = round(product[k] * grams / 100, 1)
            self.food.append(row)
            self.writes.append(("food", key, dict(row)))
        return self._food_day(key)

    async def delete_food(self, entry_id: int) -> dict[str, Any]:
        self._food_up()
        entry = next((e for e in self.food if e["id"] == entry_id), None)
        if entry is None:
            return {"ok": True}
        self.food.remove(entry)
        self.writes.append(("food_delete", entry["date"], dict(entry)))
        return self._food_day(entry["date"])

    async def food_days(self, start: date, end: date) -> list[dict[str, Any]]:
        self._food_up()
        keys = sorted({e["date"] for e in self.food if start.isoformat() <= e["date"] <= end.isoformat()})
        return [{"date": k, "n": len(self._food_day(k)["entries"]), **self._food_day(k)["totals"]} for k in keys]

    async def food_targets(self) -> dict[str, Any]:
        self._food_up()
        return dict(self.targets)

    async def save_food_targets(self, targets: dict[str, Any]) -> dict[str, Any]:
        self._food_up()  # like LMF: the named norms change (None clears one), the rest stay
        merged = {**self.targets, **targets}
        self.targets = {k: v for k, v in merged.items() if v is not None}
        return dict(self.targets)

    async def labs(self) -> list[dict[str, Any]]:
        self._up()
        return sorted(copy.deepcopy(self.lab_rows), key=lambda r: r["date"], reverse=True)

    async def save_labs(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        self._up()
        for row in results:
            key = (row["date"], row["name"], row["source"])
            self.lab_rows = [r for r in self.lab_rows if (r["date"], r["name"], r["source"]) != key]
            self.lab_rows.append(dict(row))
        return {"saved": len(results)}

    async def save_tpl(self, period: str, checks: dict[int, bool]) -> dict[str, Any]:
        self._up()
        self.writes.append(("tpl", period, {str(k): v for k, v in checks.items()}))
        self.checks.setdefault(period, {}).update(checks)
        return await self.tpl(period)
