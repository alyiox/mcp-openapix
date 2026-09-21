"""End-to-end resources/read over an in-memory client/server pair.

The unit tests in test_responses.py call the pure helpers directly, so they stay
green even when the registered resource is unreachable over JSON-RPC. These
tests drive the real request path, which is where the Context-revalidation bug
(every read failing with "Error creating resource from template") showed up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp_openapix import server as server_mod
from mcp_openapix.config import Config
from mcp_openapix.responses import ResponseCache


@asynccontextmanager
async def connected(
    monkeypatch: pytest.MonkeyPatch, config: Config, specs_dir: Path
) -> AsyncIterator[tuple[ClientSession, ResponseCache]]:
    """Run the real server in-process and yield a client plus its response cache.

    The cache is created inside the server lifespan, so it is monkeypatched to a
    shared instance the test can seed.
    """
    cache = ResponseCache(ttl_seconds=60)
    monkeypatch.setattr(server_mod, "load_config", lambda: config)
    monkeypatch.setattr(server_mod, "_specs_dir", lambda: specs_dir)
    monkeypatch.setattr(server_mod, "ResponseCache", lambda **_: cache)

    low = server_mod.mcp._lowlevel_server
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: low.run(
                    server_streams[0],
                    server_streams[1],
                    low.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                yield session, cache
            tg.cancel_scope.cancel()


async def _read(session: ClientSession, uri: str) -> str:
    result = await session.read_resource(uri)
    assert result.contents, f"no contents for {uri}"
    text = getattr(result.contents[0], "text", None)
    assert text is not None
    return text


@pytest.mark.anyio
async def test_read_cached_response_resource(
    monkeypatch: pytest.MonkeyPatch, config: Config, specs_dir: Path
) -> None:
    async with connected(monkeypatch, config, specs_dir) as (session, cache):
        cache.put("req-1", {"id": 1, "name": "京东"})
        text = await _read(session, "openapi://responses/req-1")
        assert '"id": 1' in text
        assert "京东" in text


@pytest.mark.anyio
async def test_read_cached_response_missing_id_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, config: Config, specs_dir: Path
) -> None:
    """A miss must return an explanatory body, not a JSON-RPC error."""
    async with connected(monkeypatch, config, specs_dir) as (session, _cache):
        assert "No cached response found" in await _read(
            session, "openapi://responses/nope"
        )


@pytest.mark.anyio
async def test_read_curl_resource(
    monkeypatch: pytest.MonkeyPatch, config: Config, specs_dir: Path
) -> None:
    async with connected(monkeypatch, config, specs_dir) as (session, cache):
        cache.put("curl/req-2", "curl -X GET https://api.example.com/demo/api/items")
        text = await _read(session, "openapi://curl/req-2")
        assert "https://api.example.com/demo/api/items" in text
