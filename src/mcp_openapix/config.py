"""Configuration loading and validation for the OpenAPI MCP server.

Schema: platform → region → services → service → env. A separate top-level
``token_helpers`` section holds named token helpers; any level of the hierarchy
MAY name one, and the most specific wins. The per-service base URL lives in the
env.

An optional ``config.d/`` beside ``config.json`` holds drop-in files, in the
style of an nginx ``conf.d``: each may declare anything ``config.json`` may, and
they merge over it in sorted order as if pasted into one file -- objects merge
recursively, a later scalar or list replaces an earlier one. The point is blast
radius: a stray comma while editing one platform otherwise takes down every
platform, because a parse failure precedes per-platform validation. Split out, a
file that fails to parse costs only what it declares, and a platform that fails
validation costs only itself.

See ``docs/token-protocol.md`` for the helper contract.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import ItemsView, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PrivateAttr,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)

logger = logging.getLogger(__name__)

#: Directory beside config.json holding drop-in config files.
CONFIG_D = "config.d"

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
        return (self.__pydantic_extra__ or {})[name]

    def __contains__(self, name: object) -> bool:
        return name in (self.__pydantic_extra__ or {})

    # Iterating service names is the point of this model, so the mapping
    # signature deliberately replaces pydantic's field iteration.
    def __iter__(self) -> Iterator[str]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return iter(self.__pydantic_extra__ or {})

    def __len__(self) -> int:
        return len(self.__pydantic_extra__ or {})

    def items(self) -> ItemsView[str, ServiceConfig]:
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
    and leaves ``mcp-openapix --refresh`` working.

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
    """Top-level config.json schema, plus what went wrong while loading it.

    ``platform_errors`` maps a platform that failed to load to the loader's
    message; the platform itself is absent from ``platforms``. ``file_errors``
    maps a drop-in that could not be read at all to why -- a separate case,
    because what an unreadable file declares is unknowable. ``overrides`` lists
    ``(setting, earlier file, later file)`` for every value a later file
    replaced: allowed, but worth being able to see.
    """

    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    platforms: dict[str, PlatformConfig] = Field(default_factory=dict)
    token_helpers: dict[str, TokenHelperConfig] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    response_cache_ttl: int = Field(default=3600, ge=0)
    truncate_threshold: int = Field(default=4096, gt=0)
    spec_refresh: SpecRefresh = Field(default_factory=SpecRefresh)

    _platform_errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _file_errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _overrides: list[tuple[str, str, str]] = PrivateAttr(default_factory=list)

    @property
    def platform_errors(self) -> dict[str, str]:
        return self._platform_errors

    @property
    def file_errors(self) -> dict[str, str]:
        return self._file_errors

    @property
    def overrides(self) -> list[tuple[str, str, str]]:
        return self._overrides


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
    return base / ".config" / "mcp-openapix" / "config.json"


def default_cache_dir() -> Path:
    """Return the user-scoped spec cache root for this OS.

    Rooted at the user's home directory (``$USERPROFILE`` on Windows, ``$HOME``
    otherwise). The server caches fetched OpenAPI specs under
    ``<root>/.cache/mcp-openapix/<platform>/<region>/<service>.json``.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        base = Path.home()
    return base / ".cache" / "mcp-openapix"


def load_config(path: Path | None = None) -> Config:
    """Load and validate config.json merged with any ``config.d/*.json`` drop-ins.

    Args:
        path: explicit config path; falls back to ``default_config_path()``.

    Raises:
        ConfigError: if config.json is missing or unreadable, a setting outside
            ``platforms`` fails validation (the message names the file that set
            it), or defaults are inconsistent. A drop-in that cannot be read, or
            a platform that fails validation, is recorded in ``file_errors`` /
            ``platform_errors`` instead and the rest load.
    """
    cfg_path = path or default_config_path()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")

    merged: dict[str, Any] = {}
    owners: dict[tuple[str, ...], Path] = {}
    overrides: list[tuple[str, str, str]] = []
    _deep_merge(
        merged,
        _read_config_file(cfg_path),
        source=cfg_path,
        owners=owners,
        overrides=overrides,
    )
    # Each drop-in is read on its own before any of it is merged, so an
    # unreadable one costs exactly what it declares -- the reason the directory
    # exists.
    file_errors: dict[str, str] = {}
    for extra in _drop_in_files(cfg_path):
        try:
            block = _read_config_file(extra)
        except ConfigError as e:
            file_errors[str(extra)] = str(e)
            continue
        _deep_merge(merged, block, source=extra, owners=owners, overrides=overrides)

    raw_platforms = merged.pop("platforms", {})
    if not isinstance(raw_platforms, dict):
        files = _files_for(("platforms",), owners)
        raise ConfigError(
            f"config failed validation: {files}: platforms: must be an object"
        )
    try:
        config = Config.model_validate(merged)
    except ValidationError as e:
        files = sorted(
            {
                _files_for(tuple(str(p) for p in err["loc"]), owners)
                for err in e.errors()
            }
        )
        raise ConfigError(f"config failed validation: {', '.join(files)}: {e}") from e
    config.file_errors.update(file_errors)
    config.overrides.extend(overrides)
    # Before the platforms: every one of them falls back to this helper, so a
    # bad name would otherwise surface as each platform failing on its own.
    _validate_token_helpers(config)
    _load_platforms(config, raw_platforms, owners=owners)

    _drop_defaults_of_failed_platform(config)
    _validate_defaults(config)

    for file, message in config.file_errors.items():
        logger.warning("config file skipped: %s: %s", file, message)
    for message in config.platform_errors.values():
        logger.warning("%s", message)
    return config


