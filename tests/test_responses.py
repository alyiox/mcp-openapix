"""Tests for the in-memory response cache backing the responses resource."""

from __future__ import annotations

import time

from mcp_openapix.responses import ResponseCache, read_cached_response


def test_cache_store_and_get() -> None:
    cache = ResponseCache(ttl_seconds=60)
    cache.put("req-1", {"key": "value"})
    assert cache.get("req-1") == {"key": "value"}


def test_cache_expiry() -> None:
    cache = ResponseCache(ttl_seconds=0)
    cache.put("req-1", {"key": "value"})
    time.sleep(0.01)
    assert cache.get("req-1") is None


def test_cache_missing_key() -> None:
    assert ResponseCache().get("nonexistent") is None


def test_cache_list_ids_drops_expired() -> None:
    cache = ResponseCache(ttl_seconds=0)
    cache.put("a", 1)
    time.sleep(0.01)
    assert cache.list_ids() == []


def test_read_cached_response_json() -> None:
    cache = ResponseCache()
    cache.put("req-x", [{"id": 1}, {"id": 2}])
    result = read_cached_response("req-x", cache)
    assert result is not None
    assert '"id": 1' in result


def test_read_cached_response_string_verbatim() -> None:
    cache = ResponseCache()
    cache.put("req-s", "plain text body")
    assert read_cached_response("req-s", cache) == "plain text body"


def test_read_cached_response_non_ascii() -> None:
    cache = ResponseCache()
    cache.put("req-cn", {"name": "京东"})
    result = read_cached_response("req-cn", cache)
    assert result is not None
    assert "京东" in result  # ensure_ascii=False keeps CJK readable


def test_read_cached_response_missing() -> None:
    assert read_cached_response("nope", ResponseCache()) is None
