"""Background refresh: the lease, the interval, and what a sweep records."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from mcp_openapix import cache, refresh
from mcp_openapix.config import Config
from mcp_openapix.spec_loader import SpecRegistry, build_registry

_US_URL = "https://api.example.com/demo/swagger/v1/swagger.json"
_EU_URL = "https://api-eu.example.com/demo/swagger/v1/swagger.json"


def _doc(operations: int = 1) -> dict:
    return {
        "openapi": "3.0.1",
        "paths": {f"/op{i}": {"get": {}} for i in range(operations)},
    }


def _state(specs_dir: Path) -> dict:
    return json.loads(refresh.state_path(specs_dir).read_text(encoding="utf-8"))


def _serve(mock: respx.MockRouter, document: object = None) -> None:
    body = _doc() if document is None else document
    for url in (_US_URL, _EU_URL):
        mock.get(url).mock(return_value=httpx.Response(200, json=body))


def test_only_cached_specs_are_swept(specs_dir: Path) -> None:
    # A spec reaches the cache by being used; the sweep keeps it current rather
    # than pre-fetching every deployment in the config.
    assert refresh.cached_keys(specs_dir) == [
        ("demo", "eu", "api"),
        ("demo", "us", "api"),
    ]

    refresh.state_path(specs_dir).write_text("{}")
    assert refresh.state_path(specs_dir).name not in {
        k[2] for k in refresh.cached_keys(specs_dir)
    }


async def test_a_sweep_records_every_spec_it_attempted(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    with respx.mock() as mock:
        _serve(mock)
        rows = await refresh.refresh_due(config, registry, specs_dir, now=1000.0)

    assert {r["spec"] for r in rows} == {"demo/us/api", "demo/eu/api"}
    recorded = _state(specs_dir)["specs"]
    assert set(recorded) == {"demo/us/api", "demo/eu/api"}
    assert all(row["attempted"] == 1000.0 for row in recorded.values())
    assert _state(specs_dir)["lease_expires"] == 0.0


async def test_a_spec_refreshed_inside_the_interval_is_skipped(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    with respx.mock() as mock:
        _serve(mock)
        assert await refresh.refresh_due(
            config, registry, specs_dir, interval=100.0, now=1000.0
        )
        assert (
            await refresh.refresh_due(
                config, registry, specs_dir, interval=100.0, now=1050.0
            )
            == []
        )
        assert await refresh.refresh_due(
            config, registry, specs_dir, interval=100.0, now=1200.0
        )


async def test_force_ignores_the_interval(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    with respx.mock() as mock:
        _serve(mock)
        await refresh.refresh_due(
            config, registry, specs_dir, interval=100.0, now=1000.0
        )
        rows = await refresh.refresh_due(
            config, registry, specs_dir, interval=100.0, force=True, now=1001.0
        )
    assert len(rows) == 2


async def test_a_live_lease_holds_off_a_second_sweeper(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    # Stand in for another process mid-sweep: a lease that has not yet expired.
    refresh.state_path(specs_dir).write_text(json.dumps({"lease_expires": 2000.0}))
    with respx.mock() as mock:
        _serve(mock)
        assert await refresh.refresh_due(config, registry, specs_dir, now=1999.0) == []
        assert await refresh.refresh_due(config, registry, specs_dir, now=2001.0) != []


async def test_a_failed_spec_is_not_retried_until_the_next_interval(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    with respx.mock() as mock:
        for url in (_US_URL, _EU_URL):
            mock.get(url).mock(side_effect=httpx.ConnectError("deployment down"))
        rows = await refresh.refresh_due(
            config, registry, specs_dir, interval=100.0, now=1000.0
        )
        assert {r["status"] for r in rows} == {"error"}
        assert _state(specs_dir)["specs"]["demo/us/api"]["attempted"] == 1000.0

        # A deployment that is down is retried once per interval, not every tick.
        assert (
            await refresh.refresh_due(
                config, registry, specs_dir, interval=100.0, now=1050.0
            )
            == []
        )


async def test_a_rejected_document_leaves_the_cached_spec_alone(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    cached = cache.spec_cache_path(specs_dir, "demo", "us", "api")
    before = cached.read_bytes()
    with respx.mock() as mock:
        _serve(mock, {"message": "Not Found"})
        rows = await refresh.refresh_due(config, registry, specs_dir, now=1000.0)

    assert all(r["status"] == "error" for r in rows)
    assert "no operations" in _state(specs_dir)["specs"]["demo/us/api"]["error"]
    assert cached.read_bytes() == before


async def test_an_unchanged_document_is_not_rewritten(
    config: Config, registry: SpecRegistry, specs_dir: Path, mini_spec: dict
) -> None:
    cached = cache.spec_cache_path(specs_dir, "demo", "us", "api")
    cache.write_spec(cached, mini_spec)
    mtime = cached.stat().st_mtime

    with respx.mock() as mock:
        _serve(mock, mini_spec)
        rows = await refresh.refresh_due(config, registry, specs_dir, now=1000.0)

    statuses = {r["spec"]: r["status"] for r in rows}
    assert statuses["demo/us/api"] == "unchanged"
    assert cached.stat().st_mtime == mtime


async def test_an_unreadable_state_file_is_treated_as_empty(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    # A crash can truncate the state mid-write; it is derived data, so a damaged
    # read costs one extra sweep rather than raising.
    refresh.state_path(specs_dir).write_bytes(b'{"lease_expires": 20')
    with respx.mock() as mock:
        _serve(mock)
        assert (
            len(await refresh.refresh_due(config, registry, specs_dir, now=1000.0)) == 2
        )


async def test_cancelling_a_sweep_releases_the_lease(
    config: Config, registry: SpecRegistry, specs_dir: Path
) -> None:
    calls = 0

    async def cancel_on_second(spec_url: str, **_: object) -> dict:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError
        return _doc()

    original = cache.fetch_spec
    cache.fetch_spec = cancel_on_second  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await refresh.refresh_due(config, registry, specs_dir, now=1000.0)
    finally:
        cache.fetch_spec = original  # type: ignore[assignment]

    # Without the release, the next process waits out the whole TTL.
    assert _state(specs_dir)["lease_expires"] == 0.0


async def test_a_refreshed_spec_reaches_a_running_registry(
    config: Config, specs_dir: Path
) -> None:
    """The regression the registry invalidation exists to prevent.

    Nothing re-reads a spec once it is loaded, so without invalidation a running
    server would keep serving the old document until it restarted.
    """
    reg = build_registry(config, specs_dir)
    before = {op.operation_id for op in await reg.list_operations("demo", "us", "api")}

    with respx.mock() as mock:
        _serve(mock, _doc(operations=3))
        rows = await refresh.refresh_due(config, reg, specs_dir, now=1000.0)

    assert {r["status"] for r in rows} == {"written"}
    after = {op.operation_id for op in await reg.list_operations("demo", "us", "api")}
    assert after == {"GET /op0", "GET /op1", "GET /op2"}
    assert after != before


async def test_the_loop_survives_a_failing_sweep(
    config: Config,
    registry: SpecRegistry,
    specs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def exploding(*_: object, **__: object) -> list:
        nonlocal calls
        calls += 1
        raise RuntimeError("network gone")

    monkeypatch.setattr(refresh, "refresh_due", exploding)
    task = asyncio.create_task(refresh.refresh_loop(config, registry, specs_dir, 0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls > 1  # a failed sweep must not end the loop
