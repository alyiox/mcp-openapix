"""describe_endpoint — full schema for one OpenAPI operation."""

from __future__ import annotations

from typing import Any

from ..config import Config, resolve
from ..spec_loader import SpecRegistry


async def describe_endpoint(
    *,
    config: Config,
    registry: SpecRegistry,
    operation_id: str,
    platform: str | None = None,
    region: str | None = None,
    service: str | None = None,
) -> dict[str, Any]:
    """Return the operation + its transitive ``components.schemas`` closure.

    Resolves ``(platform, region, service)`` (falling back to defaults), loading
    the spec on first use. The closure is computed by following every ``$ref``
    (transitively) inside the operation, so the agent has enough context to build
    request bodies / interpret responses without needing the full spec.
    """
    platform_name = resolve("platform", platform, config.defaults.platform)
    region_name = resolve("region", region, config.defaults.region)
    service_name = resolve("service", service, config.defaults.service)
    op = await registry.get_operation(
        platform_name, region_name, service_name, operation_id
    )
    schemas = await registry.resolve_refs(platform_name, region_name, service_name, op)
    return {
        "platform": platform_name,
        "region": region_name,
        "service": service_name,
        "operation_id": op.operation_id,
        "method": op.method.upper(),
        "path": op.path,
        "operation": op.raw,
        "components": {"schemas": schemas},
    }
