"""Tests for the API client: path params, headers, body, truncation."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx

from mcp_openapix.api_client import ApiClient, ApiClientError
from mcp_openapix.auth import TokenProvider
from mcp_openapix.config import Config, TokenHelperConfig
from mcp_openapix.spec_loader import SpecRegistry


@pytest.fixture
def api_client(
    config: Config,
    registry: SpecRegistry,
    http_client: httpx.AsyncClient,
    tokens: TokenProvider,
) -> ApiClient:
    return ApiClient(config, registry, http_client, tokens)


@pytest.mark.asyncio
async def test_call_substitutes_path_params(api_client: ApiClient) -> None:
    with respx.mock() as mock:
        route = mock.get("https://api.example.com/demo/api/items/42").mock(
            return_value=httpx.Response(200, json={"id": "42"})
        )
        resp = await api_client.call(
            operation_id="GET /api/items/{id}",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            path_params={"id": "42"},
        )
    assert resp.status == 200
    assert resp.body == {"id": "42"}
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer us-token"
    assert sent.headers["x-product"] == "demo"


@pytest.mark.asyncio
async def test_missing_path_param_raises(api_client: ApiClient) -> None:
    with pytest.raises(ApiClientError, match="missing path params"):
        await api_client.call(
            operation_id="GET /api/items/{id}",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            path_params={},
        )


@pytest.mark.asyncio
async def test_caller_cannot_override_authorization(api_client: ApiClient) -> None:
    with respx.mock() as mock:
        route = mock.get("https://api.example.com/demo/api/items/1").mock(
            return_value=httpx.Response(200, json={})
        )
        await api_client.call(
            operation_id="GET /api/items/{id}",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            path_params={"id": "1"},
            headers={"Authorization": "Bearer evil"},
        )
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer us-token"


@pytest.mark.asyncio
async def test_response_body_returned_whole(api_client: ApiClient) -> None:
    # The client no longer truncates; it returns the full body and a request_id.
    # Size-based truncation is the tool layer's concern (see test_tools.py).
    big = "x" * (150 * 1024)
    with respx.mock() as mock:
        mock.get("https://api.example.com/demo/api/items/9").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/plain"}, text=big
            )
        )
        resp = await api_client.call(
            operation_id="GET /api/items/{id}",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            path_params={"id": "9"},
        )
    assert resp.body == big
    assert resp.request_id


@pytest.mark.asyncio
async def test_response_carries_curl_command(api_client: ApiClient) -> None:
    with respx.mock() as mock:
        mock.post("https://api.example.com/demo/api/items").mock(
            return_value=httpx.Response(200, json={"created": True})
        )
        resp = await api_client.call(
            operation_id="POST /api/items",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            body={"name": "Widget"},
        )
    assert resp.curl is not None
    assert resp.curl.startswith("curl -X POST 'https://api.example.com/demo/api/items'")
    assert "-H 'Authorization: Bearer us-token'" in resp.curl
    assert '-d \'{"name": "Widget"}\'' in resp.curl


@pytest.mark.asyncio
async def test_timeout_is_forwarded_to_httpx(api_client: ApiClient) -> None:
    with respx.mock() as mock:
        route = mock.get("https://api.example.com/demo/api/items/1").mock(
            return_value=httpx.Response(200, json={})
        )
        await api_client.call(
            operation_id="GET /api/items/{id}",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            path_params={"id": "1"},
            timeout=5.0,
        )
    # httpx records the effective timeout on the request's extensions.
    assert route.calls.last.request.extensions["timeout"]["read"] == 5.0


@pytest.mark.asyncio
async def test_raw_method_and_path_needs_no_spec(api_client: ApiClient) -> None:
    # An endpoint absent from the spec is reachable via raw method + path.
    with respx.mock() as mock:
        route = mock.get("https://api.example.com/demo/api/unpublished").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = await api_client.call(
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            method="get",
            path="/api/unpublished",
        )
    assert resp.status == 200
    assert resp.body == {"ok": True}
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer us-token"


@pytest.mark.asyncio
async def test_raw_path_without_leading_slash_is_normalized(
    api_client: ApiClient,
) -> None:
    with respx.mock() as mock:
        route = mock.get("https://api.example.com/demo/api/unpublished").mock(
            return_value=httpx.Response(200, json={})
        )
        await api_client.call(
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            method="get",
            path="api/unpublished",
        )
    assert route.calls.last.request.url.path == "/demo/api/unpublished"


@pytest.mark.asyncio
async def test_call_without_operation_id_or_path_raises(
    api_client: ApiClient,
) -> None:
    with pytest.raises(ApiClientError, match="requires either 'operation_id'"):
        await api_client.call(
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
        )


@pytest.mark.asyncio
async def test_post_sends_json_body(api_client: ApiClient) -> None:
    with respx.mock() as mock:
        route = mock.post("https://api.example.com/demo/api/items").mock(
            return_value=httpx.Response(200, json={"created": True})
        )
        await api_client.call(
            operation_id="POST /api/items",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            body={"name": "Widget"},
        )
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("application/json")
    assert b'"name"' in sent.content


@pytest.mark.asyncio
async def test_auth_header_replaces_any_casing_of_its_name(
    api_client: ApiClient,
) -> None:
    """Two spellings of one name are two headers on the wire, not one."""
    with respx.mock() as mock:
        route = mock.get("https://api.example.com/demo/api/items/1").mock(
            return_value=httpx.Response(200, json={})
        )
        await api_client.call(
            operation_id="GET /api/items/{id}",
            platform="demo",
            region="us",
            service="api",
            env="prod",
            username="alice",
            path_params={"id": "1"},
            headers={"authorization": "Bearer evil"},
        )
    sent = route.calls.last.request
    assert sent.headers.get_list("authorization") == ["Bearer us-token"]


@pytest.mark.asyncio
async def test_401_retires_the_cached_token(
    config: Config,
    registry: SpecRegistry,
    http_client: httpx.AsyncClient,
    tokens: TokenProvider,
    tmp_path: Path,
) -> None:
    """A 401 retires the token, whatever its expiry claimed, so the next call
    mints a fresh one rather than replaying a credential already refused."""
    counter = tmp_path / "mints.txt"
    script = tmp_path / "mint.py"
    script.write_text(
        f"import pathlib\nc = pathlib.Path({str(counter)!r})\n"
        "c.write_text(str(int(c.read_text()) + 1) if c.exists() else '1')\n"
        "print('us-token')\n",
        encoding="utf-8",
    )
    minting = TokenHelperConfig(command=sys.executable, args=[str(script)])
    config = config.model_copy(
        update={"token_helpers": {**config.token_helpers, "us": minting}}
    )
    client = ApiClient(config, registry, http_client, tokens)

    async def call(status: int) -> None:
        with respx.mock() as mock:
            mock.get("https://api.example.com/demo/api/items/1").mock(
                return_value=httpx.Response(status, json={})
            )
            await client.call(
                operation_id="GET /api/items/{id}",
                platform="demo",
                region="us",
                service="api",
                env="prod",
                username="alice",
                path_params={"id": "1"},
            )

    await call(200)
    await call(401)
    assert int(counter.read_text()) == 1
    await call(200)
    assert int(counter.read_text()) == 2
