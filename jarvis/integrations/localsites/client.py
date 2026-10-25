"""LocalSites (~/Projects/localsites) — the user's manager of local sites, through its «Пульт» API.

The Пульт listens on :7788; requests from this Mac (loopback — Docker's host.docker.internal arrives as 127.0.0.1)
need no password. LocalSites accepts only localhost / IP literals in `Host` (DNS-rebinding guard), so the header is
set to 127.0.0.1. Jarvis uses a closed set: state, start, stop, LAN on, tunnel on/off — never «all», never site
settings or commands. Site views carry access keys (`token`, keyed URLs): they are not logged.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from jarvis.core.errors import PermanentError, TransientError
from jarvis.core.http import ResilientHttp
from jarvis.core.retry import RetryPolicy

ACTIONS = ("start", "stop", "lan-on", "tunnel-on", "tunnel-off")


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    status: str  # stopped | starting | running | error
    port: int
    group: str = ""
    folder: str = ""
    archived: bool = False
    url: str | None = None  # http://localhost:<port> while running
    lan: bool = False
    lan_url: str | None = None  # with the site key
    tunnel_status: str = "off"  # off | starting | on | error
    tunnel_url: str | None = None  # with the site key (unless tunnel_open)
    tunnel_open: bool = False  # the tunnel lets anyone in without the key
    error: str | None = None
    tunnel_error: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @classmethod
    def from_view(cls, v: dict[str, Any]) -> Site:
        path = str(v.get("path") or "").rstrip("/")
        return cls(
            id=str(v["id"]),
            name=str(v.get("name") or path.rsplit("/", 1)[-1]),
            status=str(v.get("status") or "stopped"),
            port=int(v.get("port") or 0),
            group=str(v.get("group") or ""),
            folder=path.rsplit("/", 1)[-1],
            archived=bool(v.get("archived")),
            url=v.get("url") or None,
            lan=bool(v.get("lan")),
            lan_url=v.get("lanUrl") or None,
            tunnel_status=str(v.get("tunnelStatus") or "off"),
            tunnel_url=v.get("tunnelUrl") or None,
            tunnel_open=bool(v.get("tunnelOpen")),
            error=v.get("error") or None,
            tunnel_error=v.get("tunnelError") or None,
        )


@dataclass(frozen=True)
class SitesState:
    ip: str | None
    sites: tuple[Site, ...]

    def site(self, site_id: str) -> Site | None:
        return next((s for s in self.sites if s.id == site_id), None)


def parse_state(data: Any) -> SitesState:
    try:
        return SitesState(data.get("ip"), tuple(Site.from_view(v) for v in data["sites"]))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"localsites: unexpected state ({type(exc).__name__})") from exc


class SitesApi(Protocol):
    async def state(self) -> SitesState: ...

    async def action(self, site_id: str, action: str) -> SitesState:
        """One of ACTIONS for one site; returns the new state."""
        ...


class HttpSites:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        port = urlsplit(base_url).port or 7788
        self._http = ResilientHttp(
            "localsites",
            base_url=base_url.rstrip("/") + "/api/",
            timeout=30.0,  # a «command» site (Node app) may take a while to come up
            retry=RetryPolicy(base_seconds=0.5, cap_seconds=1.0, max_attempts=2, jitter_ratio=0),
            headers={"Host": f"127.0.0.1:{port}", "Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def state(self) -> SitesState:
        return parse_state((await self._http.get("state")).json())

    async def action(self, site_id: str, action: str) -> SitesState:
        if action not in ACTIONS:
            raise ValueError(f"localsites: action {action!r} is not allowed")
        response = await self._http.post(f"site/{site_id}/{action}")
        return parse_state(response.json())


@dataclass
class FakeSites:
    """In-memory LocalSites for tests: `sites` as views; start/stop/LAN/tunnel change them like the app does."""

    views: list[dict[str, Any]] = field(default_factory=list)
    ip: str = "192.168.1.20"
    unavailable: bool = False
    tunnel_delay: int = 0  # states to report «starting» before the tunnel URL appears
    calls: list[tuple[str, str]] = field(default_factory=list)
    on_start: Any = None  # callable(site_id) — e.g. LMF comes up when the TLT site starts
    _pending: dict[str, int] = field(default_factory=dict)

    def add(self, site_id: str, name: str, port: int, *, running: bool = False, **extra: Any) -> None:
        view = {
            "id": site_id, "name": name, "status": "running" if running else "stopped", "port": port, "group": "",
            "path": f"/Users/me/Sites/{name}", "archived": False, "lan": False, "token": f"key-{site_id}",
            "tunnelStatus": "off", "tunnelUrl": None, "tunnelOpen": False, "error": None, "tunnelError": None,
            "url": f"http://localhost:{port}" if running else None, "lanUrl": None,
        }  # fmt: skip
        view.update(extra)
        self.views.append(view)

    def _view(self, site_id: str) -> dict[str, Any]:
        view = next((v for v in self.views if v["id"] == site_id), None)
        if view is None:
            raise PermanentError("localsites: unknown site")
        return view

    def _snapshot(self) -> SitesState:
        for site_id, left in list(self._pending.items()):
            view = self._view(site_id)
            if left <= 0:
                view["tunnelStatus"] = "on"
                view["tunnelUrl"] = f"https://{site_id}.trycloudflare.com/" + (
                    "" if view["tunnelOpen"] else f"?k={view['token']}"
                )
                del self._pending[site_id]
            else:
                self._pending[site_id] = left - 1
        return parse_state({"ip": self.ip, "sites": copy.deepcopy(self.views)})

    async def state(self) -> SitesState:
        if self.unavailable:
            raise TransientError("localsites: connect error (fake)")
        return self._snapshot()

    async def action(self, site_id: str, action: str) -> SitesState:
        if self.unavailable:
            raise TransientError("localsites: connect error (fake)")
        if action not in ACTIONS:
            raise ValueError(action)
        self.calls.append((site_id, action))
        view = self._view(site_id)
        if action == "start":
            view["status"], view["url"] = "running", f"http://localhost:{view['port']}"
            if self.on_start is not None:
                self.on_start(site_id)
        elif action == "stop":
            view["status"], view["url"], view["lanUrl"] = "stopped", None, None
            view["tunnelStatus"], view["tunnelUrl"] = "off", None
        elif action == "lan-on":
            view["lan"] = True
        elif action == "tunnel-on":
            view["tunnelStatus"] = "starting"
            self._pending[site_id] = self.tunnel_delay
        elif action == "tunnel-off":
            view["tunnelStatus"], view["tunnelUrl"] = "off", None
        if view["status"] == "running" and view["lan"]:
            view["lanUrl"] = f"http://{self.ip}:{view['port']}/?k={view['token']}"
        return self._snapshot()
