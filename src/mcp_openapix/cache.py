"""Local OpenAPI spec cache: path layout + concurrency-safe reads/writes.

Specs live under ``<root>/<platform>/<region>/<service>.json``. Both the MCP
server (one process per client session) and the background refresh write here,
so the rules that keep the cache trustworthy are the ones in this module:

* a fetched document is installed only once it yields an operation, so an
  upstream answering ``200`` with an error body or a login page cannot replace a
  working spec with one that serves nothing;
* a byte-identical document is not rewritten, which keeps mtimes stable and
  makes "nothing changed upstream" a fact a caller can act on;
* writes are atomic and stage through a scratch file **unique per writer** — a
  name derived only from the target is shared by every process writing that
  spec, and two interleaved writers produce a file that is complete, corrupt,
  and installed atomically, which is worse than a partial one because nothing
  downstream can detect it.

There is deliberately no lock around the fetch. Correctness rests on the two
rules above, not on mutual exclusion: two processes installing the same
validated document write the same bytes. What a lock would buy is avoided
duplicate downloads, and :mod:`mcp_openapix.refresh` buys that with a
lease instead — without ever making a caller wait on someone else's download.

The in-process ``asyncio`` serialization that prevents redundant fetches within
a single event loop lives in ``SpecRegistry`` (``spec_loader``).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

# Path-item keys that denote an operation; everything else under a path
# ("parameters", "summary", vendor extensions) is not one.
HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

# One document's fetch, end to end. An httpx timeout is per socket operation, so
# a slow-drip response never trips it; this is the bound that actually holds.
FETCH_DEADLINE = 60.0


class SpecRejected(Exception):
    """Raised when a fetched document is not usable as a spec."""


def spec_cache_path(root: Path, platform: str, region: str, service: str) -> Path:
    """Return the cache file path for one ``(platform, region, service)``."""
    return root / platform / region / f"{service}.json"


def count_operations(spec: Any) -> int:
    """Count HTTP operations in a document, tolerating any shape.

    Tolerant because the input is an untrusted HTTP response that is not yet
    known to be a spec at all.
    """
    if not isinstance(spec, dict):
        return 0
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return 0
    return sum(
        1
        for methods in paths.values()
        if isinstance(methods, dict)
        for method, op in methods.items()
        if method.lower() in HTTP_METHODS and isinstance(op, dict)
    )


def validate_spec(spec: Any) -> dict[str, Any]:
    """Return the document if it declares at least one operation, else raise.

    The trust boundary, and the only check worth making, because it tests the
    property that actually matters: whether the server still works after the
    write. Status codes and content types do not substitute — an unauthenticated
    swagger endpoint behind a gateway answers ``200`` with something useless more
    often than it answers ``404``.
    """
    if count_operations(spec) == 0:
        raise SpecRejected("fetched document declares no operations")
    return spec


def write_spec(target: Path, spec: dict[str, Any]) -> bool:
    """Atomically install a spec, returning whether anything changed on disk."""
    body = json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")
    if target.is_file() and target.read_bytes() == body:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=".tmp"
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)  # leave litter, never a trap
        raise
    return True


async def fetch_spec(
    spec_url: str,
    *,
    timeout: float = 30.0,
    deadline: float = FETCH_DEADLINE,
) -> dict[str, Any]:
    """Download and validate one swagger document.

    ``timeout`` bounds each socket operation and ``deadline`` bounds the whole
    fetch. Both are needed: a response delivered one slow byte at a time resets
    the per-operation clock forever and would otherwise hang a caller with no
    bound at all. The swagger fetch is a plain unauthenticated ``GET``.
    """
    async with asyncio.timeout(deadline):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(spec_url)
            response.raise_for_status()
            return validate_spec(response.json())


async def fetch_and_cache(
    target: Path,
    spec_url: str,
    *,
    force: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Return the spec at ``target``, downloading it from ``spec_url`` if needed.

    ``force`` re-downloads even when the file exists; otherwise an existing file
    is reused and never re-fetched.
    """
    if not force and target.is_file():
        return json.loads(target.read_text(encoding="utf-8"))
    spec = await fetch_spec(spec_url, timeout=timeout)
    await asyncio.to_thread(write_spec, target, spec)
    return spec
