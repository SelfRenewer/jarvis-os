"""Durable job handlers. Each handler is idempotent or guarded by a ledger."""

from __future__ import annotations

import contextlib
import functools
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text, update

from jarvis.bot import fmt
from jarvis.core.clock import local_datetime, parse_hhmm, to_local
from jarvis.core.errors import AuthError, NotConfiguredError, TransientError
from jarvis.db.models import (
    Approval,
    CalendarEvent,
    Idea,
    PendingChoice,
    Reminder,
    ScheduledJob,
    TelegramUpdate,
    WebhookDelivery,
)
from jarvis.integrations.telegram.client import InlineButton, inline_keyboard
from jarvis.reminders.policy import Channel, LateAction
from jarvis.reminders.route_link import reminder_route_markup
from jarvis.reminders.service import PlanStats, build_fire_decision
from jarvis.scheduler.runner import HandlerRegistry, JobContext, SkipJob

log = logging.getLogger(__name__)


def approval_keyboard(
    nonce: str, approve_text: str = "✅ Применить", reject_text: str = "✖️ Отклонить"
) -> dict[str, Any]:
    return inline_keyboard([[InlineButton(approve_text, f"ap:a:{nonce}"), InlineButton(reject_text, f"ap:r:{nonce}")]])


async def _auth_alert(ctx: JobContext, provider: str, exc: Exception) -> None:
    """Revoked/invalid credentials: alert once per day instead of dead-lettering every periodic run."""
    day = ctx.now.date().isoformat()
    hint = {"google": "запусти `jarvis google-auth`", "todoist": "обнови TODOIST_API_TOKEN"}.get(provider, "")
    with contextlib.suppress(Exception):
        await ctx.container.notifier.send(
            f"🔐 {provider}: авторизация недействительна ({exc}). {hint}".strip(),
            dedupe_key=f"auth-alert:{provider}:{day}",
        )
    raise SkipJob(f"{provider} authorization failed") from exc


