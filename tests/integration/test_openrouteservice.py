"""OpenRouteService adapter (respx, no network) and MAPS_PROVIDER selection.

Response shapes follow the ORS directions docs (`routes[0].summary.duration/distance`) and Pelias (GeoJSON features,
coordinates [lon, lat]); 401/403 bodies are the ones the gateway answered live without / with a wrong key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from jarvis.integrations.maps.openrouteservice import OpenRouteServiceRoutesApi, ors_profile
from jarvis.integrations.maps.provider import build_maps, maps_provider_name, missing_key_hint
from jarvis.integrations.maps.routes import HttpRoutesApi, RouteStatus, UnavailableRoutesApi
from tests.conftest import make_settings

KEY = "ors-test-key-0123456789"  # gitleaks:allow (test value)
DIRECTIONS = "https://api.openrouteservice.org/v2/directions/{}/json"
GEOCODE = "https://api.openrouteservice.org/geocode/search"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
HOME = (41.7443, 44.8304)  # (lat, lng) somewhere in the city
CENTRE = (41.7151, 44.8271)
TBILISI = (41.7151, 44.8271)


def route_json(duration: float = 287.4, distance: float = 2803.1) -> dict[str, Any]:
    return {
        "bbox": [44.82, 41.71, 44.84, 41.75],
        "routes": [
            {
                "summary": {"distance": distance, "duration": duration},
                "bbox": [44.82, 41.71, 44.84, 41.75],
                "way_points": [0, 42],
            }
        ],
        "metadata": {"service": "routing", "query": {"profile": "driving-car"}},
    }


def feature(
    lat: float, lng: float, layer: str = "address", confidence: float = 0.9, label: str = "x"
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},  # GeoJSON: [lon, lat]
        "properties": {"layer": layer, "confidence": confidence, "label": label, "country_a": "GEO"},
    }


def collection(*features: dict[str, Any]) -> dict[str, Any]:
    return {"geocoding": {"version": "0.2"}, "type": "FeatureCollection", "features": list(features)}


def api() -> OpenRouteServiceRoutesApi:
    client = OpenRouteServiceRoutesApi(KEY, focus=TBILISI, country="ge")
    client._http._sleep = _no_sleep
    return client


async def _no_sleep(_: float) -> None:
    return None


def coords(point: tuple[float, float]) -> str:
    return f"{point[0]},{point[1]}"


# --- directions ------------------------------------------------------------------------------------------------


@respx.mock
async def test_directions_request_shape_and_parsing() -> None:
    route = respx.post(DIRECTIONS.format("driving-car")).mock(return_value=httpx.Response(200, json=route_json()))
    client = api()
    result = await client.compute(coords(HOME), coords(CENTRE), datetime.now(UTC), "DRIVE")
    assert result.status == RouteStatus.OK
    assert (result.duration_seconds, result.distance_meters) == (287, 2803)
    assert result.traffic_aware is False
    request = route.calls[0].request
    assert request.headers["authorization"] == KEY
    assert KEY not in str(request.url) and "api_key" not in str(request.url)
    body = json.loads(request.content)
    assert body["coordinates"] == [[HOME[1], HOME[0]], [CENTRE[1], CENTRE[0]]]  # [lon, lat]!
    assert body["instructions"] is False and body["geometry"] is False
    assert respx.calls.call_count == 1  # coordinates need no geocoding
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("mode", "profile"), [("DRIVE", "driving-car"), ("walk", "foot-walking"), ("BICYCLE", "cycling-regular")]
)
async def test_travel_modes_map_to_profiles(mode: str, profile: str) -> None:
    route = respx.post(DIRECTIONS.format(profile)).mock(return_value=httpx.Response(200, json=route_json()))
    client = api()
    assert (await client.compute(coords(HOME), coords(CENTRE), datetime.now(UTC), mode)).status == RouteStatus.OK
    assert route.called
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("mode", ["TRANSIT", "TWO_WHEELER"])
async def test_unsupported_mode_makes_no_request(mode: str) -> None:
    client = api()
    result = await client.compute(coords(HOME), coords(CENTRE), datetime.now(UTC), mode)
    assert result.status == RouteStatus.UNAVAILABLE
    assert "not supported" in (result.error or "")
    assert respx.calls.call_count == 0
    assert ors_profile(mode) is None
    await client.aclose()


@respx.mock
async def test_same_point_route_has_an_empty_summary() -> None:
    """ORS leaves zero values out: a route between the same points comes back with `summary: {}`."""
    body = {"routes": [{"summary": {}, "way_points": [0, 0]}]}
    respx.post(DIRECTIONS.format("driving-car")).mock(return_value=httpx.Response(200, json=body))
    client = api()
    result = await client.compute(coords(HOME), coords(HOME), datetime.now(UTC), "DRIVE")
    assert (result.status, result.duration_seconds) == (RouteStatus.OK, 0)
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("response", "status", "error"),
    [
        (httpx.Response(401, json={"error": "Authorization field missing"}), RouteStatus.UNAVAILABLE, "API key"),
        (
            httpx.Response(403, json={"error": "Access to this API has been disallowed"}),
            RouteStatus.UNAVAILABLE,
            "API key",
        ),
        (
            httpx.Response(
                404,
                json={"error": {"code": 2010, "message": "Could not find routable point within a radius of 350.0 m"}},
            ),
            RouteStatus.NOT_FOUND,
            "404",
        ),
        (httpx.Response(400, json={"error": {"code": 2004, "message": "too far"}}), RouteStatus.NOT_FOUND, "400"),
        (httpx.Response(429, json={"error": "Rate limit exceeded"}), RouteStatus.QUOTA, "429"),
        (httpx.Response(503), RouteStatus.UNAVAILABLE, "503"),
        (httpx.Response(200, json={"routes": []}), RouteStatus.NOT_FOUND, "no route"),
        (httpx.Response(200, json={"routes": [{"segments": []}]}), RouteStatus.NOT_FOUND, "summary"),
    ],
)
async def test_directions_failures_never_invent_duration(
    response: httpx.Response, status: RouteStatus, error: str
) -> None:
    respx.post(DIRECTIONS.format("driving-car")).mock(return_value=response)
    client = api()
    result = await client.compute(coords(HOME), coords(CENTRE), datetime.now(UTC), "DRIVE")
    assert result.status == status
    assert result.duration_seconds is None
    assert error in (result.error or "")
    assert KEY not in (result.error or "")
    await client.aclose()


# --- geocoding -------------------------------------------------------------------------------------------------


@respx.mock
async def test_geocode_request_shape_and_lon_lat_order() -> None:
    search = respx.get(GEOCODE).mock(
        return_value=httpx.Response(
            200, json=collection(feature(41.7438, 44.8306, layer="venue", label="Blue Door Cafe"))
        )
    )
    client = api()
    result = await client.geocode("  Blue Door Cafe,  12 Rustaveli Ave ")
    assert result.status == RouteStatus.OK and result.point is not None
    assert (result.point.latitude, result.point.longitude) == (41.7438, 44.8306)
    request = search.calls[0].request
    assert request.headers["authorization"] == KEY
    params = dict(request.url.params)
    assert params == {
        "text": "Blue Door Cafe, 12 Rustaveli Ave",
        "size": "5",
        "boundary.country": "GE",
        "focus.point.lat": "41.71510",
        "focus.point.lon": "44.82710",
    }
    assert KEY not in str(request.url)
    await client.aclose()


@respx.mock
async def test_geocode_skips_coarse_and_doubtful_matches() -> None:
    respx.get(GEOCODE).mock(
        return_value=httpx.Response(
            200,
            json=collection(
                feature(41.7383, 44.8204, layer="locality", label="Tbilisi"),  # a city centroid: not a destination
                feature(41.6983, 44.7904, layer="address", confidence=0.3),  # too doubtful
                feature(41.7484, 44.8206, layer="street", confidence=0.8, label="Rustaveli Ave"),
            ),
        )
    )
    client = api()
    result = await client.geocode("Rustaveli Ave")
    assert result.status == RouteStatus.OK and result.point is not None
    assert (result.point.latitude, result.point.longitude) == (41.7484, 44.8206)
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(200, json=collection()), RouteStatus.NOT_FOUND),
        (httpx.Response(200, json=collection(feature(41.6, 41.6, layer="region"))), RouteStatus.NOT_FOUND),
        (httpx.Response(403, json={"error": "Access to this API has been disallowed"}), RouteStatus.UNAVAILABLE),
        (httpx.Response(429), RouteStatus.QUOTA),
        (httpx.Response(502), RouteStatus.UNAVAILABLE),
    ],
)
async def test_geocode_failures(response: httpx.Response, status: RouteStatus) -> None:
    respx.get(GEOCODE).mock(return_value=response)
    fallback = respx.get(NOMINATIM).mock(return_value=httpx.Response(200, json=[]))  # it does not know it either
    client = api()
    result = await client.geocode("Nowhere 1")
    assert result.status == status and result.point is None
    assert fallback.called == (status == RouteStatus.NOT_FOUND)  # an outage / quota never goes to Nominatim
    await client.aclose()


@respx.mock
async def test_compute_with_addresses_geocodes_then_routes() -> None:
    """Called directly (doctor --live-maps), the adapter geocodes addresses itself; coordinates pass through."""
    search = respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=collection(feature(*HOME))))
    route = respx.post(DIRECTIONS.format("driving-car")).mock(return_value=httpx.Response(200, json=route_json(600)))
    client = api()
    result = await client.compute("Blue Door Cafe, Tbilisi", coords(CENTRE), datetime.now(UTC), "DRIVE")
    assert (result.status, result.duration_seconds) == (RouteStatus.OK, 600)
    assert search.call_count == 1
    assert json.loads(route.calls[0].request.content)["coordinates"][0] == [HOME[1], HOME[0]]
    await client.aclose()


@respx.mock
async def test_unknown_address_stops_before_routing() -> None:
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=collection()))
    respx.get(NOMINATIM).mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(DIRECTIONS.format("driving-car"))
    client = api()
    result = await client.compute("Nowhere 1", coords(CENTRE), datetime.now(UTC), "DRIVE")
    assert result.status == RouteStatus.NOT_FOUND and result.duration_seconds is None
    assert not route.called
    await client.aclose()


@respx.mock
async def test_nominatim_finds_what_pelias_knows_only_as_the_city(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found live 2026-10-14: Pelias answered «Tbilisi» (locality) for «Blue Door Cafe, 12 Rustaveli Ave»;
    OpenStreetMap's Nominatim finds the building — used as a fallback, street level or finer only."""
    monkeypatch.setattr(OpenRouteServiceRoutesApi, "nominatim_interval", 0.0)
    respx.get(GEOCODE).mock(
        return_value=httpx.Response(200, json=collection(feature(41.7343, 44.8324, layer="locality")))
    )
    house = {
        "lat": "41.7238538",
        "lon": "44.7903612",
        "place_rank": 30,
        "display_name": "2ბ, გრიგოლ ლორთქიფანიძის ქუჩა",
    }
    city = {"lat": "41.7383", "lon": "44.8204", "place_rank": 16, "display_name": "თბილისი"}
    fallback = respx.get(NOMINATIM).mock(
        side_effect=[  # the full string with the venue name finds nothing, without it the house is found
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[house]),
            httpx.Response(200, json=[city]),
        ]
    )
    client = api()
    found = await client.geocode("Blue Door Cafe, 12 Rustaveli Ave, Tbilisi, Georgia")
    assert found.status == RouteStatus.OK and found.point is not None
    assert (round(found.point.latitude, 4), round(found.point.longitude, 4)) == (41.7239, 44.7904)
    assert [c.request.url.params["q"] for c in fallback.calls] == [
        "Blue Door Cafe, 12 Rustaveli Ave, Tbilisi, Georgia",
        "12 Rustaveli Ave, Tbilisi, Georgia",
    ]
    request = fallback.calls[0].request
    assert request.url.params["format"] == "jsonv2" and request.url.params["countrycodes"] == "ge"
    assert request.headers["user-agent"].startswith("JARVIS-personal-assistant/")
    assert "authorization" not in request.headers  # the ORS key never goes to another service
    coarse = await client.geocode("Somewhere, Tbilisi")
    assert coarse.status == RouteStatus.NOT_FOUND  # a city-level answer is not a place to route to
    await client.aclose()


