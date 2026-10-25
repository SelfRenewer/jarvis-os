"""A route in 2GIS as an https link (2GIS has live traffic and public transport in Tbilisi; its server API is
commercial, so Jarvis only links to it).

Telegram URL buttons accept only http(s)/tg links, so the app's own scheme (`dgis://2gis.ru/routeSearch/…`,
https://help.2gis.com/question/developers-launching-2gis-navigation-using-deeplink) cannot be used. The web route
`https://2gis.ge/<city>/directions/points/<lon>,<lat>|<lon>,<lat>` is the format 2GIS itself redirects to
(checked 2026-10-14: `/directions/points/…` → 302 to `/tbilisi/directions/points/…`, the page answers 200 with
«how to get from … to …»; coordinates are lon,lat — the swapped order lands in another country). The 2GIS iOS app
claims every path of 2gis.ge as a universal link (`/.well-known/apple-app-site-association`: `*`), so on a phone
with 2GIS the link opens the app; otherwise the web map.

Transport tabs the site keeps: `tab/pedestrian`, `tab/bus`; the car route is the default (no tab).
"""

from __future__ import annotations

from jarvis.integrations.maps.routes import GeoPoint

DEFAULT_BASE_URL = "https://2gis.ge/tbilisi"
TABS = {"WALK": "tab/pedestrian/", "TRANSIT": "tab/bus/"}  # anything else: by car (2GIS's default)


def _point(point: GeoPoint) -> str:
    return f"{point.longitude:.6f}%2C{point.latitude:.6f}"  # lon,lat (the comma percent-encoded)


def route_url(origin: GeoPoint, destination: GeoPoint, travel_mode: str = "DRIVE", base_url: str = "") -> str:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    tab = TABS.get(travel_mode.upper(), "")
    return f"{base}/directions/{tab}points/{_point(origin)}%7C{_point(destination)}"


__all__ = ["DEFAULT_BASE_URL", "route_url"]
