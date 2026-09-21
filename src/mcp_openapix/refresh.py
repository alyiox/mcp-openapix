"""Background refresh of the cached swagger snapshots.

The trigger lives outside the session. Staleness is invisible from inside one --
a stale spec simply lacks an endpoint, or names it differently -- so an agent
asked to judge when a refresh is worthwhile either never fires or fires
superstitiously after an unrelated failure. A schedule, or the ``--refresh``
flag, decides instead.

One file at the cache root, ``spec-state.json``, is both the lock and the
record. ``lock_descriptor`` locks a descriptor we opened ourselves and, unlike
``FileLock``, never opens, truncates or unlinks the path -- ``FileLock``'s winner
truncates the file it locks, which would erase the state on every acquire.

The state is rewritten in place rather than replaced. ``os.replace`` would swap
the inode out from under a held lock, splitting waiters across two inodes and
breaking the mutual exclusion the lock exists for. A crash mid-write can
therefore truncate it -- acceptable only because this file is derived data: it
reads back as empty and costs one extra sweep, while the specs themselves keep
their atomic install.

The sweep covers what is already cached, not every deployment in the config. A
spec reaches the cache by being used; keeping it current is this module's job,
populating it is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from filelock import lock_descriptor, unlock_descriptor

from . import cache
from .config import Config, ConfigError, spec_source_url
from .spec_loader import SpecKey, SpecRegistry

logger = logging.getLogger(__name__)

# How long a sweeper's claim stays valid. It must outlast one document's fetch,
# and it is renewed after each one, so a long sweep never expires its own lease.
# A process that dies mid-sweep blocks the next one for at most this long.
LEASE_TTL = 300.0

# Seconds between sweeps when the config does not say. Swagger documents move on
# a deployment cadence, not an hourly one.
DEFAULT_REFRESH_INTERVAL = 7 * 24 * 60 * 60

STATE_NAME = "spec-state.json"

_sweep_lock = asyncio.Lock()


def state_path(specs_dir: Path) -> Path:
    """The lease and per-spec refresh record, at the cache root."""
    return specs_dir / STATE_NAME


def cached_keys(specs_dir: Path) -> list[SpecKey]:
    """Every ``(platform, region, service)`` with a spec already on disk.

    The layout is ``<root>/<platform>/<region>/<service>.json``, so the state
    file at the root is excluded by the shape of the glob rather than by name.
    """
    return sorted(
        (path.parent.parent.name, path.parent.name, path.stem)
        for path in specs_dir.glob("*/*/*.json")
    )


def _read_state(fd: int) -> dict[str, Any]:
    """Parse the open state file, treating anything unusable as empty."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    try:
        state = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(fd: int, state: dict[str, Any]) -> None:
    body = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, body)
    os.fsync(fd)


