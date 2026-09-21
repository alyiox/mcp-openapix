"""MCPServer entrypoint that registers the OpenAPI tools."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, SkipValidation

from . import refresh
from .api_client import ApiClient
from .auth import TokenProvider
from .config import Config, default_cache_dir, load_config
from .responses import ResponseCache, read_cached_response
from .spec_loader import SpecRegistry, build_registry
from .tools.call_endpoint import call_endpoint as _call_endpoint
from .tools.describe_endpoint import describe_endpoint as _describe_endpoint
from .tools.list_endpoints import list_endpoints as _list_endpoints
from .tools.list_platforms import list_platforms as _list_platforms

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    config: Config
    registry: SpecRegistry
    http_client: httpx.AsyncClient
    tokens: TokenProvider
    api_client: ApiClient
    response_cache: ResponseCache
    specs_dir: Path


def _specs_dir() -> Path:
    # User-local cache; specs are fetched on demand, not bundled in the repo.
    return default_cache_dir()


@asynccontextmanager
async def _lifespan(app: MCPServer[ServerContext]) -> AsyncIterator[ServerContext]:
    config = load_config()
    specs_dir = _specs_dir()
    registry = build_registry(config, specs_dir)
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        tokens = TokenProvider(specs_dir)
        api_client = ApiClient(config, registry, http_client, tokens)
        response_cache = ResponseCache(ttl_seconds=config.response_cache_ttl)
        ctx = ServerContext(
            config=config,
            registry=registry,
            http_client=http_client,
            tokens=tokens,
            api_client=api_client,
            response_cache=response_cache,
            specs_dir=specs_dir,
        )
        logger.info(
            "mcp-openapi ready: %d platforms, %d token helpers",
            len(registry.platforms),
            len(config.token_helpers),
        )
        if not config.spec_refresh.auto:
            yield ctx
            return
        task = asyncio.create_task(
            refresh.refresh_loop(
                config, registry, specs_dir, config.spec_refresh.interval_seconds
            )
        )
        try:
            yield ctx
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


mcp: MCPServer[ServerContext] = MCPServer("mcp-openapi", lifespan=_lifespan)


# Resource handlers are wrapped in pydantic's ``validate_call`` by
# ``ResourceTemplate.from_function``; tool handlers are not. A parameterized
# ``Context[ServerContext]`` annotation is a different concrete class from the
# bare ``Context`` the SDK injects, so pydantic would *rebuild* the model from
# its public fields and silently drop the private ``_request_context`` --
# making every ``resources/read`` fail. ``SkipValidation`` passes the injected
# instance through untouched while keeping the static type.
ResourceContext = SkipValidation[Context[ServerContext]]


def _server_ctx(ctx: Context[ServerContext]) -> ServerContext:
    return ctx.request_context.lifespan_context


@mcp.resource(
    "openapi://responses/{request_id}",
    name="cached_response",
    description=(
        "Full body of a truncated call_endpoint response, cached in "
        "memory (TTL from config.response_cache_ttl). Src: responses"
    ),
)
def cached_response(request_id: str, ctx: ResourceContext) -> str:
    content = read_cached_response(request_id, _server_ctx(ctx).response_cache)
    if content is None:
        return (
            f"No cached response found for request_id={request_id} "
            "(it may have expired or never been truncated)."
        )
    return content


@mcp.resource(
    "openapi://curl/{request_id}",
    name="request_curl",
    description=(
        "Equivalent curl command for a call_endpoint request, cached in "
        "memory (TTL from config.response_cache_ttl). Auth headers are "
        "time-limited. Src: responses"
    ),
)
def request_curl(request_id: str, ctx: ResourceContext) -> str:
    data = _server_ctx(ctx).response_cache.get(f"curl/{request_id}")
    if data is None:
        return (
            f"No curl command found for request_id={request_id} "
            "(it may have expired or never been recorded)."
        )
    return f"# curl (auth headers are time-limited)\n\n{data}"


@mcp.tool(
    name="list_platforms",
    description=(
        "[OpenAPI] List platforms with their served regions, services, "
        "envs, and optional service descriptions."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def list_platforms(
    ctx: Context[ServerContext],
    region: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Optional region filter; only platforms served in "
                "this region are returned. Src: regions"
            ),
        ),
    ] = None,
) -> list[dict[str, Any]]:
    server = _server_ctx(ctx)
    return _list_platforms(
        config=server.config, registry=server.registry, region=region
    )


@mcp.tool(
    name="list_endpoints",
    description=(
        "[OpenAPI] List OpenAPI operations for a service with optional filters."
    ),
    # Not read-only: specs are not bundled, so the first call may fetch the
    # document and install it in the local cache, and writing a file is a change.
    # Additive only, and a repeat call has no further effect once it is cached.
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def list_endpoints(
    ctx: Context[ServerContext],
    platform: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Platform name; defaults to defaults.platform "
                "when omitted. Src: platforms"
            ),
        ),
    ] = None,
    region: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Region name; defaults to defaults.region when "
                "omitted. Src: regions"
            ),
        ),
    ] = None,
    service: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Service name (e.g. newui, engine); defaults to "
                "defaults.service when omitted. Src: services"
            ),
        ),
    ] = None,
    query: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Substring match (case-insensitive) on "
                "operationId, path, or summary."
            ),
        ),
    ] = None,
    tag: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Filter to operations whose OpenAPI tags include this value."
            ),
        ),
    ] = None,
    method: Annotated[
        str | None,
        Field(
            default=None,
            description=("[OpenAPI] Filter by HTTP verb (GET/POST/PUT/DELETE/PATCH)."),
        ),
    ] = None,
) -> list[dict[str, Any]]:
    server = _server_ctx(ctx)
    return await _list_endpoints(
        config=server.config,
        registry=server.registry,
        platform=platform,
        region=region,
        service=service,
        query=query,
        tag=tag,
        method=method,
    )


@mcp.tool(
    name="describe_endpoint",
    description="[OpenAPI] Get the full OpenAPI schema for one operation.",
    # Not read-only: specs are not bundled, so the first call may fetch the
    # document and install it in the local cache, and writing a file is a change.
    # Additive only, and a repeat call has no further effect once it is cached.
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def describe_endpoint(
    ctx: Context[ServerContext],
    operation_id: Annotated[
        str,
        Field(
            description=(
                "[OpenAPI] Operation ID, typically synthesized as 'METHOD path' "
                "(e.g. 'POST /api/sponsor/report/adGroup/list') unless the spec "
                "declares an operationId. Src: operations"
            )
        ),
    ],
    platform: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Platform name; defaults to defaults.platform "
                "when omitted. Src: platforms"
            ),
        ),
    ] = None,
    region: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Region name; defaults to defaults.region when "
                "omitted. Src: regions"
            ),
        ),
    ] = None,
    service: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Service name (e.g. newui, engine); defaults to "
                "defaults.service when omitted. Src: services"
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    server = _server_ctx(ctx)
    return await _describe_endpoint(
        config=server.config,
        registry=server.registry,
        operation_id=operation_id,
        platform=platform,
        region=region,
        service=service,
    )


@mcp.tool(
    name="call_endpoint",
    description=(
        "[OpenAPI] Execute an API operation with bearer auth. Identify it by "
        "operation_id, or by raw method + path; raw method+path also reaches "
        "unpublished endpoints absent from the specs. Bodies over the configured "
        "byte threshold are truncated to a preview; read the returned resource_uri "
        "(openapi://responses/{request_id}) for full data. The result also carries "
        "a curl_uri (openapi://curl/{request_id}) with the equivalent curl command."
    ),
    # Passthrough to any spec operation — the caller picks the verb, so assume
    # the most cautious shape: writes, may delete, retries are not safe.
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def call_endpoint(
    ctx: Context[ServerContext],
    operation_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Operation ID, typically synthesized as 'METHOD path' "
                "(e.g. 'POST /api/sponsor/report/adGroup/list') unless the spec "
                "declares an operationId. Provide this, or both method and path. "
                "Src: operations"
            ),
        ),
    ] = None,
    method: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] HTTP verb (GET/POST/PUT/PATCH/DELETE) for a raw call; "
                "pair with path when no operation_id is given."
            ),
        ),
    ] = None,
    path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Request path for a raw call "
                "(e.g. '/api/sponsor/campaign/list'), appended to the service "
                "base URL; may include {param} placeholders. Reaches unpublished "
                "endpoints absent from the specs."
            ),
        ),
    ] = None,
    platform: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Platform name; defaults to defaults.platform. Src: platforms"
            ),
        ),
    ] = None,
    region: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Region name; defaults to defaults.region. Src: regions"
            ),
        ),
    ] = None,
    service: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Service name (e.g. newui, api); "
                "defaults to defaults.service. Src: services"
            ),
        ),
    ] = None,
    env: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Deployment env (dev/stage/prod/...); "
                "defaults to defaults.env. Src: envs"
            ),
        ),
    ] = None,
    username: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Identity to authenticate as; passed to the auth "
                "helper. Defaults to defaults.username."
            ),
        ),
    ] = None,
    path_params: Annotated[
        dict[str, Any] | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Values for {param} placeholders in the operation path."
            ),
        ),
    ] = None,
    query_params: Annotated[
        dict[str, Any] | None,
        Field(
            default=None,
            description=("[OpenAPI] Query string parameters as a key/value object."),
        ),
    ] = None,
    body: Annotated[
        Any,
        Field(
            default=None,
            description=(
                "[OpenAPI] Request body; dict/list serialized as JSON, "
                "str/bytes sent verbatim."
            ),
        ),
    ] = None,
    headers: Annotated[
        dict[str, str] | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Extra request headers; Authorization is always overridden."
            ),
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        Field(
            default=None,
            description=(
                "[OpenAPI] Per-request timeout in seconds; overrides the default "
                "30s client timeout. Raise it for slow report/export operations."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    server = _server_ctx(ctx)
    return await _call_endpoint(
        config=server.config,
        api_client=server.api_client,
        response_cache=server.response_cache,
        operation_id=operation_id,
        method=method,
        path=path,
        platform=platform,
        region=region,
        service=service,
        env=env,
        username=username,
        path_params=path_params,
        query_params=query_params,
        body=body,
        headers=headers,
        timeout=timeout,
    )


def _refresh_now() -> int:
    """Sweep every cached spec now, ignoring the interval, and report the result.

    The manual lever that replaces the old tool: the user knows when a refresh is
    worth doing -- they hit the endpoint the snapshot does not have -- and a
    schedule alone cannot answer that until its next run.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config()
    specs_dir = _specs_dir()
    registry = build_registry(config, specs_dir)
    rows = asyncio.run(refresh.refresh_due(config, registry, specs_dir, force=True))
    if not rows:
        print("nothing cached to refresh, or another process is refreshing")
        return 0
    width = max(len(row["spec"]) for row in rows) + 2
    for row in rows:
        detail = row.get("error") or f"{row.get('operations')} operations"
        print(f"{row['spec']:<{width}}{row['status']:<11}{detail}")
    return 1 if any(row["status"] == "error" for row in rows) else 0


def _logout() -> int:
    """Wipe every cached token.

    Tokens at rest are a secret this design introduces that an in-memory cache
    did not, so there is one obvious way to clear them.
    """
    removed = TokenProvider(_specs_dir()).purge()
    print(f"removed {removed} cached token(s)")
    return 0


def main() -> None:
    if "--refresh" in sys.argv[1:]:
        raise SystemExit(_refresh_now())
    if "--logout" in sys.argv[1:]:
        raise SystemExit(_logout())
    logging.basicConfig(
        level=os.environ.get("MCP_OPENAPI_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
