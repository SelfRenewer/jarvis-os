"""LocalSites from chat: open a site on the Mac / at home (LAN) / away (tunnel that closes itself), stop, list,
«открой TLT» without the word «сайт», and LMF (the TLT site) started when the tracker needs it. Fakes only."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from jarvis.bot.service import BotService
from jarvis.db.models import MacAction

USER = 1001
pytestmark = pytest.mark.db


def msg(text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": USER},
            "chat": {"id": USER, "type": "private"},
        },
    }


def tap(data: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": USER},
            "message": {"message_id": 1, "chat": {"id": USER, "type": "private"}},
        },
    }


def buttons(c: Any) -> dict[str, str]:
    markup = c.telegram.sent[-1].get("reply_markup") or {}
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


def sites(c: Any) -> Any:
    fake = c.sites_api
    fake.add("tlt", "TLT", 5181, path="/Users/me/Sites/TLT")
    fake.add("stom", "Stomatology", 5176, tunnelOpen=True)
    fake.add("tex", "Texas", 5177)
    fake.add("road", "Roadmap", 5180)
    return fake


async def mac_online(c: Any) -> None:
    async with c.db.session() as s:
        await c.desktop.hello(s, c.clock.now(), apps=["Safari"], projects=[], version="1.0")


async def test_open_away_with_a_tunnel_that_closes_itself(container: Any, worker: Any) -> None:
    c = container
    fake = sites(c)
    bot = BotService(c)
    await bot.handle_update(msg("открой сайт roadmap", 1))
    assert c.telegram.texts()[-1] == "🌐 Roadmap запущен (на Mac: http://localhost:5180). Где открыть?"
    assert set(buttons(c)) == {"🏠 Я дома", "🌍 Я не дома", "💻 Открыть на Mac"}
    assert fake.calls == [("road", "start")]
    await bot.handle_update(tap(buttons(c)["🌍 Я не дома"], 2))
    assert c.telegram.texts()[-1] == (
        "🌍 Roadmap через интернет: https://road.trycloudflare.com/?k=key-road\n"
        "Закрою туннель сам через 3 ч — или скажите «закрой туннель»."
    )
    c.clock.advance(timedelta(hours=3, minutes=1))
    await worker.drain()
    assert ("road", "tunnel-off") in fake.calls
    assert c.telegram.texts()[-1].startswith("🔒 Закрыл туннель Roadmap (прошло 3 ч).")


async def test_away_in_words_and_an_open_tunnel_warning(container: Any) -> None:
    c = container
    fake = sites(c)
    fake.tunnel_delay = 1  # the URL appears on the next state poll
    bot = BotService(c)
    await bot.handle_update(msg("запусти сайт стоматологию, я не дома", 10))
    text = c.telegram.texts()[-1]
    assert text.startswith("🌍 Stomatology через интернет: https://stom.trycloudflare.com/\n")
    assert text.endswith("⚠️ У этого сайта туннель пускает без ключа — ссылка открыта всем, у кого она есть.")


async def test_open_at_home_and_on_the_mac(container: Any) -> None:
    c = container
    fake = sites(c)
    bot = BotService(c)
    await bot.handle_update(msg("открой сайт техас дома", 20))
    assert c.telegram.texts()[-1].startswith("🏠 Texas в домашней сети: http://192.168.1.20:5177/?k=key-tex")
    assert fake.calls == [("tex", "start"), ("tex", "lan-on")]
    await mac_online(c)
    await bot.handle_update(msg("открой roadmap", 21))  # not an app on the Mac → the LocalSites site
    await bot.handle_update(tap(buttons(c)["💻 Открыть на Mac"], 22))
    assert c.telegram.texts()[-1] == "🖥 Roadmap запущен — открываю на Mac: http://localhost:5180."
    async with c.db.session() as s:
        action = (await s.scalars(select(MacAction))).one()
    assert (action.kind, action.payload["url"]) == ("open_url", "http://localhost:5180")


async def test_tlt_opens_in_jarvis_os_on_the_mac(container: Any) -> None:
    """16.10: TLT moved from LocalSites to Jarvis OS — «открой TLT» / «открой сайт TLT» open its page on the Mac."""
    c = container
    fake = sites(c)
    await mac_online(c)
    bot = BotService(c)
    await bot.handle_update(msg("открой TLT", 60))
    assert c.telegram.texts()[-1] == "📓 Открываю TLT на Mac."
    await bot.handle_update(msg("открой сайт TLT, я не дома", 61))
    assert c.telegram.texts()[-1].startswith("📓 Открываю TLT на Mac. Ссылки «дома / не дома» для TLT пока нет")
    async with c.db.session() as s:
        actions = (await s.scalars(select(MacAction))).all()
    assert [(a.kind, a.payload["path"]) for a in actions] == [("open_jarvis", "/tlt"), ("open_jarvis", "/tlt")]
    assert fake.calls == []  # LocalSites is not asked


async def test_stop_list_and_close_tunnel(container: Any) -> None:
    c = container
    fake = sites(c)
    fake.views[1]["status"] = "running"
    fake.views[1]["tunnelStatus"], fake.views[1]["tunnelUrl"] = "on", "https://stom.trycloudflare.com/"
    bot = BotService(c)
    await bot.handle_update(msg("какие сайты запущены?", 30))
    assert c.telegram.texts()[-1].splitlines()[:2] == [
        "🌐 Сайты LocalSites: запущено 1 из 4",
        "• Stomatology — http://localhost:5176 · 🌍 туннель",
    ]
    await bot.handle_update(msg("закрой туннель", 31))
    assert c.telegram.texts()[-1] == "🔒 Туннель закрыт: Stomatology."
    await bot.handle_update(msg("останови сайт стоматология", 32))
    assert c.telegram.texts()[-1] == "⏹ Stomatology остановлен."
    await bot.handle_update(msg("открой сайт несуществующий", 33))
    assert c.telegram.texts()[-1] == (
        "Не нашёл сайт «несуществующий» в LocalSites. Есть: TLT, Stomatology, Texas, Roadmap."
    )


async def test_lmf_down_starts_the_tlt_site(container: Any) -> None:
    c = container
    fake = sites(c)
    c.lmf_api.unavailable = True

    def lmf_up(site_id: str) -> None:
        if site_id == "tlt":
            c.lmf_api.unavailable = False

    fake.on_start = lmf_up
    bot = BotService(c)
    bot.LMF_START_WAIT = 0
    await bot.handle_update(msg("спал 7:30, оценка 7", 40))
    assert fake.calls == [("tlt", "start")]
    assert c.telegram.texts()[-1].startswith("📓 Записать в TLT · сегодня, ср 23.09?")


@respx.mock
async def test_lmf_down_is_woken_through_jarvis_os_first(container: Any) -> None:
    """Jarvis OS runs TLT since 16.10: it is asked first (with the site key); LocalSites only if it does not answer."""
    from jarvis.core.security import desktop_token
    from jarvis.system.site import site_key

    c = container
    fake = sites(c)
    c.settings.system_site_url = "http://jarvis-os.test:39110"
    c.settings.telegram_bot_token = SecretStr("123:test-bot-token")  # the desktop token and the site key derive from it
    route = respx.post("http://jarvis-os.test:39110/api/os/services/tlt/ensure").mock(
        return_value=httpx.Response(200, json={"state": "running"})
    )
    assert await c.sites.ensure_running("TLT") is True
    token = desktop_token(c.settings.jarvis_encryption_key, c.settings.telegram_bot_token)
    assert route.calls[0].request.headers["authorization"] == f"Bearer {site_key(token or '')}"
    assert fake.calls == []
    route.mock(return_value=httpx.Response(503, json={"error": "down"}))
    assert await c.sites.ensure_running("TLT") is True  # Jarvis OS could not → LocalSites starts it
    assert fake.calls == [("tlt", "start")]


async def test_localsites_down(container: Any) -> None:
    c = container
    c.sites_api.unavailable = True
    await BotService(c).handle_update(msg("какие сайты запущены?", 50))
    assert c.telegram.texts()[-1].startswith("LocalSites не отвечает")
