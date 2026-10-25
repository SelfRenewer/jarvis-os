"""Weather categories, the day summary and "the weather is about to change" detection (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.integrations.weather.client import Forecast, HourForecast, parse_forecast
from jarvis.weather.service import WeatherService, category
from tests.conftest import make_settings

TZ = ZoneInfo("Asia/Tbilisi")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=TZ)


def forecast(codes: list[int], temps: list[float] | None = None, current: int | None = None) -> Forecast:
    """Hourly forecast from 09:00 local; `codes[i]` is the WMO code of hour 9+i."""
    temps = temps or [20.0] * len(codes)
    hours = tuple(
        HourForecast(NOW + timedelta(hours=i), temps[i], codes[i], 80 if codes[i] >= 51 else 5)
        for i in range(len(codes))
    )
    return Forecast(NOW, temps[0], codes[0] if current is None else current, hours)


def service() -> WeatherService:
    return WeatherService(make_settings(), None)


def test_categories() -> None:
    assert [category(c) for c in (0, 1, 2, 3, 45, 61, 80, 71, 95)] == [
        "clear",
        "clear",
        "cloudy",
        "cloudy",
        "fog",
        "rain",
        "rain",
        "snow",
        "storm",
    ]


def test_rain_is_announced_with_temperature_change() -> None:
    f = forecast([3, 3, 61, 61, 61], temps=[22, 21, 17, 17, 16])
    change = service().upcoming_change(f, NOW, within_hours=2, address="сэр")
    assert change is not None
    assert (change.before, change.after) == ("cloudy", "rain")
    assert change.text == (
        "🌧 Сэр, в Тбилиси около 11:00 начнётся дождь (вероятность 80%), похолодает до +17°. Сейчас +22°, облачно."
    )


def test_single_odd_hour_is_not_a_change_and_far_changes_wait() -> None:
    assert service().upcoming_change(forecast([3, 61, 3, 3, 3]), NOW) is None  # one rainy hour: flapping
    assert service().upcoming_change(forecast([3, 3, 3, 3, 61, 61]), NOW, within_hours=2) is None  # in 4 h
    stops = service().upcoming_change(forecast([61, 61, 0, 0, 0]), NOW)
    assert stops is not None
    assert "дождь закончится, будет ясно" in stops.text


def test_today_text() -> None:
    text = service().today_text(
        forecast([0, 0, 0, 0, 0, 0, 61, 61, 61], temps=[18, 19, 21, 22, 22, 21, 19, 18, 18]), NOW
    )
    assert text == "🌡 Тбилиси: сейчас +18°, ☀️ ясно; до конца дня +18°…+22°; 🌧 дождь с 15:00"


def test_parse_open_meteo_shape() -> None:
    data = {
        "current": {"time": "2026-09-24T17:00", "interval": 900, "temperature_2m": 20.1, "weather_code": 61},
        "hourly": {
            "time": ["2026-09-24T17:00", "2026-09-24T18:00"],
            "temperature_2m": [20.1, 19.4],
            "weather_code": [61, 3],
            "precipitation_probability": [70, 20],
        },
    }
    parsed = parse_forecast(data, TZ)
    assert parsed.current_at == datetime(2026, 9, 24, 17, 0, tzinfo=TZ)
    assert [h.code for h in parsed.hours] == [61, 3]
