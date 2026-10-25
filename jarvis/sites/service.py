"""Sites of LocalSites from chat and voice: «открой сайт TLT», «запусти стоматологию, я не дома», «какие сайты
запущены?», «закрой туннель».

A site is found by its name (or folder) by ear — Russian or English, translit, word by word. Away from home a
cloudflared tunnel is opened (the link carries the site key; the tunnel closes by itself after
`SITES_TUNNEL_HOURS`); at home the site is opened to the local network (also keyed). Ids come from LocalSites.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from jarvis.core.config import Settings
from jarvis.core.errors import NotConfiguredError, TransientError
from jarvis.desktop.service import Ambiguous, best_match
from jarvis.integrations.localsites.client import Site, SitesApi, SitesState

log = logging.getLogger(__name__)

_NOISE = re.compile(r"^(?:мне\s+|мой\s+|мою\s+)?(?:сайт\w*|лендинг\w*|проект\w*)\s+", re.IGNORECASE)
WHERE_AWAY = re.compile(
    r"[,\s]*(?:я\s+не\s+дома|не\s+дома|я\s+на\s+улице|на\s+улице|я\s+в\s+городе|(?:через|в)\s+(?:интернет\w*|туннел\w*)"
    r"|с\s+телефона|для\s+телефона|наружу)\s*$",
    re.IGNORECASE,
)
WHERE_HOME = re.compile(
    r"[,\s]*(?:я\s+дома|дома|в\s+(?:домашней\s+)?сети|по\s+wi-?fi|по\s+вай-?фаю?)\s*$", re.IGNORECASE
)
WHERE_MAC = re.compile(r"[,\s]*(?:на\s+(?:маке|макбуке|mac|компьютере|ноутбуке)|здесь|тут)\s*$", re.IGNORECASE)


def split_where(said: str) -> tuple[str, str | None]:
    """«стоматологию, я не дома» → («стоматологию», «away»); «tlt на маке» → («tlt», «mac»)."""
    for pattern, where in ((WHERE_AWAY, "away"), (WHERE_HOME, "home"), (WHERE_MAC, "mac")):
        if m := pattern.search(said):
            return said[: m.start()].strip(" ,"), where
    return said.strip(" ,"), None


@dataclass(frozen=True)
class Found:
    site: Site | None
    options: list[str]  # ambiguous names


class SitesService:
    def __init__(self, settings: Settings, api: SitesApi | None) -> None:
        self.settings = settings
        self.api = api

    def _api(self) -> SitesApi:
        if self.api is None:
            raise NotConfiguredError("LocalSites не подключён (LOCALSITES_ENABLED=false)")
        return self.api

    async def state(self) -> SitesState:
        return await self._api().state()

    async def find(self, said: str) -> tuple[Found, SitesState]:
        state = await self.state()
        name = _NOISE.sub("", said.strip().strip("«»\"'")).strip()
        # Names and folders, with «x» heard as «х» (Texas = «техас»).
        keys: dict[str, Site] = {}
        for site in state.sites:
            for key in {site.name, site.folder}:
                if key:
                    keys.setdefault(key, site)
                    keys.setdefault(re.sub("[xX]", "h", key), site)
        found = best_match(name, list(keys), threshold=0.72)
        if isinstance(found, Ambiguous):
            sites = {keys[o].id: keys[o] for o in found.options}
            if len(sites) == 1:
                return Found(next(iter(sites.values())), []), state
            live = [s for s in sites.values() if not s.archived]
            return Found(None, [s.name for s in (live or list(sites.values()))]), state
        return Found(keys.get(found) if found else None, []), state

    async def start(self, site: Site) -> Site:
        if site.running:
            return site
        state = await self._api().action(site.id, "start")
        started = state.site(site.id) or site
        if started.status == "error":
            raise TransientError(f"LocalSites не запустил «{site.name}»: {started.error or 'ошибка'}")
        return started

    async def stop(self, site: Site) -> Site:
        api = self._api()
        if site.tunnel_status != "off":
            await api.action(site.id, "tunnel-off")
        state = await api.action(site.id, "stop")
        return state.site(site.id) or site

    async def open_home(self, site: Site) -> Site:
        """Home network: LAN access for this site (LocalSites remembers it; links carry the site key)."""
        site = await self.start(site)
        if site.lan and site.lan_url:
            return site
        state = await self._api().action(site.id, "lan-on")
        return state.site(site.id) or site

    async def open_away(self, site: Site, *, wait_seconds: float = 25.0) -> Site:
        """A cloudflared tunnel; waits until LocalSites reports its public URL."""
        api = self._api()
        site = await self.start(site)
        if site.tunnel_status == "on" and site.tunnel_url:
            return site
        state = await api.action(site.id, "tunnel-on")
        current = state.site(site.id) or site
        waited = 0.0
        while current.tunnel_status == "starting" or (current.tunnel_status == "on" and not current.tunnel_url):
            if waited >= wait_seconds:
                break
            await asyncio.sleep(1.0)
            waited += 1.0
            current = (await api.state()).site(site.id) or current
        if current.tunnel_status == "error":
            raise TransientError(f"туннель не поднялся: {current.tunnel_error or 'ошибка cloudflared'}")
        return current

    async def close_tunnel(self, site: Site) -> Site:
        state = await self._api().action(site.id, "tunnel-off")
        return state.site(site.id) or site

    async def ensure_running(self, name: str) -> bool:
        """«TLT» (LMF) is down → start it: through Jarvis OS, which runs TLT since 16.10 (the user moved it out of
        LocalSites), else in LocalSites. True when it was started now."""
        if name.lower() == self.settings.lmf_site_name.lower() and await self._wake_via_os():
            return True
        if self.api is None:
            return False
        try:
            state = await self.api.state()
            site = next((s for s in state.sites if s.name.lower() == name.lower()), None)
            if site is None or site.running:
                return False
            await self.start(site)
            return True
        except Exception as exc:
            log.info("localsites: could not start %s: %s", name, exc)
            return False

    async def _wake_via_os(self) -> bool:
        """POST {system_site_url}/api/os/services/tlt/ensure with the site key (Jarvis OS starts TLT and answers once
        its port is up). False when Jarvis OS is not running or not configured — LocalSites is tried then."""
        import httpx

        from jarvis.core.security import desktop_token
        from jarvis.system.site import site_key

        token = desktop_token(self.settings.jarvis_encryption_key, self.settings.telegram_bot_token)
        if not token or not self.settings.system_site_url:
            return False
        url = self.settings.system_site_url.rstrip("/") + "/api/os/services/tlt/ensure"
        try:
            async with httpx.AsyncClient(timeout=25.0) as http:
                response = await http.post(url, headers={"Authorization": f"Bearer {site_key(token)}"})
            state = response.json().get("state") if response.status_code == 200 else None
        except Exception as exc:  # best effort: Jarvis OS down / not running → LocalSites is tried
            log.info("jarvis os: could not wake TLT: %s", type(exc).__name__)
            return False
        return state in ("running", "external")

    @staticmethod
    def list_text(state: SitesState) -> str:
        running = [s for s in state.sites if s.running]
        lines = [f"🌐 Сайты LocalSites: запущено {len(running)} из {len(state.sites)}"]
        for s in running:
            extra = " · 🌍 туннель" if s.tunnel_status == "on" else ""
            lines.append(f"• {s.name} — http://localhost:{s.port}{extra}")
        groups: dict[str, list[str]] = {}
        for s in state.sites:
            if not s.running and not s.archived:
                groups.setdefault(s.group or "Без сферы", []).append(s.name)
        for group, names in groups.items():
            lines.append(f"○ {group}: {', '.join(names)}")
        lines.append("Открыть: «открой сайт TLT», вне дома: «открой сайт TLT, я не дома».")
        return "\n".join(lines)
