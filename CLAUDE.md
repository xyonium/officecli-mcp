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
  config.py            # env-based settings (ports, workdir, ttl, api key, officecli version pin)
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

## Verification

See the spec's "Verification" section. Before claiming a feature works, drive the real flow end-to-end: upload a real .docx via `/files`, call `view_html`, confirm HTML returns; call `view_screenshot`, confirm a base64 PNG returns.
