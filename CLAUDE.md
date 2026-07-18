# officecli-mcp

An MCP server wrapping OfficeCLI for OpenWebUI. Adds a handle-based HTTP file layer on top of OfficeCLI's path-only CLI/MCP, so a remote LLM client can push office documents and get rendered HTML/screenshots back.

## What this project is (and isn't)

- **Is**: a thin Python (FastMCP) bridge that (a) auto-downloads the `officecli` binary, (b) exposes an HTTP `/files` upload/download endpoint returning `file_id` handles, and (c) exposes handle-based MCP tools that shell out to `officecli`.
- **Is not**: a fork or patch of OfficeCLI. We invoke the upstream binary unchanged. OfficeCLI's own `officecli mcp` (stdio, single `command` string, path-only) is NOT used directly - it can't accept files from a remote client.

## Core design constraint (read before changing the file layer)

OpenWebUI **does not inject file bytes into MCP/OpenAPI tool parameters**, and the LLM never holds raw bytes. Only native OpenWebUI Python tools get `__files__` (and even there `data.content` is RAG-extracted *text*; raw bytes must be fetched via `GET /api/v1/files/{id}/content` with a Bearer API key). So the ONLY working path is: a small native OpenWebUI tool reads `__files__`, fetches bytes from OpenWebUI's own REST API, POSTs them to our `/files` endpoint, and returns a `file_id`. The LLM then passes `file_id` to MCP tools. Do not redesign toward base64-in-tool-params - it cannot work for the LLM. See `docs/superpowers/specs/` and the memory notes.

## Architecture

```
src/officecli_mcp/
  __init__.py
  __main__.py          # entry: choose streamable-HTTP or stdio via --transport
  server.py            # FastMCP app + mount HTTP /files router
  binary.py            # locate/download/verify officecli binary (latest release)
  runner.py            # resolve file_id -> path, build argv, exec officecli, capture output
  files.py             # HTTP file store: POST/GET/DELETE /files, workdir mgmt, TTL cleanup
  tools.py             # MCP tool definitions (handle-based): create, view_html, view_screenshot, view_text, edit, get, set, add, ...
  config.py            # env-based settings (ports, workdir, ttl, api key, officecli version pin, MCP host-header allow-list)
  models.py            # pydantic models for file_id, tool params, responses
examples/
  openwebui_officecli_file.py     # the merged OpenWebUI native tool shim (upload + download)
tests/
docs/superpowers/specs/
Dockerfile
docker-compose.yml
pyproject.toml
```

## Key facts about OfficeCLI (from research, 2026-07)

- Single self-contained .NET binary (~33MB). Releases: `officecli-linux-x64`, `-linux-arm64`, `-alpine-*`, `-mac-*`, `-win-*.exe`. Download base: `https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/<asset>`.
- .NET runtime dependency: crashes at startup (`Couldn't find a valid ICU package`) without `libicu`. We run it in globalization-invariant mode (`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`, set in the `Dockerfile` and in `runner.py::_subprocess_env()`) instead of bundling ICU. See "Runtime dependency: .NET globalization" in README.
- Verbs: `create`, `view` (modes: `text|annotated|outline|stats|issues|html|svg|screenshot|forms`), `get`, `query`, `set`, `add`, `remove`, `move`, `swap`, `validate`, `batch`, `help`, `load_skill`. Add `--json` to `get`/`query`/`validate`/`view issues`.
- `view <file> html` -> HTML to **stdout** (return as MCP TextContent).
- `view <file> screenshot` -> PNG; with `-o <path>` writes a file we read and return as a base64 MCP ImageContent. (No `-o -`/`--stdout` convention exists.)
- Files are strictly path-based; no stdin file content. We own a per-file workdir under `$WORK_DIR/<file_id>/`.

## Commands

```bash
# dev run (streamable-HTTP on :8765)
python -m officecli_mcp --transport http --port 8765

# stdio mode (for mcpo)
python -m officecli_mcp --transport stdio

# tests
pytest
```

## Conventions

- Python 3.11+. FastMCP + Starlette/FastAPI for the HTTP surface in one process (shared workdir between `/files` and MCP tools).
- Keep the officecli subprocess layer dumb: build argv, run, capture stdout/stderr/exit code. Intercept only `view html` (-> text) and `view screenshot` (-> base64 image).
- Set MCP ToolAnnotations (readOnlyHint/destructiveHint/openWorldHint) like the canonical filesystem MCP server.
- Never commit the officecli binary or the `/data` `/work` runtime dirs (gitignored).

## Gotcha: MCP /mcp streaming endpoint is cross-container -> "Invalid Host header"

The MCP SDK (mcp 1.28+, `mcp/server/transport_security.py`) enforces a DNS-rebinding guard on the streamable-HTTP `/mcp` endpoint: it returns HTTP 421 `Invalid Host header` when the inbound `Host` isn't allow-listed. `FastMCP.__init__` auto-enables this guard (allow-listing only `127.0.0.1:*`, `localhost:*`, `[::1]:*`) whenever its internal `host` is `127.0.0.1/localhost/::1`. We bind uvicorn `0.0.0.0` so OpenWebUI can reach us across the docker network as `http://officecli-mcp:8765/mcp`, so the incoming `Host: officecli-mcp:8765` must be declared or every client gets 421.

We own this explicitly in `server.py`/`tools.py` (`build_mcp(... host=settings.host, transport_security=TransportSecuritySettings(...))`), driven by:
- `OFFICECLI_MCP_ALLOWED_HOSTS` (comma list; e.g. `officecli-mcp:8765,localhost:8765`; `host:*` = any port). Default = localhost set.
- `OFFICECLI_MCP_DNS_REBINDING_PROTECTION` (`1` default; `0` to disable the guard entirely).

`docker-compose.yml` sets `OFFICECLI_MCP_ALLOWED_HOSTS` to the docker service name. **When you debug an OpenWebUI->officecli-mcp "can't connect" symptom, reproduce with**: `docker exec officecli-mcp curl -s -i -X POST -H "Host: officecli-mcp:8765" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' http://127.0.0.1:8765/mcp` — 200/421 tells you immediately whether the allow-list is the issue.

## Verification

See the spec's "Verification" section. Before claiming a feature works, drive the real flow end-to-end: upload a real .docx via `/files`, call `view_html`, confirm HTML returns; call `view_screenshot`, confirm a base64 PNG returns.
