"""Configuration loading and validation for the OpenAPI MCP server.

Schema: platform → region → services → service → env. A separate top-level
``token_helpers`` section holds named token helpers; any level of the hierarchy
MAY name one, and the most specific wins. The per-service base URL lives in the
env.

See ``docs/token-protocol.md`` for the helper contract.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)

#: Default seconds a token helper may run before its process group is killed.
HELPER_TIMEOUT_SECONDS = 60.0

#: Ceiling on that timeout. A helper has no interactive channel to the user, so
#: a longer wait is a hang, not a login in progress.
HELPER_TIMEOUT_MAX_SECONDS = 300.0


class TokenHelperConfig(BaseModel):
    """One named token helper, in the shape of an MCP server entry.

    The server runs ``[command, *args]`` verbatim -- there is no substitution,
    so what the config says is what runs -- and reads a token from stdout.
    Config names a command and nothing else: sourcing credentials is the
    helper's own business.
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    timeout: Annotated[
        StrictInt | StrictFloat, Field(gt=0, le=HELPER_TIMEOUT_MAX_SECONDS)
    ] = HELPER_TIMEOUT_SECONDS


class ServiceEnvConfig(BaseModel):
    """One deployment env — carries the full service base URL."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    token_helper: str | None = None


class ServiceConfig(BaseModel):
    """One service within a platform+region (e.g. ``items``)."""

    model_config = ConfigDict(extra="forbid")

    desc: str | None = None
    spec_path: str = Field(min_length=1)
    canonical_env: str | None = None
    token_helper: str | None = None
    envs: dict[str, ServiceEnvConfig] = Field(default_factory=dict)


class ServicesConfig(BaseModel):
    """Service collection with an optional shared token-helper default.

    Service names are dynamic keys alongside the reserved ``token_helper``
    field, so the model validates those extra values explicitly.
    """

    model_config = ConfigDict(extra="allow")

    token_helper: str | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_services(cls, obj: Any) -> Any:
        if isinstance(obj, dict):
            obj = dict(obj)
            for name, value in obj.items():
                if name != "token_helper":
                    obj[name] = ServiceConfig.model_validate(value)
        return obj

    def __getitem__(self, name: str) -> ServiceConfig:
        return self.__pydantic_extra__[name]

    def __contains__(self, name: object) -> bool:
        return name in (self.__pydantic_extra__ or {})

    def __iter__(self):
        return iter(self.__pydantic_extra__ or {})

    def __len__(self) -> int:
        return len(self.__pydantic_extra__ or {})

    def items(self):
        return (self.__pydantic_extra__ or {}).items()


class PlatformRegionConfig(BaseModel):
    """Services offered by one platform in one region."""

    model_config = ConfigDict(extra="forbid")

    token_helper: str | None = None
    services: ServicesConfig = Field(default_factory=ServicesConfig)


class PlatformConfig(BaseModel):
    """Top-level platform entry — a mapping of region name → region config."""

    model_config = ConfigDict(extra="forbid")

    token_helper: str | None = None
    regions: dict[str, PlatformRegionConfig] = Field(default_factory=dict)


class Defaults(BaseModel):
    """Optional defaults applied when tool args are omitted."""

    model_config = ConfigDict(extra="forbid")

    region: str | None = None
    env: str | None = None
    platform: str | None = None
    service: str | None = None
    username: str | None = None
    token_helper: str | None = None


class SpecRefresh(BaseModel):
    """Whether cached specs refresh on their own, and how often.

    Two fields rather than one, because "off" and "how often" are different
    questions: a single interval with ``0`` meaning disabled would lose the
    cadence the moment someone turned it off, and would make a typo of ``0``
    indistinguishable from a decision. ``auto=False`` stops the background sweep
    and leaves ``mcp-openapi --refresh`` working.

    ``interval`` is in days -- nobody reads ``604800`` as a week -- and
    fractional, so a sub-day cadence stays expressible (``0.5`` is twelve hours).
    """

    model_config = ConfigDict(extra="forbid")

    # Strict: config is hand-written JSON where true/false and numbers are native
    # types, so a quoted "yes" or "7" is a typo worth naming rather than coercing.
    auto: StrictBool = True
    interval: Annotated[StrictInt | StrictFloat, Field(gt=0)] = 7

    @property
    def interval_seconds(self) -> float:
        return self.interval * 86400.0


class Config(BaseModel):
    """Top-level config.json schema."""

    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    platforms: dict[str, PlatformConfig] = Field(default_factory=dict)
    token_helpers: dict[str, TokenHelperConfig] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    response_cache_ttl: int = Field(default=3600, ge=0)
    truncate_threshold: int = Field(default=1024, gt=0)
    spec_refresh: SpecRefresh = Field(default_factory=SpecRefresh)


@dataclass(frozen=True)
class ResolvedDeployment:
    """Flattened (platform, region, service, env) view used by the HTTP layer.

    ``api_base`` is the full service URL (e.g. ``https://api.example.com/items``).
    ``token_helper`` is ``None`` when the deployment needs no token. ``headers`` are
    sent verbatim on every call to this deployment.
    """

    api_base: str
    token_helper_name: str | None
    token_helper: TokenHelperConfig | None
    headers: dict[str, str]


class ConfigError(Exception):
    """Raised when config.json is missing, malformed, or internally inconsistent."""


def default_config_path() -> Path:
    """Return the user-scoped config.json path for this OS."""
    if sys.platform == "win32":
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        base = Path.home()
    return base / ".config" / "mcp-openapi" / "config.json"


def default_cache_dir() -> Path:
    """Return the user-scoped spec cache root for this OS.

    Rooted at the user's home directory (``$USERPROFILE`` on Windows, ``$HOME``
    otherwise). The server caches fetched OpenAPI specs under
    ``<root>/.cache/mcp-openapi/<platform>/<region>/<service>.json``.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        base = Path.home()
    return base / ".cache" / "mcp-openapi"


