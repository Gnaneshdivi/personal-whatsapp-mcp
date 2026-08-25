"""The /events stream.

This endpoint returned 500 for the entire build. It was constructed with
Starlette's plain `Response`, which treats its first argument as a body rather
than something to iterate, so every live update the server produced was thrown
away and the UI silently degraded to its background poll. Nothing caught it
because no test ever read the response: the route existed, the handler was
correct, and the browser's EventSource retries a failed connection quietly.

So these assert the two things that were actually broken — that the response
streams at all, and that an emitted event reaches a reader.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn

from wa_mcp.config import Settings, resolve_storage


@pytest.fixture
async def server(tmp_path, monkeypatch):
    """A real uvicorn server, not an in-process ASGI transport.

    httpx's ASGITransport joins the whole body before returning a response, so
    a stream that never ends never returns -- and, more to the point, an
    endpoint that streams and one that doesn't look identical through it. That
    is the exact distinction under test, so this binds a real port.
    """
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    settings = Settings(host="127.0.0.1", port=0, auth_token="t0ken")
    storage = resolve_storage("", tmp_path)

    import wa_mcp.app as appmod

    app = appmod.create_app(settings, storage)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                        log_level="error", lifespan="on"))
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    assert srv.started, "server never came up"
    port = srv.servers[0].sockets[0].getsockname()[1]

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                 timeout=15.0) as c:
        yield c, "t0ken", appmod.RT

    srv.should_exit = True
    await asyncio.wait_for(task, timeout=10)


async def _read_events(client, token, *, want: int, timeout: float = 8.0):
    """Collect parsed SSE frames until `want` of them arrive."""
    frames, buf = [], ""
    async with client.stream("GET", f"/events?k={token}") as resp:
        assert resp.status_code == 200, "the stream must not error"
        assert resp.headers["content-type"].startswith("text/event-stream")

        async def pump():
            nonlocal buf
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    name = payload = None
                    for line in raw.splitlines():
                        if line.startswith("event: "):
                            name = line[7:]
                        elif line.startswith("data: "):
                            payload = line[6:]
                    if name:
                        frames.append((name, json.loads(payload or "null")))
                        if len(frames) >= want:
                            return

        try:
            await asyncio.wait_for(pump(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
    return frames


async def test_the_stream_opens_and_leads_with_status(server):
    """A freshly loaded page must learn the state without waiting for a tick.

    The banner is hidden by a status event. If the first one only arrives after
    a delay -- or never -- the page shows a stale "Syncing" bar over a
    connection that has been ready for hours.
    """
    client, token, runtime = server
    frames = await _read_events(client, token, want=1)
    assert frames, "no frames at all -- the response is not streaming"
    assert frames[0][0] == "status"
    assert "ready" in frames[0][1]


async def test_an_emitted_event_reaches_the_browser(server):
    """The whole point: something the runtime emits shows up on the wire."""
    client, token, runtime = server

    async def emit_soon():
        await asyncio.sleep(0.4)
        await runtime._fanout("message.delivered", {
            "message_ids": ["ABC123"], "status": "delivered",
            "chat_jid": "1@s.whatsapp.net"})

    task = asyncio.create_task(emit_soon())
    frames = await _read_events(client, token, want=3)
    await task

    wa = [payload for name, payload in frames if name == "wa"]
    assert wa, f"emitted event never arrived; got {[n for n, _ in frames]}"
    assert wa[0]["message_ids"] == ["ABC123"]
    assert wa[0]["status"] == "delivered"


async def test_a_burst_is_not_metered_out_one_per_tick(server):
    """Receipts arrive in groups.

    Draining one event per two-second tick meant a dozen ticked messages took
    half a minute to update -- long enough to read as "ticks are broken".
    """
    client, token, runtime = server

    async def emit_burst():
        await asyncio.sleep(0.4)
        for i in range(5):
            await runtime._fanout("message.read", {
                "message_ids": [f"M{i}"], "status": "read",
                "chat_jid": "1@s.whatsapp.net"})

    task = asyncio.create_task(emit_burst())
    # Five events plus the leading status, well inside a single 2s window.
    frames = await _read_events(client, token, want=6, timeout=3.0)
    await task

    wa = [p for n, p in frames if n == "wa"]
    assert len(wa) == 5, f"burst was metered: only {len(wa)} of 5 arrived in 3s"
