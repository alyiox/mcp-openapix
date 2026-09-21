"""Tests for the meta-tool wrappers (defaults resolution + return shapes)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_openapix.api_client import ApiClient
from mcp_openapix.auth import TokenProvider
from mcp_openapix.config import Config, ConfigError
from mcp_openapix.responses import ResponseCache, read_cached_response
from mcp_openapix.spec_loader import SpecRegistry
from mcp_openapix.tools.call_endpoint import call_endpoint
from mcp_openapix.tools.describe_endpoint import describe_endpoint
from mcp_openapix.tools.list_endpoints import list_endpoints
from mcp_openapix.tools.list_platforms import list_platforms


def test_list_platforms_returns_regions_and_envs(
    config: Config, registry: SpecRegistry
) -> None:
    result = list_platforms(config=config, registry=registry)
    assert len(result) == 1
    demo = result[0]
    assert demo["name"] == "demo"
    regions = {r["name"]: r["services"] for r in demo["regions"]}
    assert regions["us"] == {"api": {"envs": ["dev", "prod"], "desc": "Demo items API"}}
    assert regions["eu"] == {"api": {"envs": ["prod"]}}


def test_list_platforms_region_filter_drops_non_served(
    config: Config, registry: SpecRegistry
) -> None:
    # demo serves us/eu; cn is not in its regions list → filter should hide it
    result = list_platforms(config=config, registry=registry, region="cn")
    assert result == []


async def test_list_endpoints_uses_default_platform(
    config: Config, registry: SpecRegistry
) -> None:
    ops = await list_endpoints(config=config, registry=registry)
    ids = {op["operation_id"] for op in ops}
    assert ids == {"GET /api/items/{id}", "POST /api/items"}
    assert all(op["region"] == "us" and op["service"] == "api" for op in ops)


async def test_list_endpoints_query_filter(
    config: Config, registry: SpecRegistry
) -> None:
    ops = await list_endpoints(config=config, registry=registry, query="items/{id}")
    assert [op["operation_id"] for op in ops] == ["GET /api/items/{id}"]


async def test_list_endpoints_method_filter(
    config: Config, registry: SpecRegistry
) -> None:
    ops = await list_endpoints(config=config, registry=registry, method="post")
    assert [op["operation_id"] for op in ops] == ["POST /api/items"]


async def test_list_endpoints_no_default_platform_raises(
    config: Config, registry: SpecRegistry
) -> None:
    config.defaults.platform = None
    with pytest.raises(ConfigError, match="missing 'platform'"):
        await list_endpoints(config=config, registry=registry)


async def test_describe_endpoint_includes_ref_closure(
    config: Config, registry: SpecRegistry
) -> None:
    result = await describe_endpoint(
        config=config, registry=registry, operation_id="GET /api/items/{id}"
    )
    assert result["method"] == "GET"
    assert result["service"] == "api"
    schemas = result["components"]["schemas"]
    # Transitive: Item → Owner; Unused must NOT appear.
    assert set(schemas) == {"Item", "Owner"}


@pytest.fixture
def api_client(
    config: Config,
    registry: SpecRegistry,
    http_client: httpx.AsyncClient,
    tokens: TokenProvider,
) -> ApiClient:
    return ApiClient(config, registry, http_client, tokens)


async def test_call_endpoint_small_body_not_truncated(
    config: Config, api_client: ApiClient
) -> None:
    cache = ResponseCache()
    with respx.mock() as mock:
        mock.get("https://api.example.com/demo/api/items/1").mock(
            return_value=httpx.Response(200, json={"id": "1"})
        )
        result = await call_endpoint(
            config=config,
            api_client=api_client,
            response_cache=cache,
            operation_id="GET /api/items/{id}",
            path_params={"id": "1"},
        )
    assert result["status"] == 200
    assert result["body"] == {"id": "1"}
    assert "truncated" not in result
    assert "resource_uri" not in result
    # A small body is not cached, but its curl command always is.
    request_id = result["curl_uri"].removeprefix("openapi://curl/")
    curl = cache.get(f"curl/{request_id}")
    assert curl is not None
    assert curl.startswith("curl -X GET 'https://api.example.com/demo/api/items/1'")
    assert "Authorization: Bearer us-token" in curl
    assert cache.list_ids() == [f"curl/{request_id}"]


async def test_call_endpoint_raw_method_and_path(
    config: Config, api_client: ApiClient
) -> None:
    cache = ResponseCache()
    with respx.mock() as mock:
        mock.post("https://api.example.com/demo/api/unpublished").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        result = await call_endpoint(
            config=config,
            api_client=api_client,
            response_cache=cache,
            method="post",
            path="/api/unpublished",
            body={"q": 1},
        )
    assert result["status"] == 200
    assert result["body"] == {"ok": True}


async def test_call_endpoint_large_body_truncated_and_cached(
    config: Config, api_client: ApiClient
) -> None:
    config.truncate_threshold = 1024
    cache = ResponseCache()
    payload = {"items": [{"i": i, "pad": "x" * 50} for i in range(200)]}
    with respx.mock() as mock:
        mock.get("https://api.example.com/demo/api/items/9").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await call_endpoint(
            config=config,
            api_client=api_client,
            response_cache=cache,
            operation_id="GET /api/items/{id}",
            path_params={"id": "9"},
        )
    assert result["truncated"] is True
    assert result["body"].endswith("(truncated — full body at resource_uri)")
    assert len(result["body"].encode("utf-8")) < 1024 + 100
    # The URI carries the request id and resolves to the full body in the cache.
    request_id = result["resource_uri"].removeprefix("openapi://responses/")
    assert request_id in cache.list_ids()
    restored = read_cached_response(request_id, cache)
    assert restored is not None
    assert '"i": 199' in restored  # the tail that the preview dropped