def load_config(path: Path | None = None) -> Config:
    """Load and validate the config.json file.

    Args:
        path: explicit config path; falls back to ``default_config_path()``.

    Raises:
        ConfigError: if the file is missing, invalid JSON, fails schema
            validation, or has cross-field inconsistencies.
    """
    cfg_path = path or default_config_path()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file is not valid JSON: {cfg_path}: {e}") from e

    try:
        config = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"config file failed validation: {cfg_path}: {e}") from e

    _validate_defaults(config)
    _validate_token_helpers(config)
    return config


def _validate_token_helpers(config: Config) -> None:
    """Check that every configured deployment binds to a real helper."""
    if (
        config.defaults.token_helper is not None
        and config.defaults.token_helper not in config.token_helpers
    ):
        raise ConfigError(
            f"defaults.token_helper={config.defaults.token_helper!r} is not in "
            f"token_helpers (configured: {sorted(config.token_helpers)})"
        )

    for platform, platform_cfg in config.platforms.items():
        for region, region_cfg in platform_cfg.regions.items():
            for service, svc_cfg in region_cfg.services.items():
                for env in svc_cfg.envs:
                    resolve_token_helper_name(config, platform, region, service, env)


def _validate_defaults(config: Config) -> None:
    """Check that defaults reference configured entries (in dependency order)."""
    platform = config.defaults.platform
    region = config.defaults.region
    service = config.defaults.service
    env = config.defaults.env

    if platform is not None and platform not in config.platforms:
        raise ConfigError(
            f"defaults.platform={platform!r} is not in platforms "
            f"(configured: {sorted(config.platforms)})"
        )

    if platform is not None and region is not None:
        if region not in config.platforms[platform].regions:
            raise ConfigError(
                f"defaults.region={region!r} is not configured under "
                f"platform {platform!r} "
                f"(configured: {sorted(config.platforms[platform].regions)})"
            )

    if platform is not None and region is not None and service is not None:
        region_cfg = config.platforms[platform].regions[region]
        if service not in region_cfg.services:
            raise ConfigError(
                f"defaults.service={service!r} is not configured under "
                f"platform {platform!r} region {region!r} "
                f"(configured: {sorted(region_cfg.services)})"
            )

    if env is not None:
        if platform is None or region is None or service is None:
            raise ConfigError(
                "defaults.env requires defaults.platform, defaults.region, "
                "and defaults.service to also be set"
            )
        svc = config.platforms[platform].regions[region].services[service]
        if env not in svc.envs:
            raise ConfigError(
                f"defaults.env={env!r} is not configured under service "
                f"{service!r} (configured: {sorted(svc.envs)})"
            )


def get_deployment(
    config: Config, platform: str, region: str, service: str, env: str
) -> ResolvedDeployment:
    """Flatten a (platform, region, service, env) tuple into a ``ResolvedDeployment``.

    ``api_base`` is the full service URL; the token helper is resolved by
    :func:`resolve_token_helper_name`. Raises ``ConfigError`` with a structured message
    listing available options when any component of the tuple is not
    configured.
    """
    if platform not in config.platforms:
        raise ConfigError(
            f"platform {platform!r} not configured "
            f"(configured: {sorted(config.platforms)})"
        )
    platform_cfg = config.platforms[platform]
    if region not in platform_cfg.regions:
        raise ConfigError(
            f"region {region!r} not configured under platform {platform!r} "
            f"(configured: {sorted(platform_cfg.regions)})"
        )
    region_cfg = platform_cfg.regions[region]
    if service not in region_cfg.services:
        raise ConfigError(
            f"service {service!r} not configured under platform {platform!r} "
            f"region {region!r} (configured: {sorted(region_cfg.services)})"
        )
    svc_cfg = region_cfg.services[service]
    if env not in svc_cfg.envs:
        raise ConfigError(
            f"env {env!r} not configured under service {service!r} "
            f"(configured: {sorted(svc_cfg.envs)})"
        )
    env_cfg = svc_cfg.envs[env]
    token_helper_name = resolve_token_helper_name(
        config, platform, region, service, env
    )
    return ResolvedDeployment(
        api_base=str(env_cfg.url),
        token_helper_name=token_helper_name,
        token_helper=(
            config.token_helpers[token_helper_name]
            if token_helper_name is not None
            else None
        ),
        headers=dict(config.headers),
    )