@contextmanager
def _locked_state(specs_dir: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Hold the state file locked, yielding its descriptor and contents.

    Blocking, and deliberately short: the caller reads, decides and writes, with
    no network call inside. Where the filesystem cannot lock at all the sweep
    still runs -- the cost is a duplicated download and a last-writer-wins
    install of identical bytes, which is what the write path absorbs by design.
    """
    path = state_path(specs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        try:
            lock_descriptor(fd)
            locked = True
        except OSError as e:
            logger.debug("refresh state lock unavailable (%s); continuing unlocked", e)
        yield fd, _read_state(fd)
    finally:
        if locked:
            with suppress(OSError):
                unlock_descriptor(fd)
        os.close(fd)


def _key_str(key: SpecKey) -> str:
    return "/".join(key)


def _attempted(state: dict[str, Any], key: SpecKey) -> float | None:
    """When this spec was last tried, or ``None`` if it never has been."""
    rows = state.get("specs")
    row = rows.get(_key_str(key)) if isinstance(rows, dict) else None
    if isinstance(row, dict) and isinstance(row.get("attempted"), int | float):
        return float(row["attempted"])
    return None


def _claim(
    specs_dir: Path, interval: float, *, force: bool, now: float
) -> list[SpecKey]:
    """Take the lease and return what to sweep, or nothing if another holds it.

    Never-attempted is due outright rather than by arithmetic on a zero
    timestamp: the two are different states, and conflating them would make the
    sweep depend on how far the clock happens to sit from the epoch.
    """
    with _locked_state(specs_dir) as (fd, state):
        if float(state.get("lease_expires") or 0.0) > now:
            return []
        due = []
        for key in cached_keys(specs_dir):
            last = _attempted(state, key)
            if force or last is None or now - last >= interval:
                due.append(key)
        if not due:
            return []
        state["lease_expires"] = now + LEASE_TTL
        _write_state(fd, state)
        return due


def _record(specs_dir: Path, row: dict[str, Any], *, now: float, renew: bool) -> None:
    """Write one spec's outcome and renew (or drop) the lease."""
    with _locked_state(specs_dir) as (fd, state):
        rows = state.get("specs")
        rows = rows if isinstance(rows, dict) else {}
        entry: dict[str, Any] = {"attempted": now, "status": row["status"]}
        for field in ("operations", "error"):
            if row.get(field) is not None:
                entry[field] = row[field]
        rows[row["spec"]] = entry
        state["specs"] = rows
        state["lease_expires"] = (now + LEASE_TTL) if renew else 0.0
        _write_state(fd, state)


def _release(specs_dir: Path) -> None:
    with _locked_state(specs_dir) as (fd, state):
        state["lease_expires"] = 0.0
        _write_state(fd, state)


async def refresh_one(
    config: Config,
    registry: SpecRegistry,
    specs_dir: Path,
    key: SpecKey,
) -> dict[str, Any]:
    """Re-fetch, validate and install one spec, reporting the outcome as a row.

    A row carries its detail in its own field rather than folded into ``status``,
    so outcomes can be counted without parsing prose.
    """
    platform, region, service = key
    spec_id = _key_str(key)
    try:
        spec_url = spec_source_url(config, platform, region, service)
    except ConfigError as e:
        return {"spec": spec_id, "status": "error", "error": str(e)}
    try:
        spec = await cache.fetch_spec(spec_url)
        target = cache.spec_cache_path(specs_dir, platform, region, service)
        written = await asyncio.to_thread(cache.write_spec, target, spec)
    except (
        httpx.HTTPError,
        json.JSONDecodeError,
        OSError,
        TimeoutError,
        ValueError,
        cache.SpecRejected,
    ) as e:
        return {"spec": spec_id, "status": "error", "url": spec_url, "error": str(e)}
    if written:
        # Nothing here re-reads a spec once loaded, so without this the running
        # server would serve the old document until it restarts.
        registry.invalidate(key)
    return {
        "spec": spec_id,
        "status": "written" if written else "unchanged",
        "url": spec_url,
        "operations": cache.count_operations(spec),
    }


async def refresh_due(
    config: Config,
    registry: SpecRegistry,
    specs_dir: Path,
    *,
    interval: float = DEFAULT_REFRESH_INTERVAL,
    force: bool = False,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Sweep the cached specs whose last attempt is older than ``interval``.

    Returns one row per spec actually swept, and an empty list when nothing is
    due or another process holds the lease.

    The lease is what keeps one sweeper at a time across processes -- the server
    runs one per client session, so without it every session would re-download
    the same documents. It is taken and renewed under the lock, and every fetch
    happens outside it: a caller must never wait on someone else's download.

    An attempt is recorded whether it succeeded or failed, so a deployment that
    is down is retried at the next interval rather than on every tick.
    """
    started = time.time() if now is None else now
    async with _sweep_lock:
        due = await asyncio.to_thread(
            _claim, specs_dir, interval, force=force, now=started
        )
        if not due:
            return []
        rows: list[dict[str, Any]] = []
        try:
            for index, key in enumerate(due):
                row = await refresh_one(config, registry, specs_dir, key)
                rows.append(row)
                await asyncio.to_thread(
                    _record,
                    specs_dir,
                    row,
                    now=time.time() if now is None else now,
                    renew=index < len(due) - 1,
                )
        except asyncio.CancelledError:
            # Drop the lease rather than make the next process wait out its TTL.
            with suppress(OSError):
                await asyncio.to_thread(_release, specs_dir)
            raise
        return rows


async def refresh_loop(
    config: Config,
    registry: SpecRegistry,
    specs_dir: Path,
    interval: float = DEFAULT_REFRESH_INTERVAL,
) -> None:
    """Sweep at startup, then every ``interval`` seconds, until cancelled.

    Never lets a refresh failure reach the server: a background task that dies on
    a bad network is worse than one that logs and waits for the next tick.
    """
    while True:
        try:
            rows = await refresh_due(config, registry, specs_dir, interval=interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a sweep must never take the server down
            logger.exception("spec refresh sweep failed")
        else:
            if rows:
                errors = [r for r in rows if r["status"] == "error"]
                logger.info(
                    "refreshed %d spec(s): %d written, %d unchanged, %d error",
                    len(rows),
                    sum(1 for r in rows if r["status"] == "written"),
                    sum(1 for r in rows if r["status"] == "unchanged"),
                    len(errors),
                )
                for row in errors:
                    logger.warning("spec %s: %s", row["spec"], row["error"])
        await asyncio.sleep(interval)
