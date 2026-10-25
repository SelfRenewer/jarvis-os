"""Loopback part of `jarvis google-auth` (found live 2026-10-01).

A Google tab left over from an earlier run redirected with the old `state`; the flow aborted on the first redirect
and then hung on a pending `input()`. Now stale redirects are ignored and the flow keeps waiting for its own.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from jarvis.integrations.google_calendar.auth_flow import (
    PAGE_OK,
    PAGE_STALE,
    AuthDeniedError,
    wait_for_code,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def get(port: int, path: str) -> str:
    for _ in range(50):  # the server may not be listening yet
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            break
        except ConnectionRefusedError:
            await asyncio.sleep(0.02)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    response = (await reader.read()).decode()
    writer.close()
    return response


async def test_stale_redirect_is_ignored_and_own_one_is_accepted() -> None:
    port = free_port()
    waiter = asyncio.create_task(wait_for_code("NEW", port, timeout=5, read_stdin=False))
    assert (await get(port, "/favicon.ico")).startswith("HTTP/1.1 404")
    assert (await get(port, "/callback?state=OLD&code=old-code")).endswith(PAGE_STALE)
    assert (await get(port, "/callback?state=NEW")).endswith(PAGE_STALE)  # no code
    assert not waiter.done()
    assert (await get(port, "/callback?state=NEW&code=good-code&scope=x")).endswith(PAGE_OK)
    assert await waiter == "good-code"


async def test_cancel_on_the_consent_screen_ends_the_flow() -> None:
    port = free_port()
    waiter = asyncio.create_task(wait_for_code("S", port, timeout=5, read_stdin=False))
    await get(port, "/callback?state=OTHER&error=access_denied")  # someone else's error: ignored
    assert not waiter.done()
    await get(port, "/callback?state=S&error=access_denied")
    with pytest.raises(AuthDeniedError, match="access_denied"):
        await waiter


async def test_timeout_releases_the_port() -> None:
    port = free_port()
    with pytest.raises(TimeoutError):
        await wait_for_code("S", port, timeout=0.05, read_stdin=False)
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)  # the port is free again
    server.close()