# --- provider selection ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, None),
        ({"openrouteservice_api_key": KEY}, "openrouteservice"),
        ({"google_maps_api_key": "AIza-test"}, "google"),
        ({"google_maps_api_key": "AIza-test", "openrouteservice_api_key": KEY}, "google"),  # auto: traffic first
        (
            {"maps_provider": "openrouteservice", "google_maps_api_key": "AIza-test", "openrouteservice_api_key": KEY},
            "openrouteservice",
        ),
        ({"maps_provider": "google", "openrouteservice_api_key": KEY}, None),  # explicit provider without its key
        ({"maps_provider": "OpenRouteService", "openrouteservice_api_key": KEY}, "openrouteservice"),
        ({"openrouteservice_api_key": "   "}, None),
    ],
)
def test_maps_provider_selection(overrides: dict[str, object], expected: str | None) -> None:
    assert maps_provider_name(make_settings(**overrides)) == expected


def test_invalid_maps_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(maps_provider="mapbox")


async def test_build_maps_wires_routes_and_geocoder() -> None:
    ors_only = build_maps(make_settings(openrouteservice_api_key=KEY))
    assert isinstance(ors_only.routes, OpenRouteServiceRoutesApi)
    assert ors_only.geocoder is ors_only.routes and ors_only.status == "CONFIGURED (openrouteservice)"
    assert ors_only.routes.country == "GE" and ors_only.routes.focus == TBILISI
    both = build_maps(make_settings(google_maps_api_key="AIza-x", openrouteservice_api_key=KEY))
    assert isinstance(both.routes, HttpRoutesApi) and isinstance(both.geocoder, OpenRouteServiceRoutesApi)
    assert both.status == "CONFIGURED (google)"
    none = build_maps(make_settings(maps_provider="openrouteservice"))
    assert isinstance(none.routes, UnavailableRoutesApi) and none.geocoder is None
    assert "OPENROUTESERVICE_API_KEY" in missing_key_hint(make_settings(maps_provider="openrouteservice"))
    # Routing and geocoding have separate daily caps, so separate counters (one provider, two budgets).
    assert OpenRouteServiceRoutesApi.budget_key != OpenRouteServiceRoutesApi.geocode_budget_key
    for setup in (ors_only, both, none):
        for item in setup.closables:
            await item.aclose()
