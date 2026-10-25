"""Chat reply → text for speech synthesis (deterministic; no LLM, so dates and times cannot change).

Emoji and list markers are dropped, "10:00–11:00" becomes "с 10:00 до 11:00", "чт 24.09" becomes
"четверг, 24 сентября", every line ends with punctuation (a pause), and long replies are cut at a sentence
boundary with a pointer to the text message.

What only makes sense on screen is never read aloud (2026-10-14, the user's request): links and bare domains,
lists of sources, keys and tokens (long letter-digit strings, hex, UUIDs, `sk-…`, JWTs), ENV_NAMES, file paths,
e-mail addresses, /commands, `code` and footnote marks [1].
"""

from __future__ import annotations

import re

MONTHS_GENITIVE = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
WEEKDAYS = {
    "пн": "понедельник",
    "вт": "вторник",
    "ср": "среда",
    "чт": "четверг",
    "пт": "пятница",
    "сб": "суббота",
    "вс": "воскресенье",
}
SKIP_LINES = ("• — календарь", "Кнопки ниже", "/shop quote", "ℹ️ Прошедшие дни", "Источники", "Стоимость запроса")
TAIL = " Остальное — в сообщении."

_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿⌀-⏿️‍←-⇿•☐‼⁉]")
_RANGE = re.compile(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})")
_OPEN_START = re.compile(r"…\s*[–-]\s*(\d{1,2}:\d{2})")
_OPEN_END = re.compile(r"(\d{1,2}:\d{2})\s*[–-]\s*…")
_DATE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{2})(?:\.(\d{4}))?(?![\d.]|\s*(?:GEL|₾|\$|€|USD|лари))")
_TIME = re.compile(r"(?<![\w:])\d{1,2}:\d{2}(?![\d:])")
_LEADING_TIME = re.compile(r"^((?:с|до|в) \d{1,2}:\d{2}(?: до \d{1,2}:\d{2})?) (?=[^\s—–-])")
_WEEKDAY = re.compile(r"\b(пн|вт|ср|чт|пт|сб|вс)\b(?=\s+\d{1,2}\.\d{2})", re.IGNORECASE)


_URL = re.compile(r"(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
_DOMAIN = re.compile(
    r"(?<![\w@/.-])(?:[a-z0-9-]+\.)+(?:com|ru|ge|org|net|io|ai|app|dev|me|co|ly|gg|tv|info|biz|uk|de|ua|kz|by|am|"
    r"gov|edu|online|site|store|tech|cloud|so|sh|to|xyz|pro)(?:/\S*)?(?![\w-])",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_CODE = re.compile(r"`[^`\n]*`")
_PATH = re.compile(r"(?<!\w)(?:~|\.{1,2})?/(?:[\w.@%+-]+/)*[\w.@%+-]+/?|\b[A-Za-z]:\\\S+")
_COMMAND = re.compile(
    r"(?<![\w/.~])/[a-z][a-z_]{1,30}\b(?:\s+(?:on|off|add|quote|import|pref|clear|<[^>]+>|\[[^\]]+\]))*"
)
_ENV = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SECRET = re.compile(
    r"\b(?:sk|pk|rk|xox[abpr]|ghp|gho|github_pat|AIza|ya29)[-_][\w-]{8,}"  # well-known key prefixes
    r"|\beyJ[\w-]+\.[\w-]+\.[\w-]+"  # JWT
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUID
    r"|\b[0-9a-fA-F]{16,}\b"  # hex: hashes, ids
    r"|(?<![\w-])(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{20,}(?![\w-])",  # long letter+digit tokens
    re.IGNORECASE,
)
_FOOTNOTE = re.compile(r"\[\d{1,2}(?:\s*,\s*\d{1,2})*\]")
_FILE = re.compile(
    r"[^\s,;()«»]*/[^\s,;()«»]*/[^\s,;()«»]*"
    r"|[^\s,;()«»]+\.(?:json|py|md|txt|ya?ml|toml|sh|swift|js|mjs|ts|css|html|db|env|log|plist|ics)\b"
)
# «зайти на https://…» → «зайти»: the preposition goes with the link
_PREP = r"(?:\b(?:на|в|во|с|со|из|по|от|через|к|ко|at|on)\s+)?"
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)]|[•*-])\s")


