"""Weather for the user's city: a short summary for today and "the weather is about to change" alerts.

WMO codes are grouped into the categories the user cares about (rain / cloudy / clear …); an alert is due when
the category within the next hours differs from the current one. Deterministic, no LLM.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta

from jarvis.core.config import Settings
from jarvis.integrations.weather.client import Forecast, HourForecast, WeatherApi

log = logging.getLogger(__name__)

CATEGORY_RU = {
    "clear": ("☀️", "ясно"),
    "cloudy": ("☁️", "облачно"),
    "fog": ("🌫", "туман"),
    "rain": ("🌧", "дождь"),
    "snow": ("🌨", "снег"),
    "storm": ("⛈", "гроза"),
}


def category(code: int) -> str:
    """WMO weather code → category."""
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "cloudy"


def label(code: int) -> str:
    emoji, text = CATEGORY_RU[category(code)]
    return f"{emoji} {text}"


def temp(value: float) -> str:
    rounded = round(value)
    return f"{'+' if rounded > 0 else ''}{rounded}°"


@dataclass(frozen=True)
class WeatherChange:
    at: HourForecast
    before: str  # category now
    after: str  # category from `at`
    text: str


class WeatherService:
    def __init__(self, settings: Settings, api: WeatherApi | None) -> None:
        self.settings = settings
        self.api = api
        self._cache: tuple[float, Forecast] | None = None

    @property
    def place(self) -> str:
        return self.settings.weather_place

    async def forecast(self) -> Forecast | None:
        """Cached for 10 minutes; None when unavailable (weather is never worth failing a reply for)."""
        if self.api is None or not self.settings.weather_enabled:
            return None
        if self._cache and time.monotonic() - self._cache[0] < 600:
            return self._cache[1]
        try:
            value = await self.api.forecast(
                self.settings.weather_latitude, self.settings.weather_longitude, self.settings.tz
            )
        except Exception as exc:
            log.warning("weather unavailable: %s", exc)
            return None
        self._cache = (time.monotonic(), value)
        return value

    def today_text(self, forecast: Forecast, now: datetime) -> str:
        """«🌦 Тбилиси: сейчас +20°, 🌧 дождь; днём +19…+22°; ☀️ ясно с 15:00»"""
        local_now = now.astimezone(self.settings.tz)
        rest = [h for h in forecast.hours if h.at.date() == local_now.date() and h.at >= local_now - timedelta(hours=1)]
        parts = [f"🌡 {self.place}: сейчас {temp(forecast.current_temperature)}, {label(forecast.current_code)}"]
        if rest:
            low, high = min(h.temperature for h in rest), max(h.temperature for h in rest)
            if round(low) != round(high):
                parts.append(f"до конца дня {temp(low)}…{temp(high)}")
            changes = self._changes(forecast.current_code, rest)
            if changes:
                parts.append("; ".join(f"{label(h.code)} с {h.at.strftime('%H:%M')}" for h in changes[:2]))
            rain = [h for h in rest if (h.precipitation_probability or 0) >= 60 and category(h.code) != "rain"]
            if category(forecast.current_code) != "rain" and not changes and rain:
                parts.append(f"возможен дождь около {rain[0].at.strftime('%H:%M')}")
        return "; ".join(parts)

    def day_text(self, forecast: Forecast, day: date_type) -> str | None:
        """«Завтра в Тбилиси +18…+22°, 🌧 дождь (до 90%)» for another day."""
        hours = [h for h in forecast.hours if h.at.date() == day and 8 <= h.at.hour <= 21]
        if not hours:
            return None
        low, high = min(h.temperature for h in hours), max(h.temperature for h in hours)
        counts: dict[str, int] = {}
        for h in hours:
            counts[category(h.code)] = counts.get(category(h.code), 0) + 1
        main = max(counts, key=lambda c: counts[c])
        emoji, text = CATEGORY_RU[main]
        rain = max((h.precipitation_probability or 0) for h in hours)
        chance = (
            f" (дождь до {rain}%)" if rain >= 40 and main != "rain" else (f" (до {rain}%)" if main == "rain" else "")
        )
        return f"{self.place}: {temp(low)}…{temp(high)}, {emoji} {text}{chance}"

    @staticmethod
    def _changes(current_code: int, hours: list[HourForecast]) -> list[HourForecast]:
        """Hours where the category changes and stays for at least 2 hours (no flapping on one odd hour)."""
        found: list[HourForecast] = []
        state = category(current_code)
        for i, hour in enumerate(hours):
            new = category(hour.code)
            if new != state and all(category(h.code) == new for h in hours[i : i + 2]):
                found.append(hour)
                state = new
        return found

    def upcoming_change(
        self, forecast: Forecast, now: datetime, within_hours: int = 2, address: str | None = None
    ) -> WeatherChange | None:
        """The weather type changes within the next hours (rain starts/stops, clears up, clouds in …)."""
        local_now = now.astimezone(self.settings.tz)
        window = [h for h in forecast.hours if local_now < h.at <= local_now + timedelta(hours=within_hours + 1)]
        changes = self._changes(forecast.current_code, window)
        if not changes or changes[0].at > local_now + timedelta(hours=within_hours):
            return None
        hour = changes[0]
        before, after = category(forecast.current_code), category(hour.code)
        minutes = max(0, int((hour.at - local_now).total_seconds() // 60))
        when = "вот-вот" if minutes < 20 else f"около {hour.at.strftime('%H:%M')}"
        emoji, after_ru = CATEGORY_RU[after]
        verb = {"rain": "начнётся дождь", "storm": "начнётся гроза", "snow": "пойдёт снег"}.get(after)
        if before in ("rain", "storm", "snow") and after in ("clear", "cloudy"):
            verb = f"{CATEGORY_RU[before][1]} закончится, будет {after_ru}"
        headline = verb or f"станет {after_ru}"
        change = hour.temperature - forecast.current_temperature
        trend = f", потеплеет до {temp(hour.temperature)}" if change >= 3 else ""
        trend = f", похолодает до {temp(hour.temperature)}" if change <= -3 else trend
        chance = (
            f" (вероятность {hour.precipitation_probability}%)"
            if after in ("rain", "storm") and hour.precipitation_probability
            else ""
        )
        lead = f"{address.capitalize()}, в {self.place}" if address else f"В {self.place}"
        text = (
            f"{emoji} {lead} {when} {headline}{chance}{trend}. "
            f"Сейчас {temp(forecast.current_temperature)}, {CATEGORY_RU[before][1]}."
        )
        return WeatherChange(hour, before, after, text)
