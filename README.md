# officecli-mcp

An [MCP](https://modelcontextprotocol.io/) server that wraps [OfficeCLI](https://github.com/iOfficeAI/OfficeCLI) so it can be used from **OpenWebUI** (and any streamable-HTTP MCP client). It solves OfficeCLI's core limitation for remote clients: OfficeCLI's built-in MCP mode only accepts **local file paths**, which doesn't work when the LLM client runs in a different container/pod and never holds the file bytes.

`officecli-mcp` adds a **handle-based file layer**: an HTTP upload endpoint accepts office documents and returns a `file_id`; MCP tools then operate on that `file_id`. OfficeCLI's built-in rendering returns HTML (as text) and screenshots (as base64 images) directly to the LLM, closing the _render → look → fix_ loop.

## How it works

```
OpenWebUI (pod A)                         officecli-mcp (pod B)
┌──────────────────────────────┐          ┌─────────────────────────────────┐
│ LLM ──► native MCP client    │  HTTP    │ FastMCP (streamable-HTTP)       │
│         (streamable-HTTP) ────┼──────────►  tools: create, view_html,     │
│                                │          │         view_screenshot, edit… │
│ Native Tool "officecli_file"  │          │                                 │
│   reads __files__, fetches    │  HTTP    │ HTTP /files  (upload → file_id)│
│   bytes, POSTs ───────────────┼──────────► /files/{id} (download)        │
│   returns file_id to LLM      │          │                                 │
└──────────────────────────────┘          │ officecli binary (auto-pulled) │
                                          └─────────────────────────────────┘
```

- The LLM never sees raw bytes — only a short `file_id` handle.
- Bytes move server-to-server (OpenWebUI REST → our `/files`), never through the model context.
- `officecli` is downloaded on first start (latest release for the host platform); the image stays small and decoupled from OfficeCLI version churn.

## Transport

- **Primary**: streamable-HTTP (OpenWebUI native MCP, v0.6.31+, is streamable-HTTP-only — connect directly, no mcpo needed).
- **Fallback**: stdio (wrap with [mcpo](https://github.com/open-webui/mcpo) for OpenAPI/OpenWebUI if needed).

## Runtime dependency: .NET globalization

`officecli` is a self-contained .NET app. On a slim image without `libicu` it fails fast (`Couldn't find a valid ICU package`). Rather than bundle ICU, we run .NET in **globalization-invariant mode** (`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`, set in the `Dockerfile` and injected by the runner subprocess env). This only affects locale-aware culture data (dates/numbers use invariant culture) and is fine for office document manipulation. If you ever need full locale support, `apt-get install libicu` in your image and unset that variable.

## Status

✅ Implemented and verified end-to-end against the real `officecli` binary (v1.0.136). See [`docs/`](docs/) for the design spec and implementation plan.

## Quick start (Docker)

```bash
docker compose up -d   # serves http://localhost:8765 (auto-pulls officecli on first start)
```

OpenWebUI: add an MCP connection at `http://officecli-mcp:8765/mcp` (native MCP, streamable-HTTP), and install the `officecli_file` native tool from [`examples/openwebui_officecli_file.py`](examples/openwebui_officecli_file.py) with its Valves set (`officecli_mcp_url`, `openwebui_url`, `openwebui_browser_url`).

## Local dev

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest                      # unit tests (no binary needed)
officecli-mcp --transport http --port 8765
```

E2E against the real binary:

```bash
curl -L https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/officecli-linux-x64 -o /tmp/officecli && chmod +x /tmp/officecli
OFFICECLI_BIN=/tmp/officecli python3 -m pytest tests/test_e2e_real.py -v
```

## Tools

All MCP tools are prefixed `officecli_` and take a `file_id` handle (returned by `POST /files` or the `officecli_file` tool (action="upload")):

| Tool | Purpose |
|---|---|
| `officecli_create` | create a blank doc/xlsx/pptx -> new file_id |
| `officecli_view_html` | render to HTML (returned as text) |
| `officecli_view_screenshot` | render a page to PNG (base64 image) |
| `officecli_view_text` / `_annotated` / `_outline` / `_stats` / `_issues` | various text views |
| `officecli_get` / `_set` / `_add` / `_remove` / `_move` / `_swap` / `_edit` | DOM edits (add supports `prop` list for pictures: `["src=<asset>","width=200"]`) |
| `officecli_import` | CSV/TSV -> Excel via staged `source` filename |
| `officecli_validate` | OpenXML schema validation |
| `officecli_batch` | multi-command batch |
| `officecli_file(action="stage")` | drop an image/CSV into a doc's workdir (returns `asset` name for `src=` or `source=` in other tools) |

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `OFFICECLI_MCP_TRANSPORT` | http | http (streamable-HTTP) or stdio |
| `OFFICECLI_MCP_PORT` | 8765 | HTTP port |
| `OFFICECLI_MCP_DATA_DIR` | /data | where the officecli binary lives |
| `OFFICECLI_MCP_WORK_DIR` | /work | per-file_id workdirs |
| `OFFICECLI_MCP_WORK_TTL_SECONDS` | 172800 (48h) | idle workdir cleanup (doc + staged assets); swept lazily on each upload/stage, mtime refreshed on read |
| `OFFICECLI_MCP_VIEW_HTML_MODE` | 2 (compact) | `officecli_view_html` output: `0`=disabled (error, use screenshot/annotated), `1`=full HTML, `2`=compact (strip styles/scripts, base64 images -> `[IMG]`, keep text structure), `3`=truncate to `VIEW_HTML_MAX_CHARS`. Compact is the default because officecli's full HTML is a large interactive page that blows the model context |
| `OFFICECLI_MCP_VIEW_HTML_MAX_CHARS` | 8000 | truncation limit when `VIEW_HTML_MODE=3` |
| `OFFICECLI_MCP_MAX_UPLOAD_MB` | 50 | upload size cap |
| `OFFICECLI_VERSION` | latest | pin a release tag |
| `OFFICECLI_SHA256` | (none) | verify binary integrity |
| `OFFICECLI_MCP_API_KEY` | (none) | if set, require Bearer on HTTP surface |
| `OFFICECLI_MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,[::1]:*` | comma-separated `Host` headers the `/mcp` endpoint is reachable by (use `host:*` for any port). OpenWebUI calls `http://officecli-mcp:8765/mcp` across the docker network, so the docker service name must be listed or clients get 421 `Invalid Host header`. The compose file sets this to `officecli-mcp:8765,localhost:8765,127.0.0.1:8765`. |
| `OFFICECLI_MCP_DNS_REBINDING_PROTECTION` | 1 | the MCP SDK DNS-rebinding / Host-header guard; set `0` to disable it entirely |

## OpenWebUI setup

1. Keep API keys enabled (`ENABLE_API_KEYS=true`, the default). The upload shim does **not** need a stored key - it forwards the current user's credentials via the injected `__request__`, so it works as a shared Public tool in multi-user setups (each user fetches only their own files).
2. Install the native tool [`examples/openwebui_officecli_file.py`](examples/openwebui_officecli_file.py) (Workspace > Tools); set Valves (`officecli_mcp_url`, `openwebui_url`, `openwebui_browser_url=https://ai.savorcare.com`); make it Public; attach to the model. Use `action="upload"` to get a `file_id` from attached files, `action="download"` to get a browser-reachable download link for a finished file (it also emits a `files` event so OpenWebUI shows a downloadable file chip on the assistant message - no need to copy the URL out of the tool call), or `action="stage"` to drop a generated/uploaded image or CSV into a document's workdir (returns an asset filename for `officecli_add type=picture` or `officecli_import`).
3. Add MCP connection: `http://officecli-mcp:8765/mcp` (Settings > Connections).
4. Ensure the OpenWebUI pod can reach the officecli-mcp pod.

## License

Apache-2.0 (same as OfficeCLI).
