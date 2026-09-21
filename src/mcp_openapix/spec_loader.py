"""OpenAPI spec loading + operationId indexing.

Many swagger documents omit ``operationId`` on most operations, so we
synthesize a stable identifier of the form ``"METHOD path"``
(e.g. ``"POST /api/sponsor/report/adGroup/list"``). When an operation
*does* declare an ``operationId``, that value wins so spec authors can
still customise it.

Specs are no longer bundled in the repo. Each ``(platform, region, service)``
has its own swagger file, loaded lazily on first tool use: a cache miss
downloads the spec from the deployment (unauthenticated) and writes it to the
local cache. Concurrent first-uses of the same spec are serialized by a per-key
``asyncio.Lock`` (so it is fetched once per process); cross-process safety and
atomic writes live in :mod:`mcp_openapix.cache`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import cache
from .config import Config, spec_source_url

logger = logging.getLogger(__name__)

HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

REF_RE = re.compile(r'"\$ref"\s*:\s*"([^"]+)"')
SCHEMA_REF_PREFIX = "#/components/schemas/"

# (platform, region, service) — identifies one cached spec.
SpecKey = tuple[str, str, str]


class SpecError(Exception):
    """Raised when a spec file is missing, unfetchable, or malformed."""


@dataclass
class Operation:
    operation_id: str
    platform: str
    region: str
    service: str
    method: str
    path: str
    raw: dict[str, Any]


@dataclass
class PlatformEntry:
    name: str
    regions: list[str]


@dataclass
class SpecRegistry:
    specs_dir: Path
    config: Config
    platforms: dict[str, PlatformEntry]
    _loaded: dict[SpecKey, dict[str, Any]] = field(default_factory=dict)
    _index: dict[tuple[str, str, str, str], Operation] = field(default_factory=dict)
    _indexed: set[SpecKey] = field(default_factory=set)
    _locks: dict[SpecKey, asyncio.Lock] = field(default_factory=dict)

    def platform_names(self) -> list[str]:
        return sorted(self.platforms)

    def get_platform(self, name: str) -> PlatformEntry:
        if name not in self.platforms:
            raise SpecError(
                f"platform {name!r} not in manifest (known: {sorted(self.platforms)})"
            )
        return self.platforms[name]

    def _lock_for(self, key: SpecKey) -> asyncio.Lock:
        # Created without awaiting, so get-or-create is race-free on one loop.
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get_spec(
        self, platform: str, region: str, service: str
    ) -> dict[str, Any]:
        """Return the spec for ``(platform, region, service)``.

        Reads the in-memory cache, then the on-disk cache, then downloads the
        swagger file from the deployment on a miss. Concurrent callers for the
        same key wait on a per-key lock so the fetch happens once.
        """
        key = (platform, region, service)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        self.get_platform(platform)
        async with self._lock_for(key):
            cached = self._loaded.get(key)
            if cached is not None:
                return cached
            target = cache.spec_cache_path(self.specs_dir, platform, region, service)
            spec_url = spec_source_url(self.config, platform, region, service)
            try:
                spec = await cache.fetch_and_cache(target, spec_url)
            except httpx.HTTPError as e:
                raise SpecError(
                    f"failed to fetch spec for {platform}/{region}/{service} "
                    f"from {spec_url}: {e}"
                ) from e
            except json.JSONDecodeError as e:
                raise SpecError(
                    f"spec for {platform}/{region}/{service} is not valid JSON: {e}"
                ) from e
            except cache.SpecRejected as e:
                raise SpecError(
                    f"spec for {platform}/{region}/{service} from {spec_url}: {e}"
                ) from e
            self._loaded[key] = spec
            self._index_spec(key, spec)
            return spec

    def invalidate(self, key: SpecKey) -> None:
        """Forget one spec so the next read reloads it from disk.

        Nothing here re-reads a spec once it is loaded, so a refresh that
        rewrites the file would otherwise stay invisible to this process until
        it restarts -- which is every session, since the server runs one process
        per client. The background refresh calls this after each install.
        """
        self._loaded.pop(key, None)
        self._indexed.discard(key)
        for index_key in [k for k in self._index if k[:3] == key]:
            del self._index[index_key]

    def _index_spec(self, key: SpecKey, spec: dict[str, Any]) -> None:
        if key in self._indexed:
            return
        platform, region, service = key
        paths = spec.get("paths") or {}
        if not isinstance(paths, dict):
            logger.warning("spec for %s/%s/%s has no 'paths' object", *key)
            self._indexed.add(key)
            return
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in HTTP_METHODS:
                    continue
                if not isinstance(op, dict):
                    continue
                declared = op.get("operationId")
                if isinstance(declared, str) and declared:
                    op_id = declared
                else:
                    op_id = synthesize_operation_id(method, path)
                self._index[(platform, region, service, op_id)] = Operation(
                    operation_id=op_id,
                    platform=platform,
                    region=region,
                    service=service,
                    method=method.lower(),
                    path=path,
                    raw=op,
                )
        self._indexed.add(key)

    async def resolve_refs(
        self, platform: str, region: str, service: str, operation: Operation
    ) -> dict[str, Any]:
        """Return ``components.schemas`` reachable from this operation.

        Walks ``$ref`` strings starting from the operation, then follows
        every ``$ref`` inside each referenced schema until no new schemas
        are pulled in. Only refs of the form ``#/components/schemas/<name>``
        are followed; external refs are ignored.
        """
        spec = await self.get_spec(platform, region, service)
        components = (spec.get("components") or {}).get("schemas") or {}
        if not isinstance(components, dict):
            return {}
        seen: dict[str, Any] = {}
        queue = _collect_refs(operation.raw)
        while queue:
            ref = queue.pop()
            if not ref.startswith(SCHEMA_REF_PREFIX):
                continue
            name = ref[len(SCHEMA_REF_PREFIX) :]
            if name in seen:
                continue
            schema = components.get(name)
            if schema is None:
                continue
            seen[name] = schema
            queue.extend(_collect_refs(schema))
        return seen

    async def get_operation(
        self, platform: str, region: str, service: str, operation_id: str
    ) -> Operation:
        await self.get_spec(platform, region, service)
        key = (platform, region, service, operation_id)
        if key not in self._index:
            raise SpecError(
                f"operation {operation_id!r} not found in {platform}/{region}/{service}"
            )
        return self._index[key]

    async def list_operations(
        self, platform: str, region: str, service: str
    ) -> list[Operation]:
        await self.get_spec(platform, region, service)
        prefix = (platform, region, service)
        return [op for k, op in self._index.items() if k[:3] == prefix]


def synthesize_operation_id(method: str, path: str) -> str:
    """Build a stable identifier for an operation lacking an ``operationId``.

    Format: ``"<METHOD> <path>"`` — readable and unique within a platform.
    """
    return f"{method.upper()} {path}"


def _collect_refs(value: Any) -> list[str]:
    return REF_RE.findall(json.dumps(value))


def build_registry(config: Config, specs_dir: Path) -> SpecRegistry:
    """Build a SpecRegistry from config.platforms (specs themselves load lazily)."""
    platforms = {
        name: PlatformEntry(
            name=name,
            regions=list(p.regions.keys()),
        )
        for name, p in config.platforms.items()
    }
    return SpecRegistry(specs_dir=specs_dir, config=config, platforms=platforms)