def strip_unspeakable(text: str) -> str:
    """Remove what is meant to be read or clicked, not heard (links, keys, paths, commands…)."""
    out = _CODE.sub(" ", text)
    for pattern in (_URL, _EMAIL, _DOMAIN):
        out = re.sub(_PREP + pattern.pattern, " ", out, flags=re.IGNORECASE)
    out = _SECRET.sub(" ", out)
    out = re.sub(_PREP + "(?:" + _FILE.pattern + ")", " ", out)
    out = _COMMAND.sub(" ", out)  # «/voice on» before paths: a command looks like «/path»
    out = re.sub(_PREP + "(?:" + _PATH.pattern + ")", " ", out)
    out = _ENV.sub(" ", out)
    out = _FOOTNOTE.sub("", out)
    if out == text:
        return text.strip()
    out = re.sub(r"\(\s*[,;:—–-]*\s*\)|\[\s*\]|«\s*»", " ", out)  # brackets left empty
    out = re.sub(r"(?:\s*[—–-]\s*)+([.,;!?]|$)", r"\1", out)  # «Источник — » with the link gone
    out = re.sub(r"(?:\s*[·|]\s*)+(?=[·|]|$)", "", out)  # separators left between removed commands
    out = re.sub(r"^\s*[·|]\s*|(?<=:)\s*[·|]\s*", " ", out)
    out = re.sub(r"\s+([,.;:!?)])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _only_label(original: str, cleaned: str) -> bool:
    """A list item that was a link («1. Public Service Hall — https://…», a source): not worth saying at all."""
    return bool(_LIST_ITEM.match(original)) and bool(_URL.search(original) or _DOMAIN.search(original))


def _date(match: re.Match[str]) -> str:
    day, month = int(match.group(1)), int(match.group(2))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return match.group(0)
    year = f" {match.group(3)} года" if match.group(3) else ""
    return f"{day} {MONTHS_GENITIVE[month - 1]}{year}"


def _at_time(match: re.Match[str]) -> str:
    before = match.string[max(0, match.start() - 3) : match.start()]
    if before.endswith(("с ", "до ", "в ")):
        return match.group(0)
    return f"в {match.group(0)}"


def speakable_line(line: str) -> str:
    cleaned = strip_unspeakable(line)
    if _only_label(line, cleaned) or (cleaned != line.strip() and re.fullmatch(r"[\w\s]{1,30}:", cleaned)):
        return ""  # a source link, or a label whose content was all links/commands («Команды:»)
    text = cleaned.replace("📍", ", ").replace("→", " — ").replace("«", "").replace("»", "")
    text = _EMOJI.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -—–")
    text = _WEEKDAY.sub(lambda m: WEEKDAYS[m.group(1).lower()] + ",", text)
    text = _RANGE.sub(r"с \1 до \2", text)
    text = _OPEN_START.sub(r"до \1", text)
    text = _OPEN_END.sub(r"с \1", text)
    text = _DATE.sub(_date, text)
    text = _TIME.sub(_at_time, text)
    text = _LEADING_TIME.sub(r"\1 — ", text)
    text = re.sub(r"(\d \w+) (в \d{1,2}:\d{2})", r"\1, \2", text)  # "25 сентября, в 16:00"
    text = re.sub(r"\bGEL\b", "лари", text)
    text = re.sub(r"\s+([,.!?:;])", r"\1", text)
    text = re.sub(r",\s*,", ",", text).strip()
    if text and text[-1] not in ".!?:;…":
        text += "."
    return text[:1].upper() + text[1:]


def to_speech(text: str, max_chars: int = 700) -> str | None:
    """The spoken version of a reply, or None when there is nothing worth saying aloud."""
    sentences: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith(SKIP_LINES):
            continue
        line = speakable_line(raw_line)
        if line and re.search(r"\w", line):
            sentences.append(line)
    if not sentences:
        return None
    spoken = ""
    for sentence in sentences:
        candidate = f"{spoken} {sentence}".strip()
        if len(candidate) > max_chars:
            if not spoken:  # a single very long line: cut at a word boundary
                spoken = sentence[: max_chars - len(TAIL)].rsplit(" ", 1)[0].rstrip(",;:") + "…"
            return spoken + TAIL
        spoken = candidate
    return spoken
