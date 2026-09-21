"""list_platforms — discover platforms + their served regions + configured services."""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..spec_loader import SpecRegistry


def list_platforms(
    *,
    config: Config,
    registry: SpecRegistry,
    region: str | None = None,
) -> list[dict[str, Any]]:
    """Return platforms with their served regions, services, and configured envs.

    Each region's ``services`` maps service name to
    ``{"envs": [...], "desc": "..."}``. ``desc`` is omitted when unset.
    An empty services dict means the platform is known in that region but no
    service is configured for it in ``config.json``.
    """
    out: list[dict[str, Any]] = []
    for name in registry.platform_names():
        platform = registry.get_platform(name)
        served_regions = platform.regions
        if region is not None and region not in served_regions:
            continue
        regions_payload: list[dict[str, Any]] = []
        for r in served_regions:
            platform_cfg = config.platforms.get(name)
            services_payload: dict[str, dict[str, Any]] = {}
            if platform_cfg is not None and r in platform_cfg.regions:
                for svc_name, svc_cfg in platform_cfg.regions[r].services.items():
                    entry: dict[str, Any] = {"envs": sorted(svc_cfg.envs)}
                    if svc_cfg.desc:
                        entry["desc"] = svc_cfg.desc
                    services_payload[svc_name] = entry
            regions_payload.append({"name": r, "services": services_payload})
        out.append({"name": name, "regions": regions_payload})
    return out
