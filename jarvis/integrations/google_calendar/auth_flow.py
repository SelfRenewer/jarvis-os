"""Interactive Google OAuth (installed-app style with PKCE and a loopback redirect).

Run on a machine with a browser: `jarvis google-auth`. On a headless VPS, forward the port
(`ssh -L 8765:127.0.0.1:8765 vps`) or paste the final redirect URL when prompted.
The refresh token is stored encrypted (Fernet, JARVIS_ENCRYPTION_KEY) — never printed.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import urllib.parse

from jarvis.core.config import CalendarStrategy, Settings
from jarvis.db.session import Database
from jarvis.integrations.google_calendar.client import (
    SCOPE_MIRROR,
    SCOPES_READ,
    build_auth_url,
    exchange_code,
    pkce_pair,
)

PAGE_OK = "JARVIS: access received. Go back to the terminal — you can close this tab."
PAGE_STALE = (
    "JARVIS: this page belongs to an old authorization attempt and was ignored. "
    "Open the newest link printed in the terminal."
)
PAGE_DENIED = "JARVIS: access was not granted. Run `jarvis google-auth` again if that was a mistake."
PAGE_DONE = "JARVIS: already authorized in this run — you can close this tab."


class AuthDeniedError(Exception):
    """Google redirected back with `error=…` (the user pressed Cancel)."""


async def wait_for_code(state: str, port: int, timeout: float, *, read_stdin: bool = True) -> str:
    """Wait for the redirect carrying *our* `state` and return its `code`.

    Redirects with another state (an old browser tab from an earlier run, a forged link) are ignored instead of
    ending the flow: their code is bound to another PKCE verifier anyway. The stdin reader runs in a daemon
    thread, so a pending `input()` never keeps the process alive after the flow ends.
    """
    loop = asyncio.get_running_loop()
    received: asyncio.Future[str] = loop.create_future()

    def offer(url: str, source: str) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if received.done():
            return PAGE_DONE
        if query.get("state", [""])[0] != state:
            print(f"Ignored a {source} from another authorization attempt — use the link above.")
            return PAGE_STALE
        if "error" in query:
            received.set_exception(AuthDeniedError(query["error"][0]))
            return PAGE_DENIED
        code = query.get("code", [""])[0]
        if not code:
            print(f"Ignored a {source} without a code.")
            return PAGE_STALE
        received.set_result(code)
        return PAGE_OK

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode(errors="ignore")
        path = line.split(" ")[1] if " " in line else ""
        page = offer("http://127.0.0.1" + path, "browser redirect") if path.startswith("/callback") else ""
        status = b"200 OK" if page else b"404 Not Found"
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n"
            + page.encode()
        )
        await writer.drain()
        writer.close()

    def stdin_reader() -> None:
        while True:
            try:
                line = input()
            except (EOFError, OSError):
                return
            if line.strip():
                loop.call_soon_threadsafe(offer, line.strip(), "pasted URL")

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    if read_stdin:
        threading.Thread(target=stdin_reader, name="google-auth-stdin", daemon=True).start()
    try:
        return await asyncio.wait_for(received, timeout=timeout)
    finally:
        server.close()


async def run_google_auth(settings: Settings, port: int = 8765, timeout: float = 600) -> int:
    from jarvis.core.clock import SystemClock
    from jarvis.core.security import TokenCipher
    from jarvis.services.container import DbTokenStore

    if not settings.google_client_id or settings.google_client_secret is None:
        print("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set.")
        return 2
    if settings.jarvis_encryption_key is None:
        print("JARVIS_ENCRYPTION_KEY is not set (generate one with `jarvis gen-key`).")
        return 2
    scopes = list(SCOPES_READ)
    if settings.calendar_strategy == CalendarStrategy.MIRROR:
        scopes.append(SCOPE_MIRROR)
    redirect = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    print("Close Google tabs left from earlier attempts. Open this URL in a browser and grant access:\n")
    print(build_auth_url(settings.google_client_id, redirect, scopes, state, challenge))
    print(f"\nWaiting for the redirect on {redirect} (or paste the final URL here)…")
    try:
        code = await wait_for_code(state, port, timeout)
    except AuthDeniedError as exc:
        print(f"Google returned an error ({exc}) — access was not granted.")
        return 3
    except TimeoutError:
        print(f"No redirect within {int(timeout // 60)} minutes — run `jarvis google-auth` again.")
        return 5
    tokens = await exchange_code(
        settings.google_client_id, settings.google_client_secret.get_secret_value(), code, redirect, verifier
    )
    refresh = tokens.get("refresh_token")
    if not refresh:
        print("Google did not return a refresh token (revoke the old grant and retry).")
        return 4
    db = Database.from_url(settings.database_url, pool_size=1)
    try:
        store = DbTokenStore(db, TokenCipher(settings.jarvis_encryption_key.get_secret_value()), SystemClock())
        await store.save_refresh_token(refresh, tokens.get("scope", " ".join(scopes)))
    finally:
        await db.dispose()
    print("Google Calendar authorized. Refresh token stored encrypted.")
    return 0
