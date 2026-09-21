# mcp-openapix

[![CI](https://github.com/alyiox/mcp-openapix/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/alyiox/mcp-openapix/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp-openapix.svg)](https://pypi.org/project/mcp-openapix/)
[![Python
3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

<!-- mcp-name: io.github.alyiox/mcp-openapix -->

[MCP](https://modelcontextprotocol.io) server that fronts **any** OpenAPI service behind
four generic tools.

An agent finds operations in each deployment's OpenAPI document and calls them; the
server resolves the URL, obtains a bearer token, and builds the request. Discovery is
`list_platforms`, `list_endpoints` and `describe_endpoint`; execution is the generic
proxy `call_endpoint`.

```
example / us / items / prod
  │     │     │      └── env ......... which deployment URL a call reaches
  │     │     └───────── service ..... one backend, one OpenAPI spec
  │     └─────────────── region ...... a geographic deployment
  └───────────────────── platform .... the product or API family
```

## Requirements

- Python 3.13+ and [`uv`](https://docs.astral.sh/uv/)
- A `config.json` describing the deployments you hold credentials for

## Quick start

Set up your config (see [Configuration](#configuration)), then run the server:

```bash
# Run directly with uvx (no clone needed)
npx -y @modelcontextprotocol/inspector@latest uvx mcp-openapix
```

```bash
# Or run from source
npx -y @modelcontextprotocol/inspector@latest uv run mcp-openapix
```

## Configuration

`config.json` MUST live at `~/.config/mcp-openapix/config.json`
(`%USERPROFILE%\.config\…` on Windows). `config.example.json` is a full template.

```json
{
  "headers": { "accept": "application/json" },
  "defaults": { "platform": "example", "region": "us", "service": "items", "env": "prod" },
  "platforms": {
    "example": {
      "regions": {
        "us": {
          "services": {
            "token_helper": "us",
            "items": {
              "desc": "Catalogue and inventory API",
              "spec_path": "/swagger/v1/swagger.json",
              "canonical_env": "prod",
              "envs": {
                "prod": { "url": "https://api.example.com/items" },
                "dev":  { "url": "https://api-dev.example.com/items" }
              }
            }
          }
        }
      }
    }
  },
  "token_helpers": {
    "us": {
      "command": "token-helper",
      "args": ["issue"]
    }
  }
}
```

### `platforms`

A hierarchy of `platform → region → services → service → env`. Each service declares:

| Field | Notes |
|---|---|
| `spec_path` | Required. The OpenAPI JSON endpoint relative to the service URL |
| `canonical_env` | Required when more than one env is configured — the env whose URL the spec is fetched from |
| `envs` | Required. One entry per deployment environment, each carrying a full base `url` |
| `desc` | Optional. A short description surfaced by `list_platforms` |
| `token_helper` | Optional. The token helper this level binds to |

The `services` object may also contain a `token_helper` default applying to all services
in that region. A service or environment can override it.

### `token_helpers`

Named token helpers, in the same shape as an MCP server entry:

| Field | Required | Default | Notes |
|---|---|---|---|
| `command` | yes | — | Resolved on `PATH`; never run through a shell |
| `args` | no | `[]` | Passed verbatim |
| `timeout` | no | `60` | Seconds before the helper's process group is killed; at most `300` |

The config names a command and nothing else, so `config.json` holds **no secrets**.
The complete helper invocation and output contract is documented in
[`docs/token-protocol.md`](docs/token-protocol.md).

Which helper a call uses is resolved most-specific-first:

```
env.token_helper → service.token_helper → services.token_helper
→ region.token_helper → platform.token_helper → defaults.token_helper
```

If no level declares a helper, the deployment is unauthenticated. Omit
`token_helper` for public deployments.

### `headers`

Constant headers added to every API call — for APIs that require a tenant, product or
locale header:

```json
"headers": { "accept": "application/json", "x-product": "example" }
```

### `defaults`

Makes every tool argument optional: a call falls back to `defaults.platform`, `.region`,
`.service`, `.env`, `.username` and `.token_helper` when they are omitted.

### Top-level options

| Field | Default | Notes |
|---|---|---|
| `truncate_threshold` | `1024` | Response bytes returned inline before truncating to a preview |
| `response_cache_ttl` | `3600` | Seconds a truncated body stays readable at its resource URI |
| `spec_refresh` | `{"auto": true, "interval": 7}` | Background spec refresh; `interval` is days and MAY be fractional |

## Tools

| Tool | Purpose |
|---|---|
| `list_platforms` | Every platform with its regions, services, and envs |
| `list_endpoints` | A service's operations, filtered by `query`, `tag` or `method` |
| `describe_endpoint` | One operation plus the transitive closure of the schemas it references |
| `call_endpoint` | Execute an operation, or a raw `method` + `path` absent from the spec |

### Operation ids

Many OpenAPI documents omit `operationId`, so the server synthesizes one as `"<METHOD>
<path>"`:

```
POST /api/items
└─┬─┘ └───┬───┘
method  path as the spec declares it
```

Where a spec does declare an `operationId`, that value wins.

## Specs

Specs are **not** bundled. Each deployment's document is fetched on demand — an
unauthenticated `GET` — and cached under
`~/.cache/mcp-openapix/{platform}/{region}/{service}.json`.

A document MUST declare at least one operation before it is installed, so a deployment
answering `200` with an error body cannot replace a working snapshot with one that
serves nothing.

Cached specs refresh in the background: once at startup, then every
`spec_refresh.interval` days. Set `auto` to `false` to stop it; the manual lever still
works:

```bash
uvx mcp-openapix --refresh
```

## MCP resources

| Resource URI | Description |
|---|---|
| `openapi://responses/{request_id}` | Full body of a truncated `call_endpoint` response |
| `openapi://curl/{request_id}` | Equivalent curl command for a `call_endpoint` request |

Both expire `response_cache_ttl` seconds after the call. The curl command may embed
a short-lived token.

## Tokens at rest

Tokens are cached in memory and, when expiry metadata is available, under
`~/.cache/mcp-openapix/tokens/` (mode `0600`) keyed by the token-helper declaration
and username. This lets client sessions share a login without spawning a helper each.
A `401` retires the cached token so the next call obtains a fresh one. To clear them all:

```bash
uvx mcp-openapix --logout
```

## MCP host examples

<details> <summary><b>Cursor / Claude Code</b></summary>

```json
{
  "mcpServers": {
    "openapi": { "command": "uvx", "args": ["mcp-openapix"] }
  }
}
```

</details>

<details> <summary><b>Codex</b></summary>

```toml
[mcp_servers.openapi]
command = "uvx"
args = ["mcp-openapix"]
```

</details>

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

All four MUST pass; see `AGENTS.md`. Tests use
[`respx`](https://github.com/lundberg/respx) to mock HTTP and real subprocesses for
token helpers, so no live API access is required.

## License

[MIT](LICENSE).
