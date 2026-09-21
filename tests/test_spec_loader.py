"""Tests for spec_loader: registry building, indexing, synthesized IDs, refs."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from mcp_openapi.config import Config
from mcp_openapi.spec_loader import (
    SpecError,
    SpecRegistry,
    build_registry,
    synthesize_operation_id,
)

_SPEC_URL = "https://api.example.com/demo/swagger/v1/swagger.json"


def test_synthesized_id_format() -> None:
    assert synthesize_operation_id("post", "/api/x") == "POST /api/x"
    assert synthesize_operation_id("GET", "/y/{id}") == "GET /y/{id}"


def test_build_registry_parses(registry: SpecRegistry) -> None:
    assert registry.platform_names() == ["demo"]
    assert sorted(registry.platforms["demo"].regions) == ["eu", "us"]


async def test_get_operation_uses_synthesized_id(registry: SpecRegistry) -> None:
    op = await registry.get_operation("demo", "us", "api", "GET /api/items/{id}")
    assert op.method == "get"
    assert op.path == "/api/items/{id}"
    assert (op.region, op.service) == ("us", "api")


async def test_get_operation_unknown_id(registry: SpecRegistry) -> None:
    with pytest.raises(SpecError, match="not found"):
        await registry.get_operation("demo", "us", "api", "GET /nope")


async def test_list_operations_returns_both_paths(registry: SpecRegistry) -> None:
    ops = await registry.list_operations("demo", "us", "api")
    ids = {op.operation_id for op in ops}
    assert ids == {"GET /api/items/{id}", "POST /api/items"}


async def test_resolve_refs_transitive_closure(registry: SpecRegistry) -> None:
    op = await registry.get_operation("demo", "us", "api", "GET /api/items/{id}")
    schemas = await registry.resolve_refs("demo", "us", "api", op)
    assert set(schemas) == {"Item", "Owner"}


async def test_resolve_refs_no_refs(registry: SpecRegistry) -> None:
    op = await registry.get_operation("demo", "us", "api", "POST /api/items")
    schemas = await registry.resolve_refs("demo", "us", "api", op)
    assert set(schemas) == {"CreateItem"}


async def test_get_spec_auto_fetches_and_caches(config: Config, tmp_path: Path) -> None:
    """A cache miss downloads the spec (unauthenticated) and writes it to disk."""
    cache_root = tmp_path / "cache"
    reg = build_registry(config, cache_root)
    spec_body = {"openapi": "3.0.1", "paths": {"/ping": {"get": {}}}}
    with respx.mock() as mock:
        route = mock.get(_SPEC_URL).mock(
            return_value=httpx.Response(200, json=spec_body)
        )
        spec = await reg.get_spec("demo", "us", "api")
    assert route.called
    assert spec == spec_body
    cached = cache_root / "demo" / "us" / "api.json"
    assert cached.is_file()


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"message": "Not Found"}, id="error body served as 200"),
        pytest.param({"openapi": "3.0.1", "paths": {}}, id="no paths"),
        pytest.param(
            {"openapi": "3.0.1", "paths": {"/a": {"summary": "x"}}}, id="no methods"
        ),
        pytest.param([{"openapi": "3.0.1"}], id="not an object"),
    ],
)
async def test_a_document_with_no_operations_is_not_cached(
    config: Config, tmp_path: Path, document: object
) -> None:
    """A 200 carrying something that is not a spec must not poison the cache."""
    cache_root = tmp_path / "cache"
    reg = build_registry(config, cache_root)
    with respx.mock() as mock:
        mock.get(_SPEC_URL).mock(return_value=httpx.Response(200, json=document))
        with pytest.raises(SpecError, match="no operations"):
            await reg.get_spec("demo", "us", "api")
    assert list(cache_root.rglob("*.json")) == []


async def test_get_spec_fetch_failure_raises(config: Config, tmp_path: Path) -> None:
    reg = build_registry(config, tmp_path / "cache")
    with respx.mock() as mock:
        mock.get(_SPEC_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SpecError, match="failed to fetch"):
            await reg.get_spec("demo", "us", "api")
