"""Weather forecast adapter: Open-Meteo (free, no API key; data CC BY 4.0, open-meteo.com)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from jarvis.core.errors import PermanentError, TransientError
from jarvis.core.http import ResilientHttp


@dataclass(frozen=True)
class HourForecast:
    at: datetime  # aware, local zone of the place
    temperature: float
    code: int  # WMO weather code
    precipitation_probability: int | None


@dataclass(frozen=True)
class Forecast:
    current_at: datetime
    current_temperature: float
    current_code: int
    hours: tuple[HourForecast, ...]


class WeatherApi(Protocol):
    async def forecast(self, latitude: float, longitude: float, tz: ZoneInfo) -> Forecast: ...


def parse_forecast(data: dict[str, Any], tz: ZoneInfo) -> Forecast:
    try:
        current = data["current"]
        hourly = data["hourly"]
        hours = tuple(
            HourForecast(
                datetime.fromisoformat(t).replace(tzinfo=tz),
                float(temp),
                int(code),
                int(prob) if prob is not None else None,
            )
            for t, temp, code, prob in zip(
                hourly["time"],
                hourly["temperature_2m"],
                hourly["weather_code"],
                hourly.get("precipitation_probability") or [None] * len(hourly["time"]),
                strict=False,
            )
            if temp is not None and code is not None
        )
        return Forecast(
            datetime.fromisoformat(current["time"]).replace(tzinfo=tz),
            float(current["temperature_2m"]),
            int(current["weather_code"]),
            hours,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"open-meteo: unexpected response ({type(exc).__name__})") from exc


class OpenMeteoWeather:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = ResilientHttp(
            "open-meteo", base_url="https://api.open-meteo.com/v1/", timeout=15.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def forecast(self, latitude: float, longitude: float, tz: ZoneInfo) -> Forecast:
        params = {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "current": "temperature_2m,weather_code",
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "timezone": tz.key,
            "forecast_days": 2,
        }
        return parse_forecast((await self._http.get("forecast", params=params)).json(), tz)


@dataclass
class FakeWeather:
    """Scripted forecast for tests: set `forecast_value` (or `unavailable`)."""

    forecast_value: Forecast | None = None
    unavailable: bool = False
    calls: list[tuple[float, float]] = field(default_factory=list)

    async def forecast(self, latitude: float, longitude: float, tz: ZoneInfo) -> Forecast:
        self.calls.append((latitude, longitude))
        if self.unavailable or self.forecast_value is None:
            raise TransientError("weather: fake outage")
        return self.forecast_value
