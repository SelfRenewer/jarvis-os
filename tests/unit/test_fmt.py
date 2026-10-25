"""Telegram HTML helpers (`jarvis.bot.fmt`) and splitting long HTML messages without breaking tags."""

from __future__ import annotations

import json
import re

import httpx
import respx

from jarvis.bot import fmt
from jarvis.integrations.telegram.client import MAX_TEXT, HttpTelegramApi, html_to_plain, split_html


def test_text_is_escaped_once() -> None:
    assert fmt.esc("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert fmt.esc(fmt.b("x")) == "<b>x</b>"  # Html is never escaped again
    assert fmt.b("<script>") == "<b>&lt;script&gt;</b>"
    assert fmt.b("") == ""
    assert fmt.code(fmt.b("x")) == "<code>x</code>"  # code cannot hold other formatting
    assert fmt.pre("a  1\nbb 2") == "<pre>a  1\nbb 2</pre>"


def test_links_only_http() -> None:
    assert fmt.link("Сайт", "https://x.ge/a?b=1&c=2") == '<a href="https://x.ge/a?b=1&amp;c=2">Сайт</a>'
    assert fmt.link("Сайт", "javascript:alert(1)") == "Сайт"
    assert fmt.link("Сайт", 'https://x.ge/"onmouseover=') == "Сайт"


def test_the_visual_language() -> None:
    assert fmt.title("📅", "Сегодня", "ср 23.09") == "📅 <b>Сегодня</b> · ср 23.09"
    assert fmt.head("Требует внимания", "💡") == "💡 <b>Требует внимания</b>"
    assert fmt.bullet("«Купить сигару»", "просрочена") == "• «Купить сигару» — <i>просрочена</i>"
    assert fmt.kv("Сон", "7:30", "качество 85") == "Сон: <b>7:30</b> · качество 85"
    assert fmt.hint("пример") == "<i>пример</i>"
    assert fmt.say("спал 7", "оценка 8") == "<i>«спал 7»</i>\n<i>«оценка 8»</i>"
    assert fmt.quote(fmt.lines("a", "b"), expandable=True) == "<blockquote expandable>a\nb</blockquote>"
    assert fmt.section("Пусто", []) is None
    assert fmt.blocks(fmt.title("📋", "Задачи"), None, [], ["• a", fmt.b("b")], "") == (
        "📋 <b>Задачи</b>\n\n• a\n<b>b</b>"
    )
    assert fmt.line("☐", None, fmt.b("10:00"), "Цветы & торт") == "☐ <b>10:00</b> Цветы &amp; торт"


def test_headed_bolds_the_first_line_of_a_service_text() -> None:
    assert fmt.headed("🗓 Предложение по расписанию:\n• 10:00 A & B") == (
        "🗓 <b>Предложение по расписанию</b>\n• 10:00 A &amp; B"
    )
    assert fmt.headed("✅ Отметить выполненными (2)?") == "✅ <b>Отметить выполненными (2)?</b>"
    assert fmt.headed("Просто текст") == "<b>Просто текст</b>"


def test_plain_text_for_speech_and_notifications() -> None:
    html = fmt.lines(fmt.title("🍽", "Еда"), fmt.bullet("Гречка < 200 г", "≈"))
    assert fmt.plain(html) == "🍽 Еда\n• Гречка < 200 г — ≈"
    assert fmt.plain("<b>not html</b>") == "<b>not html</b>"  # a plain str stays as it is
    assert html_to_plain("a &amp; <i>b</i> &lt;c&gt; &quot;d&quot;") == 'a & b <c> "d"'


def _balanced(chunk: str) -> bool:
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-z-]+)[^>]*>", chunk):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def test_short_html_is_one_message() -> None:
    assert split_html("<b>a</b>") == ["<b>a</b>"]


def test_long_html_is_split_at_sections_never_inside_a_tag() -> None:
    sections = [fmt.lines(fmt.head(f"Раздел {n}", "📋"), [fmt.bullet(f"пункт {n}.{k} & <x>") for k in range(40)])
                for n in range(8)]  # fmt: skip
    text = fmt.blocks(*sections)
    assert len(text) > MAX_TEXT
    chunks = split_html(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_TEXT for c in chunks)
    assert all(_balanced(c) for c in chunks)
    assert all(c.startswith("📋 <b>Раздел") for c in chunks)  # cut between sections
    assert html_to_plain("\n\n".join(chunks)) == html_to_plain(text)


def test_a_block_longer_than_a_message_is_closed_and_reopened() -> None:
    body = "\n".join(f"строка {n} — {'x' * 60}" for n in range(120))
    text = fmt.lines(fmt.title("📚", "Длинное"), fmt.quote(body, expandable=True))
    chunks = split_html(text)
    assert len(chunks) > 1 and all(len(c) <= MAX_TEXT for c in chunks)
    assert all(_balanced(c) for c in chunks)
    assert chunks[1].startswith("<blockquote expandable>")
    one_line = "&amp;" * 2000  # no newline at all: never cut inside an entity
    parts = split_html(one_line)
    assert all(re.fullmatch(r"(?:&amp;)+", p) for p in parts)


@respx.mock
async def test_http_adapter_sends_a_long_html_reply_in_parts_buttons_on_the_last() -> None:
    route = respx.post("https://api.telegram.org/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    )
    api = HttpTelegramApi("tok")
    text = fmt.blocks(*(fmt.lines(fmt.head(f"Раздел {n}"), ["• " + "x" * 80] * 30) for n in range(4)))
    markup = {"inline_keyboard": [[{"text": "ОК", "callback_data": "ap:a:x"}]]}
    await api.send_message(1001, text, reply_markup=markup, parse_mode="HTML")
    payloads = [json.loads(call.request.content) for call in route.calls]
    assert len(payloads) > 1 and all(p["parse_mode"] == "HTML" for p in payloads)
    assert all(_balanced(p["text"]) and len(p["text"]) <= MAX_TEXT for p in payloads)
    assert [("reply_markup" in p) for p in payloads] == [False] * (len(payloads) - 1) + [True]
    voice = respx.post("https://api.telegram.org/bottok/sendVoice").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})
    )
    await api.send_voice(1001, b"ID3", caption="<b>Готово</b>", parse_mode="HTML")
    assert b'name="parse_mode"' in voice.calls[0].request.content  # a formatted caption
    await api.aclose()