def resolve_token_helper_name(
    config: Config, platform: str, region: str, service: str, env: str
) -> str | None:
    """Return the token helper bound to a deployment, or ``None`` for no auth.

    Walks outward from the deployment, most specific first::

        env → service → services → region → platform → defaults

    Any level MAY declare ``"token_helper": "<name>"``. Missing or null values
    do not bind a helper and allow resolution to continue outward.
    """
    svc_cfg = config.platforms[platform].regions[region].services[service]
    nodes: tuple[BaseModel, ...] = (
        svc_cfg.envs[env],
        svc_cfg,
        config.platforms[platform].regions[region].services,
        config.platforms[platform].regions[region],
        config.platforms[platform],
        config.defaults,
    )
    for node in nodes:
        name = node.token_helper
        if name is not None:
            if name not in config.token_helpers:
                raise ConfigError(
                    f"token helper {name!r} is not configured "
                    f"(configured: {sorted(config.token_helpers)})"
                )
            return name
    return None


def resolve_spec_env(svc_cfg: ServiceConfig, env: str | None) -> str:
    """Pick the env whose URL a spec is fetched from.

    Precedence: explicit ``env`` → ``canonical_env`` → the sole configured env.
    Raises ``ConfigError`` when ambiguous (multiple envs, none preferred).
    Spec content is env-agnostic, so any env's URL serves equally; this only
    selects *which* deployment to download the swagger file from.
    """
    if env is not None:
        if env not in svc_cfg.envs:
            raise ConfigError(
                f"env {env!r} not configured for this service "
                f"(configured: {sorted(svc_cfg.envs)})"
            )
        return env
    if svc_cfg.canonical_env:
        return svc_cfg.canonical_env
    if len(svc_cfg.envs) == 1:
        return next(iter(svc_cfg.envs))
    raise ConfigError(
        "cannot pick an env to fetch the spec from: multiple envs configured "
        f"and no canonical_env set (configured: {sorted(svc_cfg.envs)})"
    )


def spec_source_url(
    config: Config,
    platform: str,
    region: str,
    service: str,
    env: str | None = None,
) -> str:
    """Resolve the full swagger URL for a (platform, region, service).

    Navigates ``platform → region → service`` and appends the service's
    ``spec_path`` to the chosen env's base URL. The fetch is unauthenticated —
    swagger endpoints require no bearer token. Raises ``ConfigError`` when any
    component of the tuple is not configured.
    """
    if platform not in config.platforms:
        raise ConfigError(
            f"platform {platform!r} not configured "
            f"(configured: {sorted(config.platforms)})"
        )
    platform_cfg = config.platforms[platform]
    if region not in platform_cfg.regions:
        raise ConfigError(
            f"region {region!r} not configured under platform {platform!r} "
            f"(configured: {sorted(platform_cfg.regions)})"
        )
    region_cfg = platform_cfg.regions[region]
    if service not in region_cfg.services:
        raise ConfigError(
            f"service {service!r} not configured under platform {platform!r} "
            f"region {region!r} (configured: {sorted(region_cfg.services)})"
        )
    svc_cfg = region_cfg.services[service]
    env_name = resolve_spec_env(svc_cfg, env)
    api_base = str(svc_cfg.envs[env_name].url).rstrip("/")
    return api_base + svc_cfg.spec_path


def resolve(arg_name: str, arg_value: str | None, default_value: str | None) -> str:
    """Return ``arg_value`` if set, else ``default_value``; raise if both ``None``."""
    if arg_value is not None:
        return arg_value
    if default_value is not None:
        return default_value
    raise ConfigError(f"missing {arg_name!r} (no default configured)")


def resolve_all(
    config: Config,
    *,
    region: str | None = None,
    env: str | None = None,
    platform: str | None = None,
    service: str | None = None,
    username: str | None = None,
) -> dict[str, str]:
    """Resolve any of the five common optional args at once."""
    out: dict[str, Any] = {}
    if region is not None or config.defaults.region is not None:
        out["region"] = resolve("region", region, config.defaults.region)
    if env is not None or config.defaults.env is not None:
        out["env"] = resolve("env", env, config.defaults.env)
    if platform is not None or config.defaults.platform is not None:
        out["platform"] = resolve("platform", platform, config.defaults.platform)
    if service is not None or config.defaults.service is not None:
        out["service"] = resolve("service", service, config.defaults.service)
    if username is not None or config.defaults.username is not None:
        out["username"] = resolve("username", username, config.defaults.username)
    return out
