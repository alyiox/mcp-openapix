"""HTTP client that resolves an OpenAPI operation to a concrete request."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import TokenProvider
from .config import Config, get_deployment
from .spec_loader import Operation, SpecRegistry, synthesize_operation_id

logger = logging.getLogger(__name__)

PATH_PARAM_RE = re.compile(r"\{([^{}]+)\}")


class ApiClientError(Exception):
    """Raised on path-param errors and other request-construction failures."""


@dataclass
class ApiResponse:
    status: int
    headers: dict[str, str]
    body: Any
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    curl: str | None = None


def build_curl(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    json_body: Any = None,
    content_body: str | bytes | None = None,
) -> str:
    """Render an equivalent ``curl`` command for an executed request.

    Auth headers are time-limited, so the command is a short-lived reproduction
    aid rather than a durable script.
    """
    parts = [f"curl -X {method.upper()} '{url}'"]
    for key, value in headers.items():
        parts.append(f"  -H '{key}: {value}'")
    if json_body is not None:
        parts.append(f"  -d '{json.dumps(json_body, ensure_ascii=False)}'")
    elif content_body is not None:
        data = (
            content_body.decode("utf-8", "replace")
            if isinstance(content_body, bytes)
            else content_body
        )
        parts.append(f"  -d '{data}'")
    return " \\\n".join(parts)


class ApiClient:
    """Resolves operationId + caller args into an authenticated HTTP call."""

    def __init__(
        self,
        config: Config,
        registry: SpecRegistry,
        client: httpx.AsyncClient,
        tokens: TokenProvider,
    ) -> None:
        self._config = config
        self._registry = registry
        self._client = client
        self._tokens = tokens

    async def call(
        self,
        *,
        platform: str,
        region: str,
        service: str,
        env: str,
        username: str | None = None,
        operation_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ApiResponse:
        operation = await self._resolve_operation(
            platform, region, service, operation_id, method, path
        )
        deployment = get_deployment(self._config, platform, region, service, env)

        url = self._build_url(deployment.api_base, operation, path_params or {})
        merged_headers: dict[str, str] = dict(deployment.headers)
        if headers:
            merged_headers.update(headers)
        auth_header = await self._tokens.header_for(deployment, username)
        if auth_header is not None:
            name, value = auth_header
            # The token replaces any header of that name already on the
            # request, whatever its casing -- two spellings of one name are two
            # headers on the wire, not one, and the server's own credential
            # always wins over a caller's.
            merged_headers = {
                k: v for k, v in merged_headers.items() if k.lower() != name.lower()
            }
            merged_headers[name] = value

        send_body = body if body is not None else None
        json_body = None
        content_body = None
        if isinstance(send_body, (dict, list)):
            json_body = send_body
        elif isinstance(send_body, (str, bytes)):
            content_body = send_body

        # httpx treats ``timeout=None`` as "disable timeout", so only pass it
        # through when the caller supplied one; otherwise the client default
        # (set in the lifespan) applies.
        request_kwargs: dict[str, Any] = {
            "method": operation.method.upper(),
            "url": url,
            "params": query_params,
            "json": json_body,
            "content": content_body,
            "headers": merged_headers,
        }
        if timeout is not None:
            request_kwargs["timeout"] = timeout

        response = await self._client.request(**request_kwargs)
        if response.status_code == 401 and auth_header is not None:
            # A 401 retires the token whatever its expiry claimed, so the next
            # call mints one instead of replaying a credential already refused.
            # Not retried here: the caller picks the verb, and a silent second
            # attempt at a write is not ours to make.
            logger.info(
                "upstream returned 401 for %s; retiring the cached token",
                operation.operation_id,
            )
            self._tokens.retire(deployment, username)
        curl = build_curl(
            operation.method,
            str(response.request.url),
            merged_headers,
            json_body=json_body,
            content_body=content_body,
        )
        return _build_response(response, curl)

    async def _resolve_operation(
        self,
        platform: str,
        region: str,
        service: str,
        operation_id: str | None,
        method: str | None,
        path: str | None,
    ) -> Operation:
        """Resolve the call target from either ``operation_id`` or raw method+path.

        ``operation_id`` is looked up in the spec registry (the spec is fetched
        on demand). A raw ``method`` + ``path`` builds an ad-hoc operation with
        no spec lookup, so alpha/beta/unpublished endpoints absent from the
        swagger files can still be called. ``operation_id`` wins when both are
        supplied.
        """
        if operation_id is not None:
            return await self._registry.get_operation(
                platform, region, service, operation_id
            )
        if method is not None and path is not None:
            normalized = path if path.startswith("/") else "/" + path
            return Operation(
                operation_id=synthesize_operation_id(method, normalized),
                platform=platform,
                region=region,
                service=service,
                method=method.lower(),
                path=normalized,
                raw={},
            )
        raise ApiClientError(
            "call requires either 'operation_id' or both 'method' and 'path'"
        )

    @staticmethod
    def _build_url(
        api_base: str, operation: Operation, path_params: dict[str, Any]
    ) -> str:
        required = set(PATH_PARAM_RE.findall(operation.path))
        missing = required - set(path_params)
        if missing:
            raise ApiClientError(
                f"missing path params for {operation.operation_id!r}: {sorted(missing)}"
            )
        path = operation.path
        for name, value in path_params.items():
            path = path.replace(f"{{{name}}}", str(value))
        return api_base.rstrip("/") + path


def _build_response(response: httpx.Response, curl: str | None = None) -> ApiResponse:
    """Parse the full response body. Size-based truncation is deferred to the
    tool layer, which previews large bodies while caching them whole for
    retrieval via the ``openapi://responses/{request_id}`` resource."""
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
    else:
        body = response.text
    return ApiResponse(
        status=response.status_code,
        headers=dict(response.headers),
        body=body,
        curl=curl,
    )
