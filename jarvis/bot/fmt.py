"""Telegram HTML for every message Jarvis writes: one visual language, safe by construction.

Telegram Bot API «HTML style» (checked 2026-10-14): supported tags are b/strong, i/em, u/ins, s/strike/del,
tg-spoiler, a href, code, pre, blockquote and blockquote expandable; only `&lt; &gt; &amp; &quot;` are named
entities; every `<`, `>` and `&` that is not markup must be escaped; bold/italic/underline/strike may nest in
anything except code/pre, blockquotes cannot nest. A text is limited to 4096 characters after parsing.

`Html` marks a string that is already safe markup. Plain `str` is always text: helpers escape it, `Html` is never
escaped twice. The language:

    📅 <b>Title</b> · secondary          ← `title()`: an emoji and a bold title line
                                         ← a blank line between sections (`blocks()`)
    <b>Section</b>                       ← `head()`
    • item — <i>detail</i>               ← `bullet()`: short bullets, details in italics
    Key: <b>value</b>                    ← `kv()`
    <i>hint or example</i>               ← `hint()`; long example lists go into `quote(…, expandable=True)`
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Iterable

__all__ = [
    "Html",
    "b",
    "blocks",
    "bullet",
    "code",
    "esc",
    "head",
    "hint",
    "i",
    "kv",
    "line",
    "lines",
    "link",
    "plain",
    "pre",
    "quote",
    "s",
    "say",
    "section",
    "strip_tags",
    "title",
    "to_html",
    "u",
]


class Html(str):
    """Safe Telegram HTML: escaped text plus supported tags. Concatenating with `+`/f-strings gives a plain `str`
    again, so build messages with the helpers below (they keep the mark)."""

    __slots__ = ()


_TAG = re.compile(r"<[^<>]+>")


def esc(text: object) -> Html:
    """Text → safe HTML (`Html` is returned as is)."""
    if isinstance(text, Html):
        return text
    return Html(_html.escape("" if text is None else str(text), quote=False))


def to_html(text: str) -> Html:
    return esc(text)


def strip_tags(text: str) -> str:
    """HTML → what the reader sees (for speech, notifications, logs)."""
    return _html.unescape(_TAG.sub("", text))


def plain(text: str) -> str:
    """`Html` → plain text; a plain `str` is returned unchanged."""
    return strip_tags(text) if isinstance(text, Html) else text


def _wrap(tag: str, text: object, attrs: str = "") -> Html:
    body = esc(text)
    return Html(f"<{tag}{attrs}>{body}</{tag}>") if body else Html("")


def b(text: object) -> Html:
    return _wrap("b", text)


def i(text: object) -> Html:
    return _wrap("i", text)


def u(text: object) -> Html:
    return _wrap("u", text)


def s(text: object) -> Html:
    return _wrap("s", text)


def code(text: object) -> Html:
    """Inline code (`/today`, an id). Code cannot contain other formatting: tags are dropped."""
    return _wrap("code", strip_tags(text) if isinstance(text, Html) else str(text))


def pre(text: object) -> Html:
    """A monospaced block (aligned tables)."""
    return _wrap("pre", strip_tags(text) if isinstance(text, Html) else str(text))


def link(text: object, url: str) -> Html:
    """A clickable title; anything but an http(s) URL stays plain text."""
    url = (url or "").strip()
    if not re.match(r"https?://[^\s<>\"]+$", url, re.IGNORECASE):
        return esc(text)
    return _wrap("a", text, f' href="{_html.escape(url, quote=True)}"')


def quote(text: object, *, expandable: bool = False) -> Html:
    """A block quotation; `expandable` shows the first lines and hides the rest behind a tap (long lists)."""
    return _wrap("blockquote", text, " expandable" if expandable else "")


def _flat(parts: Iterable[object]) -> list[Html]:
    out: list[Html] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            if part:
                out.append(esc(part))
            continue
        if isinstance(part, Iterable):
            out += _flat(part)
        else:
            out.append(esc(part))
    return out


def line(*parts: object, sep: str = " ") -> Html:
    """Pieces of one line, joined by `sep` (empty ones skipped)."""
    return Html(esc(sep).join(_flat(parts)))


def lines(*parts: object) -> Html:
    """Lines (strings, `Html` or lists of them) joined by newlines; empty ones skipped."""
    return Html("\n".join(_flat(parts)))


def blocks(*parts: object) -> Html:
    """Sections separated by a blank line; empty sections skipped."""
    return Html("\n\n".join(p for p in (lines(part) for part in parts) if p))


def title(emoji: str, text: object, note: object = None) -> Html:
    """The first line of a message: «📅 <b>Сегодня</b> · ср 23.09»."""
    tail = Html(f" · {esc(note)}") if note not in (None, "") else ""
    return Html(f"{esc(emoji)} {b(text)}{tail}".strip())


def head(text: object, emoji: str = "") -> Html:
    """A section heading: «💡 <b>Требует внимания</b>»."""
    return Html(f"{esc(emoji)} {b(text)}".strip())


def bullet(text: object, note: object = None, *, mark: str = "•") -> Html:
    """«• Купить сигару — <i>просрочена с 22.09</i>»."""
    tail = Html(f" — {i(note)}") if note not in (None, "") else ""
    return Html(f"{esc(mark)} {esc(text)}{tail}".strip())


def kv(label: object, value: object, note: object = None) -> Html:
    """«Сон: <b>7:30</b> · качество 85»."""
    tail = Html(f" · {esc(note)}") if note not in (None, "") else ""
    return Html(f"{esc(label)}: {b(value)}{tail}")


def hint(text: object) -> Html:
    """A secondary line (explanations, examples) in italics."""
    return i(text)


def say(*examples: str) -> Html:
    """Example phrases, one per line: «<i>«спал 7:30, оценка 7»</i>»."""
    return lines(*(i(f"«{e}»") for e in examples))


def headed(text: str) -> Html:
    """A plain text from a service («🗓 Предложение по расписанию:\\n• …») → HTML with its first line as the bold
    title; the rest stays as it was (escaped). `Html` is returned unchanged."""
    if isinstance(text, Html):
        return text
    first, sep, rest = text.partition("\n")
    top = first.rstrip()
    if not top:
        return esc(text)
    top = top[:-1] if top.endswith(":") else top
    emoji, _, words = top.partition(" ")
    heading = title(emoji, words) if words and not any(ch.isalnum() for ch in emoji) else b(top)
    return Html(f"{heading}{sep}{esc(rest)}") if sep else heading


def section(heading: object, items: Iterable[object], *, emoji: str = "") -> Html | None:
    """A heading with its lines; None when there is nothing to show (so `blocks()` skips it)."""
    body = _flat(items)
    if not body:
        return None
    return lines(head(heading, emoji), body)
