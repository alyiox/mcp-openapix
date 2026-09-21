"""list_endpoints — search/filter OpenAPI operations within a service."""

from __future__ import annotations

from typing import Any

from ..config import Config, resolve
from ..spec_loader import SpecRegistry


async def list_endpoints(
    *,
    config: Config,
    registry: SpecRegistry,
    platform: str | None = None,
    region: str | None = None,
    service: str | None = None,
    query: str | None = None,
    tag: str | None = None,
    method: str | None = None,
) -> list[dict[str, Any]]:
    """Return slim records for matching operations in one service's spec.

    Resolves ``(platform, region, service)`` (falling back to defaults), loading
    the spec on first use. ``query`` matches substring (case-insensitive) against
    ``operationId``, ``path``, and ``summary``. ``tag`` filters operations whose
    ``tags`` list contains the value. ``method`` filters by HTTP verb.
    """
    platform_name = resolve("platform", platform, config.defaults.platform)
    region_name = resolve("region", region, config.defaults.region)
    service_name = resolve("service", service, config.defaults.service)
    operations = await registry.list_operations(
        platform_name, region_name, service_name
    )

    q = query.lower() if query else None
    m = method.lower() if method else None

    out: list[dict[str, Any]] = []
    for op in operations:
        summary = op.raw.get("summary") or ""
        tags = op.raw.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        if q and not (
            q in op.operation_id.lower()
            or q in op.path.lower()
            or q in str(summary).lower()
        ):
            continue
        if tag and tag not in tags:
            continue
        if m and m != op.method:
            continue
        out.append(
            {
                "operation_id": op.operation_id,
                "platform": op.platform,
                "region": op.region,
                "service": op.service,
                "method": op.method.upper(),
                "path": op.path,
                "summary": summary,
                "tags": list(tags),
            }
        )
    return out
