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
│ Native Tool "officecli_upload"│          │                                 │
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

## Status

✅ Implemented and verified end-to-end against the real `officecli` binary (v1.0.136). See [`docs/`](docs/) for the design spec and implementation plan.

## Quick start (Docker)

```bash
docker compose up -d   # serves http://localhost:8765 (auto-pulls officecli on first start)
```

OpenWebUI: add an MCP connection at `http://officecli-mcp:8765/mcp` (native MCP, streamable-HTTP), and install the `officecli_upload` native tool from [`examples/openwebui_officecli_upload.py`](examples/openwebui_officecli_upload.py) with its Valves set.

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

All MCP tools are prefixed `officecli_` and take a `file_id` handle (returned by `POST /files` or the `officecli_upload` tool):

| Tool | Purpose |
|---|---|
| `officecli_create` | create a blank doc/xlsx/pptx -> new file_id |
| `officecli_view_html` | render to HTML (returned as text) |
| `officecli_view_screenshot` | render a page to PNG (base64 image) |
| `officecli_view_text` / `_annotated` / `_outline` / `_stats` / `_issues` | various text views |
| `officecli_get` / `_set` / `_add` / `_remove` / `_move` / `_swap` / `_edit` | DOM edits |
| `officecli_validate` | OpenXML schema validation |
| `officecli_batch` | multi-command batch |

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `OFFICECLI_MCP_TRANSPORT` | http | http (streamable-HTTP) or stdio |
| `OFFICECLI_MCP_PORT` | 8765 | HTTP port |
| `OFFICECLI_MCP_DATA_DIR` | /data | where the officecli binary lives |
| `OFFICECLI_MCP_WORK_DIR` | /work | per-file_id workdirs |
| `OFFICECLI_MCP_WORK_TTL_SECONDS` | 3600 | idle file cleanup |
| `OFFICECLI_MCP_MAX_UPLOAD_MB` | 50 | upload size cap |
| `OFFICECLI_VERSION` | latest | pin a release tag |
| `OFFICECLI_SHA256` | (none) | verify binary integrity |
| `OFFICECLI_MCP_API_KEY` | (none) | if set, require Bearer on HTTP surface |

## OpenWebUI setup

1. Enable API keys (`ENABLE_API_KEYS=true`); generate one in Account Settings > API Keys.
2. Install the native tool [`examples/openwebui_officecli_upload.py`](examples/openwebui_officecli_upload.py) (Workspace > Tools); set Valves (`officecli_mcp_url`, `openwebui_url`, `openwebui_api_key`); make it Public; attach to the model.
3. Add MCP connection: `http://officecli-mcp:8765/mcp` (Settings > Connections).
4. Ensure the OpenWebUI pod can reach the officecli-mcp pod.

## License

Apache-2.0 (same as OfficeCLI).
