"""`.env.example` as shipped must load on the host too: python-dotenv reads `KEY=   # note` as the value «# note»,
so comments of empty keys sit on the line above; an empty `HEALTH_HEIGHT_CM=` once failed Settings."""

from __future__ import annotations

from pathlib import Path

from jarvis.core.config import Settings

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_env_example_loads() -> None:
    settings = Settings(_env_file=EXAMPLE)  # type: ignore[call-arg]
    assert settings.health_height_cm is None
    assert settings.crm_url.startswith("https://")
    assert settings.telegram_allowed_user_ids == []
    assert settings.jarvis_api_token is None or not settings.jarvis_api_token.get_secret_value()
    assert settings.maps_2gis_base_url == "https://2gis.ge/tbilisi"  # a value with a comment after it