async def h_todoist_refresh(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    if c.todoist_sync is None:
        raise SkipJob("todoist not configured")
    try:
        async with c.db.session() as session:
            outcome = await c.todoist_sync.refresh_task(session, str(ctx.payload["task_id"]), ctx.now)
    except AuthError as exc:
        await _auth_alert(ctx, "todoist", exc)
    return {"outcome": outcome}


async def h_todoist_reconcile(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    if c.todoist_sync is None:
        raise SkipJob("todoist not configured")
    try:
        async with c.db.session() as session:
            return dict(await c.todoist_sync.reconcile(session, ctx.now))
    except AuthError as exc:
        await _auth_alert(ctx, "todoist", exc)
        raise


async def h_calendar_reconcile(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    if c.calendar_sync is None:
        raise SkipJob("google calendar not configured")
    try:
        async with c.db.session() as session:
            return dict(await c.calendar_sync.reconcile(session, ctx.now))
    except AuthError as exc:
        await _auth_alert(ctx, "google", exc)
        raise


async def h_calendar_channels(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    if c.calendar_sync is None:
        raise SkipJob("google calendar not configured")
    async with c.db.session() as session:
        return dict(await c.calendar_sync.ensure_channels(session, ctx.now))


async def h_calendar_mirror(ctx: JobContext) -> dict[str, Any]:
    from jarvis.sync.calendar_sync import mirror_tasks

    c = ctx.container
    if c.calendar is None:
        raise SkipJob("google calendar not configured")
    async with c.db.session() as session:
        return dict(await mirror_tasks(session, c.calendar, c.settings, ctx.now))


async def h_reminders_plan(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    async with c.db.session() as session:
        stats = await c.reminders.plan_upcoming(session, ctx.now)
    return stats.__dict__


async def h_refresh_travel(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    async with c.db.session() as session:
        event = await session.get(CalendarEvent, uuid.UUID(ctx.payload["event_uuid"]))
        if event is None or event.status == "cancelled" or event.start_at is None or event.start_at <= ctx.now:
            raise SkipJob("event gone or started")
        stats = PlanStats()
        await c.reminders.plan_event(session, event, ctx.now, stats, force_travel_refresh=True)
    return stats.__dict__


async def _refresh_reminded_tasks(
    ctx: JobContext, reminder_id: uuid.UUID, statuses: tuple[str, ...] = ("scheduled", "firing")
) -> None:
    """Right before a reminder: re-read its Todoist task(s) — the reminder task, or the open tasks linked to the
    event — so an item completed in the Todoist app / ChatGPT minutes ago is never reminded (polling lags)."""
    from jarvis.reminders.service import SOURCE_TASK
    from jarvis.sync.links import linked_tasks

    c = ctx.container
    if c.todoist_sync is None:
        return
    async with c.db.session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None or reminder.status not in statuses:
            return
        if reminder.source_type == SOURCE_TASK:
            task_ids = [reminder.source_id]
        else:
            event = await session.get(CalendarEvent, uuid.UUID(reminder.payload["event_uuid"]))
            links = await linked_tasks(session, event, c.settings.tz) if event is not None else []
            task_ids = [t.id for t in links if not t.checked]
    for task_id in task_ids[:5]:
        try:
            async with c.db.session() as session:
                await c.todoist_sync.refresh_task(session, task_id, ctx.now)
        except Exception as exc:  # the cached state is used; a reminder must not fail because of this check
            log.warning("pre-reminder Todoist refresh failed for %s: %s", task_id, exc)


async def h_reminder_fire(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    reminder_id = uuid.UUID(ctx.payload["reminder_id"])
    await _refresh_reminded_tasks(ctx, reminder_id)
    async with c.db.session() as session:
        decision = await build_fire_decision(session, reminder_id, ctx.now, c.settings)
    if decision is None:
        raise SkipJob("reminder no longer scheduled")
    reminder = decision.reminder
    if decision.event is None:
        raise SkipJob("event cancelled or moved")
    outcome: str
    if decision.action == LateAction.MISSED:
        event = decision.event
        if event.end_at is not None and ctx.now < event.end_at:
            await c.notifier.send(
                decision.text, dedupe_key=f"missed:{reminder.source_id}:{reminder.occurrence_start.isoformat()}"
            )
            outcome = "missed_notice_sent"
        else:
            outcome = "missed_silently (event over)"
        final = "missed"
    elif decision.action == LateAction.RECOVERY_NOTICE:
        route = await reminder_route_markup(c, decision, ctx.now)  # «🧭 Маршрут в 2ГИС» when both ends are known
        await c.notifier.send(decision.text, dedupe_key=f"reminder:{reminder.id}", reply_markup=route)
        outcome, final = "recovery_notice_sent", "sent"
        if await _mirror_to_mac(c, decision.text, ctx.now, f"reminder:{reminder.id}"):
            outcome += "+mac"
    elif reminder.channel == Channel.CALL:
        call = await c.call_service.request_call(
            idempotency_key=f"call:{reminder.id}",
            reminder_id=reminder.id,
            variables=decision.variables,
            spoken_text=decision.text,
            fallback_text=decision.text,
        )
        outcome, final = f"call_{call.status}" + ("+telegram_fallback" if call.fallback_sent else ""), "sent"
    else:
        from jarvis.reminders.escalation import buttons, schedule_repeats

        escalate = c.settings.reminder_escalation_enabled
        route = await reminder_route_markup(c, decision, ctx.now)  # «🧭 Маршрут в 2ГИС» when both ends are known
        rows = (buttons(reminder.id)["inline_keyboard"] if escalate else []) + (route or {}).get("inline_keyboard", [])
        markup = {"inline_keyboard": rows} if rows else None
        await c.notifier.send(decision.text, dedupe_key=f"reminder:{reminder.id}", reply_markup=markup)
        outcome, final = "telegram_sent", "sent"
        if await _mirror_to_mac(c, decision.text, ctx.now, f"reminder:{reminder.id}"):
            outcome += "+mac"
        if escalate:  # repeated until «👌 Понял» (the Bot API cannot call — this is the «call» that reminds)
            async with c.db.session() as session:
                row = await session.get(Reminder, reminder.id)
                if row is not None:
                    row.payload = {**(row.payload or {}), "text": str(decision.text),
                                   "html": isinstance(decision.text, fmt.Html)}  # fmt: skip
                    await schedule_repeats(session, row, ctx.now, c.settings)
    async with c.db.session() as session:
        row = await session.get(Reminder, reminder.id)
        if row is not None:
            row.status = final
            row.outcome = outcome
    from jarvis.audit.service import A, audit

    async with c.db.session() as session:
        await audit(
            session,
            now=ctx.now,
            actor="reminders",
            action=A.REMINDER_MISSED if final == "missed" else A.REMINDER_FIRED,
            target_type="reminder",
            target_id=str(reminder.id),
            details={"outcome": outcome},
        )
    return {"outcome": outcome}


async def h_reminder_repeat(ctx: JobContext) -> dict[str, Any]:
    """A persistent reminder: repeat (with a sound) until «👌 Понял», the task is done or the event is cancelled."""
    from jarvis.bot.speech import to_speech
    from jarvis.reminders.escalation import buttons, still_relevant

    c = ctx.container
    reminder_id = uuid.UUID(ctx.payload["reminder_id"])
    step = ctx.payload.get("step")
    # Done in the Todoist app a minute ago? Re-read the task (or the event's linked tasks) like before the first one.
    await _refresh_reminded_tasks(ctx, reminder_id, statuses=("sent",))
    async with c.db.session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None:
            raise SkipJob("reminder gone")
        payload = dict(reminder.payload or {})
        if step != "snooze" and payload.get("chain_stopped_at"):
            raise SkipJob("snoozed: the chain was replaced")
        why = await still_relevant(session, reminder, ctx.now, c.settings.tz)
    text = payload.get("text")
    if why or not text:
        raise SkipJob(why or "no text stored")
    head = (
        "⏰ Как просили — напоминаю" if step == "snooze" else f"🔔 Напоминаю ещё раз ({step}/{ctx.payload.get('of')})"
    )
    body = fmt.Html(text) if payload.get("html") else fmt.esc(text)
    spoken = fmt.plain(text)
    call = await _escalate_to_call(ctx, reminder_id, step, spoken)
    message = fmt.lines(fmt.b(head), body, fmt.hint("📞 Звоню в Telegram") if call == "queued" else None)
    with contextlib.suppress(NotConfiguredError):
        key = f"reminder-repeat:{reminder_id}:{step}" if step != "snooze" else f"reminder-repeat:{ctx.job.id}"
        await c.notifier.send(message, dedupe_key=key, reply_markup=buttons(reminder_id))
    voice = False
    if step == 1 and c.settings.reminder_escalation_voice and c.telegram is not None and c.notifier.default_chat_id:
        speech = to_speech(f"Сэр, напоминаю. {spoken}", c.settings.tts_max_chars)
        if speech:  # a voice note rings differently; it never carries buttons
            try:
                audio = await c.tts.synthesize(speech)
                await c.telegram.send_voice(
                    c.notifier.default_chat_id, audio.data, filename=audio.filename, mime_type=audio.mime_type
                )
                voice = True
            except Exception as exc:  # budget / provider: the text repeat went out anyway
                log.warning("reminder voice repeat failed: %s", exc)
    mac = await _mirror_to_mac(c, f"🔔 {spoken}", ctx.now, key)
    return {"step": step, "voice": voice, "mac": mac, "call": call}


async def _escalate_to_call(ctx: JobContext, reminder_id: uuid.UUID, step: Any, spoken: str) -> str | None:
    """The repeat `TELEGRAM_CALL_AT_REPEAT` without «Понял» → a Telegram call (once per reminder, not at night)."""
    from jarvis.calls.service import enabled, in_sleep_hours, request

    c = ctx.container
    at = c.settings.telegram_call_at_repeat
    if step == "snooze" or not at or step != at or not enabled(c.settings):
        return None
    if in_sleep_hours(ctx.now, c.settings):
        return "night"
    async with c.db.session() as session:
        return await request(
            session, c.settings, key=f"tg-call:reminder:{reminder_id}", text=f"Сэр, напоминаю. {spoken}",
            at=ctx.now, now=ctx.now, reminder_id=reminder_id,
        )  # fmt: skip


async def h_planner_daily(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    async with c.db.session() as session:
        outcome = await c.planner.propose(session, ctx.now, requested_by="scheduler")
    if outcome.proposal_id is None:
        raise SkipJob("nothing to plan")
    markup = approval_keyboard(outcome.approval_nonce) if outcome.approval_nonce else None
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(outcome.text, dedupe_key=f"plan:{outcome.proposal_id}", reply_markup=markup)
    return {"proposal_id": str(outcome.proposal_id), "scheduled": outcome.scheduled}


def _require_fresh_slot(ctx: JobContext, at: str) -> None:
    """A briefing is useful only near its time: after a long sleep/outage it is skipped, not sent late."""
    slot = ctx.payload.get("slot")
    if not slot:
        return  # manual `jarvis run-job …`
    settings = ctx.container.settings
    scheduled = local_datetime(date.fromisoformat(str(slot)), parse_hhmm(at), settings.tz)
    if ctx.now - scheduled > timedelta(minutes=settings.briefing_grace_minutes):
        raise SkipJob(f"briefing for {slot} is stale (now {ctx.now.isoformat()})")


async def _refresh_caches(ctx: JobContext) -> None:
    """Best effort: fresh Todoist/Calendar data before a briefing; on failure the cached data is used."""
    c = ctx.container
    for name, sync in (("todoist", c.todoist_sync), ("calendar", c.calendar_sync)):
        if sync is None:
            continue
        try:
            async with c.db.session() as session:
                await sync.reconcile(session, ctx.now)
        except Exception as exc:
            log.warning("briefing: %s refresh failed, using cache: %s", name, exc)


async def h_briefing_morning(ctx: JobContext) -> dict[str, Any]:
    """08:00: the day (events, tasks, overdue, deadlines, leave time) + the daily plan proposal, one message."""
    from jarvis.agenda.service import calendar_connected

    c = ctx.container
    _require_fresh_slot(ctx, c.settings.briefing_morning_at)
    await _refresh_caches(ctx)
    from jarvis.agenda.service import attention_block

    address = c.settings.persona_address if c.settings.persona_enabled else None
    async with c.db.session() as session:
        brief = await c.agenda.morning_brief(
            session, ctx.now, calendar_connected=calendar_connected(c.status), address=address
        )
        attention = await c.agenda.attention(session, ctx.now, limit=3)
    weather = c.weather.today_text(forecast, ctx.now) if (forecast := await c.weather.forecast()) else None
    async with c.db.session() as session:
        med_plan = await c.meds.today(session, ctx.now)
    meds_line = None
    if med_plan:
        by_med: dict[str, list[str]] = {}
        for med, at, _status in med_plan:
            by_med.setdefault(med.name, []).append(at.astimezone(c.settings.tz).strftime("%H:%M"))
        meds_line = fmt.line(
            "💊", fmt.b("Сегодня:"), "; ".join(f"{name} {', '.join(times)}" for name, times in by_med.items())
        )
    lines, suggestion_rows = attention_block(attention)
    sleep_line = None
    if c.settings.tracker_reminders_enabled:
        sleep_line = await c.tracker.morning_line(ctx.now)
        if sleep_line and await _sent(ctx, f"health:sleep:{c.tracker.today(ctx.now).isoformat()}"):
            sleep_line = "🛌 Сон с часов уже пришёл — нажмите «✅ Записать» в сообщении «⌚ Сон с часов»."
    rows: list[list[dict[str, str]]] = inline_keyboard(suggestion_rows)["inline_keyboard"]
    body_lines = await _morning_body_lines(ctx)
    shop_lines, shop_rows = await _morning_shopping(ctx)
    rows += shop_rows
    async with c.db.session() as session:
        outcome = await c.planner.propose(session, ctx.now, requested_by="scheduler")
    plan = None
    if outcome.proposal_id is not None:
        plan = fmt.headed(outcome.text)
        if outcome.approval_nonce:  # plan buttons first, then the per-task suggestions
            rows = approval_keyboard(outcome.approval_nonce)["inline_keyboard"] + rows
    text_msg = fmt.blocks(
        fmt.lines(brief.greeting, weather),
        brief.day,
        brief.deadlines,
        brief.leave,
        meds_line,
        brief.tomorrow,
        brief.note,
        lines or None,
        sleep_line,
        body_lines or None,
        shop_lines or None,
        plan,
    )
    markup = {"inline_keyboard": rows} if rows else None
    key = f"briefing:morning:{ctx.payload.get('slot') or ctx.job.id}"
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(text_msg, dedupe_key=key, reply_markup=markup)
    return {"proposal_id": str(outcome.proposal_id) if outcome.proposal_id else None}


async def _morning_shopping(ctx: JobContext) -> tuple[list[str], list[list[dict[str, str]]]]:
    """What is on the shopping list, and purchases still sitting in the tasks (one button moves them)."""
    from jarvis.shopping.service import is_purchase_task

    c = ctx.container
    lines: list[str] = []
    rows: list[list[dict[str, str]]] = []
    async with c.db.session() as session:
        items = await c.shopping.open_items(session)
        active = await c.task_ops.active(session)
        purchases = [t for t in active if is_purchase_task(t.content, t.due_is_recurring)]
        if items:
            lines.append(f"🛒 В списке покупок: {len(items)} — скажите «иду в магазин», и я покажу список.")
        if purchases and c.todoist is not None:
            move = await c.task_ops.request_op(session, op="to_shopping", tasks=purchases, now=ctx.now, chat_id=None)
            names = ", ".join(t.content for t in purchases[:4]) + ("…" if len(purchases) > 4 else "")
            lines.append(f"🛒 В задачах есть покупки ({len(purchases)}): {names} — перенести в список покупок?")
            button = InlineButton(f"🛒 Перенести в покупки ({len(purchases)})", f"ap:a:{move.nonce}")
            rows = inline_keyboard([[button]])["inline_keyboard"]
    return lines, rows


async def _morning_body_lines(ctx: JobContext) -> list[str]:
    """Sunday — the week in numbers; Monday and Thursday — weigh yourself (if the scale was quiet for 3 days);
    the 1st — measure the waist."""
    c = ctx.container
    local = ctx.now.astimezone(c.settings.tz).date()
    out: list[str] = []
    if c.health.configured():
        async with c.db.session() as session:
            if local.weekday() == 6 and (week := await c.health.week_summary(session, local)):
                out.append(week)
            if local.weekday() in (0, 3) and not await c.health.weighed_since(session, ctx.now - timedelta(days=3)):
                out.append("⚖️ Сегодня взвешивание: openScale → значок Bluetooth → встать на весы.")
    if local.day == 1 and c.settings.lmf_enabled:
        out.append("📏 Начало месяца — замерьте талию и скажите: «талия 94».")
    return out


async def h_briefing_evening(ctx: JobContext) -> dict[str, Any]:
    """21:00: what is not done (moving is said in words) + a button to mark calendar-duplicates as done."""
    c = ctx.container
    _require_fresh_slot(ctx, c.settings.briefing_evening_at)
    await _refresh_caches(ctx)
    rows: list[list[InlineButton]] = []
    async with c.db.session() as session:
        review = await c.agenda.evening_review(session, ctx.now)
        if c.todoist is not None and review.likely_done:
            done = await c.task_ops.request_op(
                session, op="complete", tasks=review.likely_done, now=ctx.now, chat_id=None
            )
            rows.append([InlineButton(f"✅ Отметить сделанными ({len(review.likely_done)})", f"ap:a:{done.nonce}")])
    async with c.db.session() as session:
        med_plan = await c.meds.today(session, ctx.now)
    unmarked = [
        f"{med.name} ({at.astimezone(c.settings.tz):%H:%M})" for med, at, st in med_plan if st in ("pending", "missed")
    ]
    pills = None
    if unmarked:
        pills = fmt.lines(
            fmt.line("💊", fmt.b("Не отмечены:"), ", ".join(unmarked)),
            fmt.hint("Скажите «выпил …» — отмечу и запишу в TLT."),
        )
    tracker_lines: list[str] = []
    if c.settings.tracker_reminders_enabled:
        tracker_lines = await c.tracker.evening_lines(ctx.now)
        if any(line.startswith("📓") for line in tracker_lines):  # today's TLT has gaps: one tap to fill them
            rows.append([InlineButton("✍️ Заполнить день", "tl:f")])
    async with c.db.session() as session:
        spent = await c.finance.today_line(session, ctx.now)
        watch = await c.health.evening_line(session, ctx.now)
    text_msg = fmt.blocks(review.text, pills, tracker_lines or None, watch, spent)
    key = f"briefing:evening:{ctx.payload.get('slot') or ctx.job.id}"
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(text_msg, dedupe_key=key, reply_markup=inline_keyboard(rows) if rows else None)
    checkin = False
    if c.settings.tracker_reminders_enabled and c.settings.lmf_enabled and c.notifier.default_chat_id is not None:
        from jarvis.bot.service import BotService  # the check-in is a bot flow (buttons answered by the bot)

        try:
            checkin = await BotService(c).cmd_checkin(c.notifier.default_chat_id)
        except Exception as exc:  # the review is out; a failed check-in must not retry the whole job
            log.warning("evening check-in skipped: %s", exc)
    return {"to_move": len(review.to_move), "likely_done": len(review.likely_done), "checkin": checkin}


async def h_usage_snapshot(ctx: JobContext) -> dict[str, Any]:
    """Hourly sample of the OpenRouter key's cumulative spend (free call) — the history for 📊 periods."""
    from jarvis.usage.service import UsageService

    usage_api = getattr(ctx.container.llm, "usage", None)
    if usage_api is None:
        raise SkipJob("no OpenRouter key")
    key = await usage_api()
    async with ctx.container.db.session() as session:
        total = await UsageService().record(session, key, ctx.now)
    return {"usage_micro": total}


async def h_weather_watch(ctx: JobContext) -> dict[str, Any]:
    """Tell the user when the weather type is about to change (rain starts/stops, clears up…) — awake hours only."""
    from jarvis.core.budget import consume
    from jarvis.core.errors import BudgetExceededError

    c = ctx.container
    s = c.settings
    if not (s.weather_enabled and s.weather_alerts_enabled):
        raise SkipJob("weather alerts disabled")
    local = ctx.now.astimezone(s.tz).time()
    sleep_start, sleep_end = parse_hhmm(s.sleep_start), parse_hhmm(s.sleep_end)
    asleep = local >= sleep_start or local < sleep_end if sleep_start > sleep_end else sleep_start <= local < sleep_end
    if asleep:
        raise SkipJob("sleep hours")
    forecast = await c.weather.forecast()
    if forecast is None:
        raise SkipJob("forecast unavailable")
    address = s.persona_address if s.persona_enabled else None
    change = c.weather.upcoming_change(forecast, ctx.now, within_hours=2, address=address)
    if change is None:
        return {"change": None}
    key = f"weather:{change.at.at.strftime('%Y-%m-%dT%H')}:{change.after}"
    try:
        async with c.db.session() as session:
            await consume(session, "weather_alert", ctx.now, s.weather_alerts_per_day)
    except BudgetExceededError as exc:
        raise SkipJob("daily weather alert limit reached") from exc
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(change.text, dedupe_key=key)
    return {"change": f"{change.before}->{change.after}", "at": change.at.at.isoformat()}


def med_message(kind: str, med: Any, intake: Any, tz: Any, address: str | None) -> tuple[fmt.Html, dict[str, Any]]:
    """Text + buttons of a medication reminder. Jarvis only reminds what the user set — no advice."""
    from jarvis.meds.service import is_pack, items_text, pack_emoji

    at = intake.due_at.astimezone(tz).strftime("%H:%M")
    dose = f" — {med.dose}" if med.dose else ""
    lead = f"{address.capitalize()}, в" if address else "В"
    if is_pack(med):
        hexid = intake.id.hex
        what = items_text(med)
        partial = InlineButton("☑ Не всё", f"mp:{hexid}")
        if kind == "late":
            text = fmt.lines(fmt.line("⚠️", fmt.b(med.name), f"в {at} не отмечены. Уже приняли?"), fmt.i(what))
            rows = [
                [InlineButton("✅ Уже принял", f"md:t:{hexid}"), partial],
                [InlineButton("✖️ Пропустил", f"md:s:{hexid}")],
            ]
        else:
            head = (
                fmt.line(pack_emoji(med.name), f"{lead}ремя:", fmt.b(med.name), f"({at})")
                if kind == "remind"
                else fmt.line("💊", "Напоминаю:", fmt.b(med.name), f"({at}) ещё не отмечены")
            )
            text = fmt.lines(head, fmt.i(what))
            rows = [
                [InlineButton("✅ Выпил всё", f"md:t:{hexid}"), partial],
                [InlineButton("⏰ 30 мин", f"md:z:{hexid}"), InlineButton("✖️ Пропустить", f"md:s:{hexid}")],
            ]
        return text, inline_keyboard(rows)
    if kind == "late":
        text = fmt.line("⚠️", "Приём", fmt.b(f"«{med.name}»"), f"в {at} не отмечен. Уже приняли?")
        rows = [
            [
                InlineButton("✅ Уже принял", f"md:t:{intake.id.hex}"),
                InlineButton("✖️ Пропустил", f"md:s:{intake.id.hex}"),
            ]
        ]
    else:
        text = (
            fmt.line("💊", f"{lead}ремя принять:", fmt.b(f"{med.name}{dose}"), f"({at}).")
            if kind == "remind"
            else fmt.line("💊", "Напоминаю:", fmt.b(f"{med.name}{dose}"), f"({at}) ещё не отмечен.")
        )
        rows = [
            [
                InlineButton("✅ Принял", f"md:t:{intake.id.hex}"),
                InlineButton("⏰ 30 мин", f"md:z:{intake.id.hex}"),
                InlineButton("✖️ Пропустить", f"md:s:{intake.id.hex}"),
            ]
        ]
    return text, inline_keyboard(rows)


COURSE_END_HOUR = 9  # a course that ended is announced the next morning


async def h_meds_tick(ctx: JobContext) -> dict[str, Any]:
    """Medication/supplement reminders: due now → remind; no answer in 45 min → one nudge; late → ask."""
    from jarvis.db.models import MedicationIntake

    c = ctx.container
    async with c.db.session() as session:
        actions = await c.meds.tick(session, ctx.now)  # committed: a crash cannot re-send the same step
    address = c.settings.persona_address if c.settings.persona_enabled else None

    async def undo(unsent: list[Any]) -> None:
        """Not delivered: undo these steps so the next tick sends them again (M8, review 17.10: a missing chat
        used to leave every step of the tick marked as sent)."""
        async with c.db.session() as session:
            for action in unsent:
                intake = await session.get(MedicationIntake, action.intake.id)
                if intake is not None and intake.status == "pending":
                    if action.kind == "nudge":
                        intake.nudged_at = None
                    else:
                        intake.reminded_at = None

    sent = 0
    for i, action in enumerate(actions):
        text, markup = med_message(action.kind, action.medication, action.intake, c.settings.tz, address)
        step = (action.intake.nudged_at or action.intake.reminded_at or ctx.now).isoformat()
        try:
            await c.notifier.send(text, dedupe_key=f"med:{action.intake.id}:{action.kind}:{step}", reply_markup=markup)
            sent += 1
        except NotConfiguredError as exc:
            await undo(actions[i:])
            raise SkipJob(f"telegram not configured: {exc}", {"sent": sent, "undelivered": len(actions) - i}) from exc
        except Exception:
            await undo(actions[i:])  # then the job retries
            raise
    if to_local(ctx.now, c.settings.tz).hour >= COURSE_END_HOUR:  # not at midnight: the morning after the last day
        async with c.db.session() as session:  # sent before the commit: a failed send keeps the course open
            for med in await c.meds.close_ended(session, ctx.now):
                with contextlib.suppress(NotConfiguredError):
                    await c.notifier.send(
                        fmt.lines(
                            fmt.line(
                                "🗓 Курс", fmt.b(med.name), f"закончился {med.ends_on:%d.%m.%Y} — напоминания остановил."
                            ),
                            fmt.hint(f"Продлить: «продли {med.name.lower()} на 2 недели» (в течение 30 дней)."),
                        ),
                        dedupe_key=f"med-course-end:{med.id}:{med.ends_on}",
                    )
    return {"sent": sent}


async def h_tasks_apply_op(ctx: JobContext) -> dict[str, Any]:
    """Execute an approved task operation (delete / bulk change). Idempotent per task."""
    from jarvis.scheduler.queue import enqueue_coalesced
    from jarvis.tasks.service import TaskOpsService

    c = ctx.container
    if c.todoist is None:
        raise SkipJob("todoist not configured")
    approval_id = uuid.UUID(ctx.payload["approval_id"])

    async def to_shopping(session: Any, task: Any) -> None:
        parent = await session.get(type(task), task.parent_id) if task.parent_id else None
        note = parent.content if parent is not None else None  # «кухонный нож (Обустройство квартиры)»
        await c.shopping.add_from_task(session, task.id, task.content, ctx.now, note=note)

    try:
        async with c.db.session() as session:
            result = await c.task_ops.apply_approved(
                session, c.todoist, c.todoist_sync, approval_id, ctx.now, to_shopping=to_shopping
            )
            approval = await session.get(Approval, approval_id)
            chat_id = approval.payload.get("chat_id") if approval is not None else None
            await enqueue_coalesced(
                session, job_type="reminders.plan", run_at=ctx.now, coalesce_key="task-op", priority=40
            )
    except AuthError as exc:
        await _auth_alert(ctx, "todoist", exc)
        raise
    if "skipped" in result:
        raise SkipJob(str(result["skipped"]))
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(
            fmt.headed(TaskOpsService.result_text(result)), dedupe_key=f"task-op-done:{approval_id}", chat_id=chat_id
        )
    return result


async def h_planner_apply(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    if c.todoist is None:
        raise SkipJob("todoist not configured")
    proposal_id = uuid.UUID(ctx.payload["proposal_id"])
    async with c.db.session() as session:
        result = await c.planner.apply(session, c.todoist, proposal_id, ctx.now)
    if result.get("applied") or result.get("failed"):
        text_msg = f"🗓 План применён: {result.get('applied', 0)} задач(и)" + (
            f", ошибок: {result['failed']}" if result.get("failed") else ""
        )
        with contextlib.suppress(NotConfiguredError):
            await c.notifier.send(text_msg, dedupe_key=f"plan-applied:{proposal_id}")
    return dict(result)


async def h_idea_analyze(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    idea_id = uuid.UUID(ctx.payload["idea_id"])
    async with c.db.session() as session:
        idea = await session.get(Idea, idea_id)
        if idea is None:
            raise SkipJob("idea not found")
        if idea.current_version > 0:
            raise SkipJob("already analyzed")
        version = await c.ideas.analyze(session, idea_id, ctx.now)
        chat_id = idea.telegram_chat_id
    text_msg = fmt.blocks(
        fmt.title("💡", version.title),
        version.summary,
        fmt.section("Открытые вопросы", [fmt.bullet(q) for q in version.open_questions[:5]], emoji="❓"),
    )
    markup = inline_keyboard([[InlineButton("📝 Сделать ТЗ", f"sp:{idea_id.hex}")]])
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(
            text_msg,
            dedupe_key=f"idea-analyzed:{idea_id}:{version.version}",
            chat_id=chat_id,
            reply_markup=markup,
        )
    return {"version": version.version}


async def h_embeddings_backfill(ctx: JobContext) -> dict[str, Any]:
    """Vectors for search by meaning: embed what is new or changed (batched, budgeted); idempotent."""
    c = ctx.container
    if not c.search.semantic:
        raise SkipJob("embeddings are lexical (hashing): search works by words")
    stats = await c.search.backfill(c.db, ctx.now)
    return stats.as_dict()


async def h_dev_reap(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    async with c.db.session() as session:
        return {"failed": await c.development.reap_lost(session, ctx.now)}


async def h_maintenance(ctx: JobContext) -> dict[str, Any]:
    """Retention for high-volume idempotency ledgers. Business records (ideas, audit, jobs) are kept."""
    c = ctx.container
    cutoff = ctx.now - timedelta(days=30)
    async with c.db.session() as session:
        a = await session.execute(delete(TelegramUpdate).where(TelegramUpdate.received_at < cutoff))
        b = await session.execute(delete(WebhookDelivery).where(WebhookDelivery.received_at < cutoff))
        e = await session.execute(delete(PendingChoice).where(PendingChoice.created_at < cutoff))
        d = await session.execute(
            text(
                "DELETE FROM scheduled_jobs WHERE status IN ('succeeded', 'skipped') "
                "AND finished_at < :cutoff AND dedupe_key LIKE 'periodic:%'"
            ),
            {"cutoff": cutoff},
        )
    counts: dict[str, Any] = {
        "telegram_updates": getattr(a, "rowcount", None),
        "webhook_deliveries": getattr(b, "rowcount", None),
        "pending_choices": getattr(e, "rowcount", None),
        "periodic_jobs": getattr(d, "rowcount", None),
    }
    return counts


async def _mirror_to_mac(c: Any, text: str, now: datetime, key: str) -> bool:
    """A reminder also pops up on the Mac while the app is online (never instead of Telegram, never failing it).
    `key` names the notice: a retried job finds it already queued and does not show it twice (L3, review 17.10)."""
    from jarvis.db.models import MacAction
    from jarvis.desktop.service import NOTIFY_MAX, Target

    if not c.settings.mac_notify_reminders:
        return False
    try:
        async with c.db.session() as session:
            env = await c.desktop.env(session)
            if env is None or not env.online(now):
                return False
            queued = await session.scalar(
                select(MacAction.id).where(MacAction.kind == "notify", MacAction.payload["key"].astext == key).limit(1)
            )
            if queued is not None:
                return True
            body = fmt.plain(text).strip()[:NOTIFY_MAX]
            notice = Target("notify", {"title": "Jarvis", "body": body, "key": key}, "напоминание")
            await c.desktop.enqueue(session, notice, now, chat_id=None)
        return True
    except Exception as exc:  # pragma: no cover - the Telegram reminder already went out
        log.warning("mac notify failed: %s", exc)
        return False


async def h_desktop_notify(ctx: JobContext) -> dict[str, Any]:
    """«напомни на маке …» at its time: on the Mac when it is online, otherwise in Telegram."""
    c = ctx.container
    text = str(ctx.payload.get("text") or "").strip()
    if not text:
        raise SkipJob("empty notice")
    if await _mirror_to_mac(c, f"🔔 {text}", ctx.now, f"desktop-notify:{ctx.job.id}"):
        return {"to": "mac"}
    try:
        await c.notifier.send(
            f"🔔 {text} (Mac не на связи — напоминаю здесь)",
            dedupe_key=f"desktop-notify:{ctx.job.id}",
            chat_id=ctx.payload.get("chat_id"),
        )
    except NotConfiguredError as exc:  # L4 (review 17.10): not «sent to Telegram» when it went nowhere
        raise SkipJob(f"undelivered: the Mac is offline and Telegram is not configured ({exc})") from exc
    return {"to": "telegram"}


async def h_research_run(ctx: JobContext) -> dict[str, Any]:
    """An approved web search: run once (paid — never retried automatically), keep the answer, send it."""
    from jarvis.core.budget import consume
    from jarvis.core.errors import BudgetExceededError

    c = ctx.container
    approval_id = uuid.UUID(ctx.payload["approval_id"])
    async with c.db.session() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None or approval.status != "approved":
            raise SkipJob("approval is not approved")
        payload = dict(approval.payload or {})
    chat_id = payload.get("chat_id")
    text_msg = payload.get("result")
    if text_msg is not None and payload.get("result_html"):
        text_msg = fmt.Html(text_msg)  # Jarvis composed and escaped it before keeping it
    if text_msg is None:
        try:
            async with c.db.session() as session:
                await consume(session, "research", ctx.now, c.settings.research_max_per_day)
        except BudgetExceededError:
            text_msg = (
                f"🔎 Лимит исследований на сегодня ({c.settings.research_max_per_day}) исчерпан — "
                "повторите завтра или поднимите RESEARCH_MAX_PER_DAY."
            )
        else:
            try:
                text_msg, _cost = await c.research.run(str(payload["question"]), ctx.now)
            except Exception as exc:  # a paid call: report instead of retrying (the provider may have billed it)
                log.warning("research failed: %s", exc)
                text_msg = f"🔎 Не получилось найти: {str(exc)[:200]}. Повторите запрос чуть позже."
        async with c.db.session() as session:
            row = await session.get(Approval, approval_id)
            if row is not None:
                row.payload = {**(row.payload or {}), "result": text_msg, "result_html": isinstance(text_msg, fmt.Html)}
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(text_msg, dedupe_key=f"research:{approval_id}", chat_id=chat_id)
    return {"chars": len(text_msg)}


async def _remember(ctx: JobContext, key: str, value: Any) -> None:
    """Keep a value in this job's payload: a retry of the job gets it back (`ctx.payload`), e.g. what a TLT day had
    before the first write — so a write whose reply was lost is not added twice."""
    ctx.payload[key] = value
    async with ctx.container.db.session() as session:
        await session.execute(
            update(ScheduledJob).where(ScheduledJob.id == ctx.job.id).values(payload=dict(ctx.payload))
        )


async def h_tracker_apply(ctx: JobContext) -> dict[str, Any]:
    """Write an approved TLT / TPL draft to LMF. LMF down → the queue retries (the user is told once)."""
    from jarvis.core.errors import TransientError
    from jarvis.tracker.service import TrackerDraft

    c = ctx.container
    approval_id = uuid.UUID(ctx.payload["approval_id"])
    async with c.db.session() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None or approval.status != "approved":
            raise SkipJob("approval is not approved")
        draft = TrackerDraft.model_validate(approval.payload.get("draft") or {})
        chat_id = approval.payload.get("chat_id")
    try:
        text_msg = await c.tracker.apply(draft, ctx.now, ctx.payload, functools.partial(_remember, ctx))
    except TransientError:
        await c.sites.ensure_running(c.settings.lmf_site_name)  # LMF down → start TLT; the retry finds it up
        last = ctx.job.attempts >= ctx.job.max_attempts
        notice = (
            "⚠️ Не смог записать в TLT: LMF так и не ответил. Откройте Jarvis OS → «Сайты» → TLT и повторите запись."
            if last
            else "⏳ LMF не отвечает — запишу в TLT автоматически, как только он поднимется."
        )
        with contextlib.suppress(NotConfiguredError):
            await c.notifier.send(
                notice, dedupe_key=f"tracker-wait:{approval_id}:{'last' if last else 'first'}", chat_id=chat_id
            )
        raise
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(text_msg, dedupe_key=f"tracker-done:{approval_id}", chat_id=chat_id)
    return {"day": draft.day, "tpl": draft.tpl_period}


async def h_supplements_log(ctx: JobContext) -> dict[str, Any]:
    """A taken pack / supplement → TLT, when LMF was down at the moment of the tap (retried by the queue)."""
    from jarvis.core.errors import TransientError
    from jarvis.db.models import Medication, MedicationIntake

    c = ctx.container
    async with c.db.session() as session:
        intake = await session.get(MedicationIntake, uuid.UUID(ctx.payload["intake_id"]))
        med = await session.get(Medication, intake.medication_id) if intake is not None else None
    if intake is None or med is None or intake.status != "taken" or not med.items:
        raise SkipJob("nothing to log")
    items = (
        med.items if intake.items_taken is None else [med.items[i] for i in intake.items_taken if i < len(med.items)]
    )
    day = intake.due_at.astimezone(c.settings.tz).date()
    try:
        text_msg = await c.tracker.log_items(items, day, ctx.now, ctx.payload, functools.partial(_remember, ctx))
    except TransientError:
        await c.sites.ensure_running(c.settings.lmf_site_name)
        if ctx.job.attempts >= ctx.job.max_attempts:
            with contextlib.suppress(NotConfiguredError):
                await c.notifier.send(
                    f"⚠️ Не смог записать в TLT «{med.name}»: LMF так и не ответил. Добавьте вручную в карточке дня.",
                    dedupe_key=f"supplements-log-failed:{intake.id}",
                )
        raise
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(text_msg, dedupe_key=f"supplements-log:{intake.id}")
    return {"logged": len(items)}


async def h_sites_tunnel_close(ctx: JobContext) -> dict[str, Any]:
    """A tunnel opened from chat closes after SITES_TUNNEL_HOURS (the user may have closed it already)."""
    c = ctx.container
    state = await c.sites.state()
    site = state.site(str(ctx.payload["site_id"]))
    if site is None or site.tunnel_status == "off":
        raise SkipJob("tunnel already closed")
    await c.sites.close_tunnel(site)
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(
            f"🔒 Закрыл туннель {site.name} (прошло {c.settings.sites_tunnel_hours:g} ч). "
            f"Снова: «открой сайт {site.name}, я не дома».",
            dedupe_key=f"tunnel-closed:{ctx.job.id}",
        )
    return {"closed": site.name}


async def h_tracker_nudge(ctx: JobContext) -> dict[str, Any]:
    """23:00: nothing at all in today's TLT → one message (this should not happen, so it is not optional)."""
    c = ctx.container
    slot = ctx.payload.get("slot")
    if slot:
        scheduled = local_datetime(
            date.fromisoformat(str(slot)), parse_hhmm(c.settings.tracker_nudge_at), c.settings.tz
        )
        if ctx.now - scheduled > timedelta(minutes=60):
            raise SkipJob(f"tracker nudge for {slot} is stale")
    empty = await c.tracker.day_is_empty(ctx.now)
    if not empty:
        raise SkipJob("the day is filled" if empty is False else "lmf unavailable")
    address = f"{c.settings.persona_address.capitalize()}, в" if c.settings.persona_enabled else "В"
    text_msg = fmt.lines(
        fmt.line("📓", fmt.b(f"{address} TLT за сегодня пусто.")),
        "Пара слов — и я запишу: «спал 7:30, качество 85, оценка 7, настроение хорошее, CD LT».",
        fmt.hint("Или нажмите кнопку — спрошу по шагам."),
    )
    day = ctx.now.astimezone(c.settings.tz).date().isoformat()
    fill = inline_keyboard([[InlineButton("✍️ Заполнить день", "tl:f")]])
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(text_msg, dedupe_key=f"tracker-nudge:{day}", reply_markup=fill)
    return {"sent": True}


async def h_health_ingest(ctx: JobContext) -> dict[str, Any]:
    """Payloads from the phone → readings; then, once each: today's night and new weighings are offered to TLT
    («✅ Записать» — nothing is written without the button). LMF down → offered on a later run."""
    c = ctx.container
    async with c.db.session() as session:
        done = await c.health.ingest(session, ctx.now)
    offered = []
    if c.settings.lmf_enabled and c.notifier.default_chat_id is not None:
        if c.settings.health_notify_sleep:
            offered += await _offer_sleep(ctx)
        if c.settings.health_notify_weight:
            for weighing in done.weights:
                offered += await _offer_weight(ctx, weighing)
    return {"payloads": done.payloads, "readings": done.readings, "deleted": done.deleted, "offered": offered}


async def _sent(ctx: JobContext, key: str) -> bool:
    from jarvis.db.models import Notification

    async with ctx.container.db.session() as session:
        return await session.scalar(select(Notification.id).where(Notification.dedupe_key == key)) is not None


async def _offer_tracker_draft(ctx: JobContext, key: str, draft: Any) -> list[str]:
    from jarvis.approvals.service import request_approval

    c = ctx.container
    try:
        text_msg = await c.tracker.describe(draft, ctx.now)
    except (TransientError, NotConfiguredError) as exc:
        log.info("health offer %s postponed: %s", key, exc)
        return []
    async with c.db.session() as session:
        approval = await request_approval(
            session,
            kind="tracker_write",
            target_type="lmf_day",
            target_id=draft.day or "",
            now=ctx.now,
            ttl=timedelta(days=1),
            payload={"draft": draft.model_dump(), "chat_id": c.notifier.default_chat_id},
        )
    with contextlib.suppress(NotConfiguredError):
        await c.notifier.send(
            text_msg, dedupe_key=key, reply_markup=approval_keyboard(approval.nonce, "✅ Записать", "✖️ Не надо")
        )
    return [key]


async def _offer_sleep(ctx: JobContext) -> list[str]:
    from jarvis.tracker.service import TrackerDraft

    c = ctx.container
    day = c.tracker.today(ctx.now)
    key = f"health:sleep:{day.isoformat()}"
    if await _sent(ctx, key):
        return []
    async with c.db.session() as session:
        night, heart = await c.health.night(session, day)
        model = await c.health.quality_model(session)
    if night is None or night.asleep_min < c.settings.health_min_sleep_hours * 60:
        return []
    try:
        current = await c.tracker.day_values(day)
    except (TransientError, NotConfiguredError):
        return []
    if current.get("sleep_minutes") not in (None, ""):
        return []  # already in TLT (said in words): nothing to offer
    quality = c.health.night_quality(model, night)  # ≈ Samsung's score, learnt from the user's export
    fields: dict[str, Any] = {"sleep_minutes": night.asleep_min}
    if quality is not None and current.get("sleep_quality") in (None, ""):
        fields["sleep_quality"] = quality
    draft = TrackerDraft(
        day=day.isoformat(),
        fields=fields,
        header=[f"⌚ Сон с часов: {c.health.night_line(night, heart, quality)}"],
    )
    return await _offer_tracker_draft(ctx, key, draft)


async def _offer_weight(ctx: JobContext, weighing: Any) -> list[str]:
    from jarvis.tracker.service import TrackerDraft

    c = ctx.container
    if ctx.now - weighing.start_at > timedelta(hours=36) or not weighing.value:
        return []  # a backfill of old weighings is stored, not offered
    if not c.health.from_scale(weighing.source):
        return []  # Google Fit's profile weight and the like: stored, not offered
    key = f"health:weight:{weighing.record_id}"
    if await _sent(ctx, key):
        return []
    async with c.db.session() as session:
        body = await c.health.body_of(session, weighing)
        header = await c.health.weighing_line(session, weighing, body)
    day = weighing.start_at.astimezone(c.settings.tz).date()
    draft = TrackerDraft(day=day.isoformat(), body=body, header=[header])
    return await _offer_tracker_draft(ctx, key, draft)


async def h_notify(ctx: JobContext) -> dict[str, Any]:
    c = ctx.container
    await c.notifier.send(
        str(ctx.payload["text"]),
        dedupe_key=str(ctx.payload["dedupe_key"]),
        chat_id=ctx.payload.get("chat_id"),
        reply_markup=ctx.payload.get("reply_markup"),
    )
    return {"sent": True}


def build_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register("todoist.refresh_task", h_todoist_refresh)
    registry.register("health.ingest", h_health_ingest)
    registry.register("todoist.reconcile", h_todoist_reconcile)
    registry.register("calendar.reconcile", h_calendar_reconcile)
    registry.register("calendar.ensure_channels", h_calendar_channels)
    registry.register("calendar.mirror", h_calendar_mirror)
    registry.register("reminders.plan", h_reminders_plan)
    registry.register("reminders.refresh_travel", h_refresh_travel)
    registry.register("reminder.fire", h_reminder_fire)
    registry.register("reminder.repeat", h_reminder_repeat)
    registry.register("planner.daily", h_planner_daily)
    registry.register("planner.apply", h_planner_apply)
    registry.register("briefing.morning", h_briefing_morning)
    registry.register("briefing.evening", h_briefing_evening)
    registry.register("tasks.apply_op", h_tasks_apply_op)
    registry.register("weather.watch", h_weather_watch)
    registry.register("usage.snapshot", h_usage_snapshot)
    registry.register("meds.tick", h_meds_tick)
    registry.register("idea.analyze", h_idea_analyze)
    registry.register("embeddings.backfill", h_embeddings_backfill)
    registry.register("dev.reap_lost", h_dev_reap)
    registry.register("maintenance.cleanup", h_maintenance)
    registry.register("notify.telegram", h_notify)
    registry.register("research.run", h_research_run)
    registry.register("desktop.notify", h_desktop_notify)
    registry.register("tracker.apply", h_tracker_apply)
    registry.register("tracker.nudge", h_tracker_nudge)
    registry.register("supplements.log", h_supplements_log)
    registry.register("sites.tunnel_close", h_sites_tunnel_close)
    return registry


def periodic_tasks(settings: Any) -> list[Any]:
    from datetime import time

    from jarvis.core.config import CalendarStrategy
    from jarvis.integrations.embeddings.client import embeddings_provider_name
    from jarvis.scheduler.periodic import PeriodicTask

    tasks = [
        PeriodicTask(
            "todoist.reconcile", "todoist.reconcile", every=timedelta(minutes=settings.todoist_reconcile_minutes)
        ),
        PeriodicTask(
            "calendar.reconcile", "calendar.reconcile", every=timedelta(minutes=settings.calendar_reconcile_minutes)
        ),
        PeriodicTask("reminders.plan", "reminders.plan", every=timedelta(minutes=5)),
        PeriodicTask("calendar.ensure_channels", "calendar.ensure_channels", every=timedelta(hours=6)),
        PeriodicTask("dev.reap_lost", "dev.reap_lost", every=timedelta(minutes=10)),
        PeriodicTask("maintenance.cleanup", "maintenance.cleanup", daily_at=time(4, 0)),
    ]
    if settings.briefing_morning_enabled:
        # The morning briefing carries the daily plan proposal (one message instead of two).
        morning = parse_hhmm(settings.briefing_morning_at)
        tasks.append(PeriodicTask("briefing.morning", "briefing.morning", daily_at=morning))
    else:
        tasks.append(PeriodicTask("planner.daily", "planner.daily", daily_at=time(8, 0)))
    if settings.briefing_evening_enabled:
        evening = parse_hhmm(settings.briefing_evening_at)
        tasks.append(PeriodicTask("briefing.evening", "briefing.evening", daily_at=evening))
    tasks.append(PeriodicTask("meds.tick", "meds.tick", every=timedelta(minutes=5), priority=15))
    if settings.lmf_enabled and settings.tracker_reminders_enabled:
        tasks.append(PeriodicTask("tracker.nudge", "tracker.nudge", daily_at=parse_hhmm(settings.tracker_nudge_at)))
    tasks.append(PeriodicTask("usage.snapshot", "usage.snapshot", every=timedelta(hours=1), priority=200))
    if settings.weather_enabled and settings.weather_alerts_enabled:
        tasks.append(PeriodicTask("weather.watch", "weather.watch", every=timedelta(minutes=30)))
    if settings.calendar_strategy == CalendarStrategy.MIRROR:
        tasks.append(PeriodicTask("calendar.mirror", "calendar.mirror", every=timedelta(minutes=10)))
    if embeddings_provider_name(settings) != "hashing" and settings.embeddings_backfill_minutes > 0:
        every = timedelta(minutes=settings.embeddings_backfill_minutes)
        tasks.append(PeriodicTask("embeddings.backfill", "embeddings.backfill", every=every, priority=180))
    return tasks


__all__ = ["approval_keyboard", "build_registry", "periodic_tasks"]
