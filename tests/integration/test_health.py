"""Watch and scale → Jarvis: signed payloads from the phone (Life Dashboard Companion) are stored once, the night's
sleep and new weighings are offered to TLT and written only after «✅ Записать». Fakes only."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from jarvis.api.app import create_health_app
from jarvis.bot.service import BotService
from jarvis.core.config import Settings
from jarvis.db.models import HealthDay, HealthPayload, HealthSample
from tests.conftest import make_settings

USER = 1001
SECRET = "phone-secret-123"
pytestmark = pytest.mark.db


@pytest.fixture
def settings() -> Settings:
    return make_settings(health_webhook_secret=SECRET)


async def post(c: Any, body: dict[str, Any], *, secret: str | None = SECRET) -> httpx.Response:
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers["x-signature"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=create_health_app(c))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/webhooks/health", content=raw, headers=headers)


def tap(data: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }


def buttons(c: Any) -> dict[str, str]:
    markup = c.telegram.sent[-1].get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


def stage(name: str, start: str, end: str, minutes: int) -> dict[str, Any]:
    return {"stage": name, "start_time": start, "end_time": end, "duration_seconds": minutes * 60}


# 22.09 23:30 → 23.09 07:30 in Tbilisi (UTC+4); the test clock is 23.09 09:00
NIGHT = {
    "uuid": "sleep-1",
    "session_end_time": "2026-09-23T03:30:00Z",
    "duration_seconds": 8 * 3600,
    "source": "com.sec.android.app.shealth",
    "stages": [
        stage("light", "2026-09-22T19:30:00Z", "2026-09-22T23:30:00Z", 240),
        stage("deep", "2026-09-22T23:30:00Z", "2026-09-23T00:30:00Z", 60),
        stage("rem", "2026-09-23T00:30:00Z", "2026-09-23T02:00:00Z", 90),
        stage("awake", "2026-09-23T02:00:00Z", "2026-09-23T02:30:00Z", 30),
        stage("light", "2026-09-23T02:30:00Z", "2026-09-23T03:30:00Z", 60),
    ],
}
HEART = [
    {"bpm": 50, "time": "2026-09-22T22:00:00Z", "uuid": "hr-1#1"},
    {"bpm": 60, "time": "2026-09-23T01:00:00Z", "uuid": "hr-1#2"},
    {"bpm": 90, "time": "2026-09-23T04:30:00Z", "uuid": "hr-1#3"},  # after waking: not «во сне»
]


def payload(sequence: int, **records: Any) -> dict[str, Any]:
    return {"timestamp": "2026-09-23T04:50:00Z", "app_version": "1.25.0", "source": "health_connect",
            "sequence": sequence, **records}  # fmt: skip


async def test_unsigned_or_unconfigured_requests_are_refused(container: Any) -> None:
    c = container
    assert (await post(c, payload(1), secret=None)).status_code == 401
    assert (await post(c, payload(1), secret="wrong")).status_code == 401
    assert (await post(c, {"hello": "world"})).status_code == 400
    c.health.settings = make_settings()  # no HEALTH_WEBHOOK_SECRET
    assert (await post(c, payload(1))).status_code == 503
    c.health.settings = make_settings(health_webhook_secret="")  # compose passes "" when .env has none
    assert (await post(c, payload(1), secret="")).status_code == 503  # an empty key would let anyone sign
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(HealthPayload)) == 0


async def test_a_payload_is_stored_once_and_read_by_the_job(container: Any, worker: Any) -> None:
    c = container
    body = payload(5, heart_rate=HEART, daily_totals=[{"date": "2026-09-23", "steps": 3120, "distance_meters": 2210.0}])
    assert (await post(c, body)).status_code == 204
    assert (await post(c, body)).status_code == 204  # the app sends it again after a timeout: harmless
    assert (await post(c, {"source": "health_connect", "test": True})).status_code == 204  # the pairing ping
    await worker.drain()
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(HealthSample)) == 3
        day = await s.get(HealthDay, date(2026, 9, 23))
        assert day is not None and (day.steps, day.sequence) == (3120, 5)
        assert await s.scalar(select(func.count()).where(HealthPayload.processed_at.is_(None))) == 0
    # a late payload with an older sequence does not overwrite a newer daily total
    await post(c, payload(4, daily_totals=[{"date": "2026-09-23", "steps": 1000}]))
    await worker.drain()
    async with c.db.session() as s:
        day = await s.get(HealthDay, date(2026, 9, 23))
        assert day is not None and day.steps == 3120
    # a record deleted in Health Connect (and its heart-rate samples) goes away
    await post(c, payload(6, deleted_records=[{"type": "heart_rate", "uuid": "hr-1"}]))
    await worker.drain()
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(HealthSample)) == 0


async def test_a_deleted_night_changes_its_own_day(container: Any) -> None:
    # L1 (review 17.10): deleting a night always marked TODAY's night as changed.
    c = container
    assert (await post(c, payload(1, sleep=[NIGHT]))).status_code == 204
    async with c.db.session() as s:
        assert (await c.health.ingest(s, c.clock.now())).nights == {date(2026, 9, 23)}
    c.clock.advance(timedelta(days=7))
    assert (await post(c, payload(2, deleted_records=[{"type": "sleep", "uuid": "sleep-1"}]))).status_code == 204
    async with c.db.session() as s:
        done = await c.health.ingest(s, c.clock.now())
    assert (done.nights, done.deleted) == ({date(2026, 9, 23)}, 1)


async def test_the_night_is_offered_to_tlt_once(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await post(c, payload(1, sleep=[NIGHT], heart_rate=HEART))
    await worker.drain()
    assert c.telegram.texts()[-1].splitlines() == [
        "⌚ Сон с часов: 7:30 (23:30–07:30) · глубокий 1:00 · REM 1:30 · просыпались 0:30 · пульс во сне ~55 (мин. 50)",
        "",
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• Сон: 7:30",
    ]
    assert list(buttons(c)) == ["✅ Записать", "✖️ Не надо"]
    assert c.lmf_api.writes == []  # nothing without the button
    sent = len(c.telegram.sent)
    await post(c, payload(2, heart_rate=[{"bpm": 70, "time": "2026-09-23T04:40:00Z", "uuid": "hr-2#1"}]))
    await worker.drain()
    assert len(c.telegram.sent) == sent  # offered once a day
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 10))
    await worker.drain()
    assert c.lmf_api.days_store["2026-09-23"]["sleep_minutes"] == 450


async def test_no_offer_when_sleep_is_in_tlt_or_the_night_is_short(container: Any, worker: Any) -> None:
    c = container
    c.lmf_api.days_store["2026-09-23"] = {"date": "2026-09-23", "sleep_minutes": 420, "tags": [], "supplements": []}
    await post(c, payload(1, sleep=[NIGHT]))
    await worker.drain()
    assert c.telegram.sent == []
    c.lmf_api.days_store.clear()
    nap = {**NIGHT, "uuid": "nap", "duration_seconds": 2 * 3600, "stages": []}
    await post(c, payload(2, deleted_records=[{"type": "sleep", "uuid": "sleep-1"}], sleep=[nap]))
    await worker.drain()
    assert c.telegram.sent == []  # 2 h is a nap, not a night


async def test_a_weighing_is_offered_and_merged_into_the_day_body(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    c.lmf_api.days_store["2026-09-23"] = {
        "date": "2026-09-23", "tags": [], "supplements": [], "body": {"date": "2026-09-23", "muscle_kg": 55.7},
    }  # fmt: skip
    await post(
        c,
        payload(
            1,
            weight=[
                {
                    "kilograms": 70.2,
                    "time": "2026-09-20T04:00:00Z",
                    "uuid": "w-old",
                    "source": "com.health.openscale.sync",
                }
            ],
        ),
    )
    await worker.drain()
    assert c.telegram.sent == []  # three days old (a backfill): stored, not offered
    await post(
        c,
        payload(
            2,
            weight=[
                {
                    "kilograms": 69.8,
                    "time": "2026-09-23T04:10:00Z",
                    "uuid": "w-new",
                    "source": "com.health.openscale.sync",
                }
            ],
            body_fat=[
                {
                    "percentage": 14.9,
                    "time": "2026-09-23T04:10:00Z",
                    "uuid": "f-new",
                    "source": "com.health.openscale.sync",
                }
            ],
            height=[{"meters": 1.8, "time": "2026-01-01T00:00:00Z", "uuid": "h"}],
        ),
    )
    await worker.drain()
    assert c.telegram.texts()[-1].splitlines() == [
        "⌚ Весы: 69,8 кг · жир 14,9 % (−0,4 кг с 20.09) в 08:10",
        "",
        "📓 Записать в TLT · сегодня, ср 23.09?",
        "• ⚖️ Тело: 69,8 кг · жир 14,9 % · ИМТ 21,5",
    ]
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 20))
    await worker.drain()
    body = c.lmf_api.days_store["2026-09-23"]["body"]
    assert (body["weight"], body["fat_pct"], body["fat_kg"], body["bmi"], body["muscle_kg"]) == (
        69.8, 14.9, 10.4, 21.5, 55.7,
    )  # fmt: skip
    assert "date" not in body


async def test_old_payloads_do_not_offer_yesterday(container: Any, worker: Any) -> None:
    c = container
    c.clock.advance(timedelta(days=1))  # 24.09: last night's data is for 23.09 → not offered today
    await post(c, payload(1, sleep=[NIGHT]))
    await worker.drain()
    assert c.telegram.sent == []


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


async def test_what_the_watch_says_in_chat_and_in_the_evening(container: Any, worker: Any) -> None:
    c = container
    c.settings.health_notify_sleep = False
    bot = BotService(c)
    await bot.handle_update(msg("какой у меня пульс", 30))
    assert c.telegram.texts()[-1].splitlines()[1] == "С телефона пока ничего не приходило."
    await post(
        c,
        payload(
            1,
            sleep=[NIGHT],
            heart_rate=HEART,
            daily_totals=[{"date": "2026-09-23", "steps": 8421, "distance_meters": 6210.4}],
            weight=[
                {"kilograms": 69.8, "time": "2026-09-23T04:10:00Z", "uuid": "w1", "source": "com.health.openscale.sync"}
            ],
        ),
    )
    await worker.drain()
    await bot.handle_update(msg("какой у меня пульс", 31))
    assert c.telegram.texts()[-1].splitlines() == [
        "⌚ Часы и весы · сегодня, ср 23.09",
        "• 🛌 Сон: 7:30 (23:30–07:30) · глубокий 1:00 · REM 1:30 · просыпались 0:30 · пульс во сне ~55 (мин. 50)",
        "• 👣 Шаги: 8 421 · 6,2 км",
        "• ❤️ Пульс: 90 в 08:30",
        "• ⚖️ Вес: 69,8 кг (23.09)",
    ]
    assert c.llm.calls == []
    async with c.db.session() as s:
        line = await c.health.evening_line(s, c.clock.now())
    assert line == "⌚ 👣 8 421 шагов · 🛌 сон 7:30 · ❤️ во сне ~55"


async def test_pairing_page_keeps_the_secret_out_of_the_output(
    container: Any, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from jarvis.cli import cmd_health_pair

    page = tmp_path / "pair.html"
    args = argparse.Namespace(host="192.168.1.10", port=8091, out=str(page))
    assert cmd_health_pair(container.settings, args) == 0
    html = page.read_text(encoding="utf-8")
    assert "life-dashboard-companion-app/pair#v=1&url=http%3A%2F%2F192.168.1.10%3A8091%2Fwebhooks%2Fhealth" in html
    assert f"secret={SECRET}&name=Jarvis&sources=health_connect" in html
    assert page.stat().st_mode & 0o777 == 0o600
    assert SECRET not in capsys.readouterr().out
    # the only external script is pinned (security review 2026-10-14, L3)
    assert html.count("<script src=") == 1
    assert 'integrity="sha384-3zSEDfvllQohrq0PHL1fOXJuC/jSOO34H46t6UQfobFOmxE5BpjjaIJY5F2/bMnU"' in html
    assert 'crossorigin="anonymous"' in html


# --- security review of e0dca6a: the body cap holds for chunked bodies; replays are harmless -----------------------


async def test_a_chunked_body_over_the_cap_is_refused_while_reading(container: Any, monkeypatch: Any) -> None:
    import jarvis.health.service as health_service

    monkeypatch.setattr(health_service, "MAX_PAYLOAD_BYTES", 1000)

    async def chunks() -> Any:  # no Content-Length: sent as chunked transfer
        for _ in range(50):
            yield b"x" * 100

    transport = httpx.ASGITransport(app=create_health_app(container))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/webhooks/health", content=chunks(), headers={"x-signature": "sha256=00"})
    assert response.status_code == 413


async def test_replayed_requests_are_idempotent(container: Any) -> None:
    from jarvis.db.models import UserPreference

    c = container
    for _ in range(3):  # a captured test ping sent again and again
        assert (await post(c, {"source": "health_connect", "test": True})).status_code == 204
    assert (await post(c, {"source": "screen_time", "sequence": 9, "screen_time": []})).status_code == 204
    assert (await post(c, {"source": "screen_time", "sequence": 9, "screen_time": []})).status_code == 204
    assert (await post(c, {"source": "health_connect", "weight": []})).status_code == 400  # no sequence
    for _ in range(3):
        assert (await post(c, payload(11, heart_rate=HEART))).status_code == 204
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(HealthPayload)) == 2  # screen 9 and data 11, once each
        assert await s.scalar(select(func.count()).select_from(UserPreference)) == 1
        assert await c.health.last_received(s) is not None


# --- security review 2026-10-14 (L1): the LAN intake reads few bodies at once and none for long ---------------------


async def test_too_many_bodies_at_once_get_503_and_slots_come_back(container: Any) -> None:
    import asyncio

    import jarvis.api.app as api_app

    app = create_health_app(container)
    gate = app.state.health_gate
    release = asyncio.Event()

    async def held() -> Any:  # a body that arrives only when released
        yield b'{"source": "health_connect", '
        await release.wait()
        yield b'"test": true}'

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        slow = [
            asyncio.create_task(client.post("/webhooks/health", content=held(), headers={"x-signature": "sha256=00"}))
            for _ in range(api_app.HEALTH_MAX_IN_FLIGHT)
        ]
        for _ in range(200):
            if gate.count == api_app.HEALTH_MAX_IN_FLIGHT:
                break
            await asyncio.sleep(0.01)
        assert gate.count == api_app.HEALTH_MAX_IN_FLIGHT
        busy = await client.post("/webhooks/health", content=b"{}", headers={"x-signature": "sha256=00"})
        assert busy.status_code == 503 and busy.headers["retry-after"] == "30"
        release.set()
        assert [r.status_code for r in await asyncio.gather(*slow)] == [401] * api_app.HEALTH_MAX_IN_FLIGHT
        assert gate.count == 0
    assert (await post(container, {"source": "health_connect", "test": True})).status_code == 204


async def test_a_slow_body_is_cut_off(container: Any, monkeypatch: Any) -> None:
    import asyncio

    import jarvis.api.app as api_app

    monkeypatch.setattr(api_app, "HEALTH_READ_TIMEOUT_SECONDS", 0.2)

    async def trickle() -> Any:
        yield b"{"
        await asyncio.sleep(5)
        yield b"}"

    app = create_health_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/webhooks/health", content=trickle(), headers={"x-signature": "sha256=00"})
    assert response.status_code == 408
    assert app.state.health_gate.count == 0


async def test_only_the_scale_is_offered_and_bmi_uses_the_height_setting(container: Any, worker: Any) -> None:
    c = container
    c.settings.health_height_cm = 175
    fit = "com.google.android.apps.fitness"
    await post(
        c,
        payload(
            1,
            weight=[{"kilograms": 79.3, "time": "2026-09-23T00:26:00Z", "uuid": "fit-w", "source": fit}],
            body_fat=[{"percentage": 24.0, "time": "2026-09-23T04:10:00Z", "uuid": "fit-f", "source": fit}],
        ),
    )
    await worker.drain()
    assert c.telegram.sent == []  # Google Fit's profile weight: stored, not offered
    scale = "com.health.openscale.sync"
    await post(
        c,
        payload(
            2,
            weight=[{"kilograms": 80.0, "time": "2026-09-23T04:10:00Z", "uuid": "s-w", "source": scale}],
            body_fat=[{"percentage": 21.4, "time": "2026-09-23T04:10:00Z", "uuid": "s-f", "source": scale}],
        ),
    )
    await worker.drain()
    lines = c.telegram.texts()[-1].splitlines()
    assert lines[0] == "⌚ Весы: 80 кг · жир 21,4 % в 08:10"  # no «с …»: Google Fit's 79,3 is not a weighing
    assert lines[-1] == "• ⚖️ Тело: 80 кг · жир 21,4 % · ИМТ 26,1"  # 80 / 1,75² — and not Fit's 24 % at that minute
    async with c.db.session() as s:
        assert await s.scalar(select(func.count()).where(HealthSample.kind == "weight")) == 2  # both stored


async def test_the_morning_offer_carries_the_personal_quality_estimate(container: Any, worker: Any) -> None:
    from jarvis.health.quality import QualityModel

    c = container
    model = QualityModel(
        curves={
            "tst": [(300.0, 10.0), (450.0, 30.0)],
            "deep": [(20.0, 6.0), (60.0, 13.0)],
            "rem": [(30.0, 10.0), (90.0, 21.0)],
            "wake": [(10.0, 8.0), (60.0, 5.0)],
        },
        latency=16,
        nights=357,
        mae=6.4,
    )
    async with c.db.session() as s:
        await c.health.save_quality_model(s, model)
    await post(c, payload(1, sleep=[NIGHT]))
    await worker.drain()
    lines = c.telegram.texts()[-1].splitlines()
    # 7:30 asleep → 30, deep 1:00 → 13, REM 1:30 → 21, awake 0:30 → 6.8, + 16 = 86.8
    assert lines[0].endswith("· просыпались 0:30 · качество ≈87")
    assert lines[-1] == "• Сон: 7:30, качество 87"


# --- 2026-10-14: waist, weigh-in reminders, the week in numbers --------------------------------------------------


async def test_waist_goes_to_the_day_body(container: Any, worker: Any) -> None:
    c = container
    bot = BotService(c)
    await bot.handle_update(msg("талия девяносто четыре", 90))
    assert c.telegram.texts()[-1].splitlines() == ["📓 Записать в TLT · сегодня, ср 23.09?", "• ⚖️ Тело: талия 94 см"]
    await bot.handle_update(tap(buttons(c)["✅ Записать"], 91))
    await worker.drain()
    assert c.lmf_api.days_store["2026-09-23"]["body"] == {"waist_cm": 94.0}


async def test_morning_body_lines_by_weekday(container: Any, worker: Any) -> None:
    from types import SimpleNamespace

    from jarvis.jobs.handlers import _morning_body_lines

    c = container
    scale = "com.health.openscale.sync"
    week_ago = [{"date": f"2026-09-{d:02d}", "steps": 2000} for d in range(13, 20)]  # the week before: 2 000 a day
    this_week = [{"date": f"2026-09-{d:02d}", "steps": 3000} for d in range(20, 27)]
    await post(
        c,
        payload(
            1,
            daily_totals=week_ago + this_week,
            weight=[
                {"kilograms": 93.4, "time": "2026-09-18T04:00:00Z", "uuid": "w1", "source": scale},
                {"kilograms": 93.0, "time": "2026-09-26T04:00:00Z", "uuid": "w2", "source": scale},
            ],
        ),
    )
    await worker.drain()

    def ctx(day: str) -> Any:
        return SimpleNamespace(container=c, now=datetime.fromisoformat(f"{day}T05:00:00+00:00"))

    # Sunday 27.09: the week 20–26.09 against 13–19.09
    assert await _morning_body_lines(ctx("2026-09-27")) == ["📈 Неделя: шаги 3 000/день (+1 000) · вес 93 кг (−0,4)"]
    # Monday 28.09: weighed on Saturday → no reminder; Thursday 01.10: quiet for 6 days → weigh; and the 1st → waist
    assert await _morning_body_lines(ctx("2026-09-28")) == []
    assert await _morning_body_lines(ctx("2026-10-01")) == [
        "⚖️ Сегодня взвешивание: openScale → значок Bluetooth → встать на весы.",
        "📏 Начало месяца — замерьте талию и скажите: «талия 94».",
    ]


async def test_screen_time_newest_payload_wins_per_day(container: Any, worker: Any) -> None:
    from jarvis.db.models import ScreenDay

    c = container

    def screen(sequence: int, minutes: int) -> dict[str, Any]:
        return {
            "source": "screen_time",
            "sequence": sequence,
            "device": "Pixel 9 Pro",
            "screen_time": [
                {
                    "date": "2026-09-23",
                    "total_screen_time_minutes": minutes,
                    "apps": [
                        {"package": "org.telegram", "name": "Telegram", "minutes": 50},
                        {"package": "com.google.youtube", "name": "YouTube", "minutes": 90},
                    ],
                }
            ],
        }

    await post(c, screen(5, 180))
    await post(c, screen(4, 60))  # an older payload delivered late: ignored for that day
    await worker.drain()
    async with c.db.session() as s:
        day = await s.get(ScreenDay, date(2026, 9, 23))
        assert day is not None and day.minutes == 180
        assert [a["name"] for a in day.apps] == ["YouTube", "Telegram"]
        line = await c.health.evening_line(s, c.clock.now())
    assert line == "⌚ 📱 экран 3:00 (YouTube 1:30, Telegram 0:50)"