def _drop_in_files(base: Path) -> list[Path]:
    """``*.json`` directly inside ``config.d/``, sorted for a stable merge order.

    Only that exact glob: a ``.bak`` or an editor swap file beside a real config
    is litter, and silently merging one would be worse than ignoring it.
    """
    directory = base.parent / CONFIG_D
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.json") if p.is_file())


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"config file is not readable: {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file is not valid JSON: {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config file must contain a JSON object: {path}")
    return raw


def _deep_merge(
    merged: dict[str, Any],
    block: dict[str, Any],
    *,
    source: Path,
    owners: dict[tuple[str, ...], Path],
    overrides: list[tuple[str, str, str]],
    at: tuple[str, ...] = (),
) -> None:
    """Merge ``block`` into ``merged`` as if the files were pasted in order.

    Objects merge key by key; anything else replaces what was there. ``owners``
    records the file that set each non-object value, so an error or an
    override can name it.
    """
    for key, value in block.items():
        path = (*at, key)
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _deep_merge(
                current,
                value,
                source=source,
                owners=owners,
                overrides=overrides,
                at=path,
            )
            continue
        if key in merged:
            replaced = [p for p in owners if p[: len(path)] == path]
            earlier_files = sorted({owners.pop(p) for p in replaced})
            # Restating the same value is not an override worth reporting.
            if current != value:
                for earlier in earlier_files:
                    overrides.append((".".join(path), str(earlier), str(source)))
        if isinstance(value, dict):
            merged[key] = {}
            _deep_merge(
                merged[key],
                value,
                source=source,
                owners=owners,
                overrides=overrides,
                at=path,
            )
            # An empty object still has to be traceable to its file.
            if not value:
                owners[path] = source
        else:
            merged[key] = value
            owners[path] = source


def _files_for(path: tuple[str, ...], owners: dict[tuple[str, ...], Path]) -> str:
    """The files that set anything at or under ``path``, or that ``path`` is under."""
    files = {
        str(file)
        for owned, file in owners.items()
        if owned[: len(path)] == path or path[: len(owned)] == owned
    }
    return ", ".join(sorted(files))


def _load_platforms(
    config: Config,
    raw_platforms: dict[str, Any],
    *,
    owners: dict[tuple[str, ...], Path],
) -> None:
    """Validate platforms one by one into ``config``.

    Anything wrong with a platform -- its shape, or a token helper it names
    that no file declares -- costs only that platform.
    """
    for name, raw_platform in raw_platforms.items():
        try:
            config.platforms[name] = PlatformConfig.model_validate(raw_platform)
            _validate_platform_token_helpers(config, name)
        except (ValidationError, ConfigError) as e:
            config.platforms.pop(name, None)
            config.platform_errors[name] = (
                f"platform {name!r} failed to load from "
                f"{_files_for(('platforms', name), owners)}: {e}"
                f"{_unreadable_hint(config)}; fix it and restart the server"
            )


def _unreadable_hint(config: Config) -> str:
    """Name the skipped drop-ins, which may hold what a lookup was missing."""
    if not config.file_errors:
        return ""
    return (
        f"; {len(config.file_errors)} config file(s) could not be read: "
        f"{', '.join(sorted(config.file_errors))}"
    )


def _drop_defaults_of_failed_platform(config: Config) -> None:
    """Unset the defaults that point into a platform that failed to load.

    The failure is already reported; rejecting the whole config over a default
    would bring back the blast radius the per-platform isolation removes.
    """
    defaults = config.defaults
    if defaults.platform is None or defaults.platform not in config.platform_errors:
        return
    logger.warning(
        "defaults.platform=%r failed to load; ignoring defaults.platform, "
        ".region, .service and .env",
        defaults.platform,
    )
    defaults.platform = defaults.region = defaults.service = defaults.env = None


def _validate_token_helpers(config: Config) -> None:
    """Check that the default helper exists; platforms are checked as merged."""
    if (
        config.defaults.token_helper is not None
        and config.defaults.token_helper not in config.token_helpers
    ):
        raise ConfigError(
            f"defaults.token_helper={config.defaults.token_helper!r} is not in "
            f"token_helpers (configured: {sorted(config.token_helpers)})"
        )


def _validate_platform_token_helpers(config: Config, platform: str) -> None:
    """Check that every deployment of one platform binds to a real helper."""
    for region, region_cfg in config.platforms[platform].regions.items():
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


def get_platform(config: Config, platform: str) -> PlatformConfig:
    """Return a platform's config, or raise saying why it is not there.

    A platform that failed to load raises the loader's own message; an unknown
    one names the configured platforms and any drop-in that could not be read,
    since the platform may be declared in it.
    """
    if platform in config.platforms:
        return config.platforms[platform]
    if platform in config.platform_errors:
        raise ConfigError(config.platform_errors[platform])
    raise ConfigError(
        f"platform {platform!r} not configured "
        f"(configured: {sorted(config.platforms)}){_unreadable_hint(config)}"
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
    platform_cfg = get_platform(config, platform)
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
    platform_cfg = get_platform(config, platform)
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
