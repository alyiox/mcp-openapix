"""call_endpoint — execute an OpenAPI operation.

The target is identified either by ``operation_id`` (resolved against the
spec) or by a raw ``method`` + ``path`` pair, which needs no spec entry and so
reaches unpublished endpoints.

Large response bodies are truncated to a configurable byte threshold in the
returned result; the full body is cached in memory and a
``openapi://responses/{request_id}`` resource URI is included so the client can
fetch the complete data on demand. An equivalent curl command is cached
alongside and exposed via ``openapi://curl/{request_id}``.
"""

from __future__ import annotations

import json
from typing import Any

from ..api_client import ApiClient, ApiResponse
from ..config import Config, resolve
from ..responses import ResponseCache

_TRUNCATION_MARKER = "\n... (truncated — full body at resource_uri)"


async def call_endpoint(
    *,
    config: Config,
    api_client: ApiClient,
    response_cache: ResponseCache,
    operation_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
    platform: str | None = None,
    region: str | None = None,
    service: str | None = None,
    env: str | None = None,
    username: str | None = None,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    platform_name = resolve("platform", platform, config.defaults.platform)
    region_name = resolve("region", region, config.defaults.region)
    service_name = resolve("service", service, config.defaults.service)
    env_name = resolve("env", env, config.defaults.env)
    user_name = username if username is not None else config.defaults.username

    response = await api_client.call(
        operation_id=operation_id,
        method=method,
        path=path,
        platform=platform_name,
        region=region_name,
        service=service_name,
        env=env_name,
        username=user_name,
        path_params=path_params,
        query_params=query_params,
        body=body,
        headers=headers,
        timeout=timeout,
    )
    return _build_result(response, response_cache, config.truncate_threshold)


def _build_result(
    response: ApiResponse, cache: ResponseCache, threshold: int
) -> dict[str, Any]:
    """Shape the tool result, truncating an oversized body and caching it whole.

    When the serialized body exceeds ``threshold`` bytes, the full body is
    stored in ``cache`` under ``response.request_id`` and the result carries a
    line-aligned preview plus a ``openapi://responses/{request_id}`` URI; the
    ``status`` and ``headers`` are always returned in full. The equivalent curl
    command is cached under ``curl/{request_id}`` and surfaced as a
    ``openapi://curl/{request_id}`` URI rather than inlined, since it repeats the
    body and the (time-limited) auth headers.
    """
    result: dict[str, Any] = {"status": response.status, "headers": response.headers}

    if response.curl is not None:
        cache.put(f"curl/{response.request_id}", response.curl)
        result["curl_uri"] = f"openapi://curl/{response.request_id}"

    body = response.body
    if isinstance(body, str):
        body_str = body
    else:
        body_str = json.dumps(body, indent=2, ensure_ascii=False)

    if len(body_str.encode("utf-8")) > threshold:
        cache.put(response.request_id, body)
        preview = body_str[:threshold].rsplit("\n", 1)[0]
        result["body"] = preview + _TRUNCATION_MARKER
        result["truncated"] = True
        result["resource_uri"] = f"openapi://responses/{response.request_id}"
    else:
        result["body"] = body

    return result
