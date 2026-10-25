"""Which maps provider computes travel time (MAPS_PROVIDER) and which one geocodes addresses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.core.config import Settings
from jarvis.integrations.maps.openrouteservice import OpenRouteServiceRoutesApi
from jarvis.integrations.maps.routes import Geocoder, HttpRoutesApi, RoutesApi, UnavailableRoutesApi

GOOGLE = "google"
OPENROUTESERVICE = "openrouteservice"


def _has(secret: Any) -> bool:
    return bool(secret is not None and secret.get_secret_value().strip())


def maps_provider_name(settings: Settings) -> str | None:
    """auto = Google if its key (live traffic), else OpenRouteService if its key (free), else None. An explicit
    provider without its key → None: no travel time rather than a silent switch to another provider."""
    choice = settings.maps_provider
    if choice in ("auto", GOOGLE) and _has(settings.google_maps_api_key):
        return GOOGLE
    if choice in ("auto", OPENROUTESERVICE) and _has(settings.openrouteservice_api_key):
        return OPENROUTESERVICE
    return None


def missing_key_hint(settings: Settings) -> str:
    if settings.maps_provider == GOOGLE:
        return "MAPS_PROVIDER=google but GOOGLE_MAPS_API_KEY is empty"
    if settings.maps_provider == OPENROUTESERVICE:
        return "MAPS_PROVIDER=openrouteservice but OPENROUTESERVICE_API_KEY is empty"
    return "set OPENROUTESERVICE_API_KEY (free) or GOOGLE_MAPS_API_KEY"


@dataclass
class MapsSetup:
    routes: RoutesApi
    geocoder: Geocoder | None  # addresses → coordinates (OpenRouteService key present, whatever routes)
    status: str
    closables: list[Any]


def build_maps(settings: Settings) -> MapsSetup:
    ors: OpenRouteServiceRoutesApi | None = None
    if _has(settings.openrouteservice_api_key):
        assert settings.openrouteservice_api_key is not None
        ors = OpenRouteServiceRoutesApi(
            settings.openrouteservice_api_key.get_secret_value().strip(),
            focus=(settings.weather_latitude, settings.weather_longitude),
            country=settings.maps_geocode_country,
        )
    closables: list[Any] = [ors] if ors is not None else []
    name = maps_provider_name(settings)
    routes: RoutesApi
    if name == GOOGLE:
        assert settings.google_maps_api_key is not None
        google = HttpRoutesApi(
            settings.google_maps_api_key.get_secret_value(), traffic_aware=settings.maps_traffic_aware
        )
        closables.append(google)
        routes, status = google, "CONFIGURED (google)"
    elif name == OPENROUTESERVICE and ors is not None:
        routes, status = ors, "CONFIGURED (openrouteservice)"
    else:
        routes, status = UnavailableRoutesApi(), "IMPLEMENTED_NEEDS_CREDENTIALS"
    return MapsSetup(routes, ors, status, closables)
