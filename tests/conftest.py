"""Shared fixtures: temp config dir, mini OpenAPI spec, registry, http client."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from mcp_openapix.auth import TokenProvider
from mcp_openapix.config import Config
from mcp_openapix.spec_loader import SpecRegistry, build_registry


def echo_helper(text: str) -> dict:
    """A token helper that prints ``text``.

    Tests drive the real subprocess path rather than a stub, so the spawn,
    environment and stdout contract are all exercised.
    """
    return {"command": sys.executable, "args": ["-c", f"print({text!r})"]}


@pytest.fixture
def mini_spec() -> dict:
    """A minimal OpenAPI 3 spec with one path, no operationIds, and one $ref."""
    return {
        "openapi": "3.0.1",
        "info": {"title": "mini", "version": "1.0"},
        "paths": {
            "/api/items/{id}": {
                "get": {
                    "tags": ["Items"],
                    "summary": "Fetch one item",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/items": {
                "post": {
                    "tags": ["Items"],
                    "summary": "Create item",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateItem"}
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "owner": {"$ref": "#/components/schemas/Owner"},
                    },
                },
                "Owner": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
                "CreateItem": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
                "Unused": {"type": "string"},
            }
        },
    }


@pytest.fixture
def specs_dir(tmp_path: Path, mini_spec: dict) -> Path:
    """Cache root pre-populated for demo/{us,eu}/api so loads need no network."""
    out = tmp_path / "specs"
    for region in ("us", "eu"):
        region_dir = out / "demo" / region
        region_dir.mkdir(parents=True)
        (region_dir / "api.json").write_text(json.dumps(mini_spec), encoding="utf-8")
    return out


@pytest.fixture
def registry(config: Config, specs_dir: Path) -> SpecRegistry:
    return build_registry(config, specs_dir)


@pytest.fixture
def config_dict() -> dict:
    return {
        "defaults": {
            "region": "us",
            "env": "prod",
            "platform": "demo",
            "service": "api",
            "username": "tester",
        },
        "platforms": {
            "demo": {
                "regions": {
                    "us": {
                        "token_helper": "us",
                        "services": {
                            "api": {
                                "desc": "Demo items API",
                                "spec_path": "/swagger/v1/swagger.json",
                                "canonical_env": "prod",
                                "envs": {
                                    "prod": {
                                        "url": "https://api.example.com/demo",
                                    },
                                    "dev": {
                                        "url": "https://api-dev.example.com/demo",
                                    },
                                },
                            }
                        },
                    },
                    "eu": {
                        "services": {
                            "api": {
                                "spec_path": "/swagger/v1/swagger.json",
                                "canonical_env": "prod",
                                "envs": {
                                    "prod": {
                                        "url": "https://api-eu.example.com/demo",
                                    }
                                },
                            }
                        }
                    },
                }
            }
        },
        "headers": {"accept": "application/json", "x-product": "demo"},
        "token_helpers": {
            "us": echo_helper("us-token"),
            "eu": echo_helper("eu-token"),
        },
    }


@pytest.fixture
def config(config_dict: dict) -> Config:
    return Config.model_validate(config_dict)


@pytest.fixture
def config_path(tmp_path: Path, config_dict: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_dict), encoding="utf-8")
    return p


@pytest.fixture
def tokens(tmp_path: Path) -> TokenProvider:
    return TokenProvider(tmp_path / "cache")


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        yield client
