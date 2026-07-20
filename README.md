# officecli-mcp

An [MCP](https://modelcontextprotocol.io/) server that wraps [OfficeCLI](https://github.com/iOfficeAI/OfficeCLI) so it can be used from **OpenWebUI** (and any streamable-HTTP MCP client). It solves OfficeCLI's core limitation for remote clients: OfficeCLI's built-in MCP mode only accepts **local file paths**, which doesn't work when the LLM client runs in a different container/pod and never holds the file bytes.

`officecli-mcp` adds a **handle-based file layer**: an HTTP upload endpoint accepts office documents and returns a `file_id`; MCP tools then operate on that `file_id`. OfficeCLI's built-in rendering returns HTML (as text) and screenshots (as base64 images) directly to the LLM, closing the _render → look → fix_ loop.

## Quick start (3 steps)

You need three things talking to each other: the **officecli-mcp** container, the **`officecli_file` native tool** auto-synced into OpenWebUI, and the model attached to it. Do them in order:

**1. Run the server.**

```bash
docker compose up -d   # http://localhost:8765, auto-pulls officecli on first start
```

The one env you MUST set when OpenWebUI is in a separate container is `OFFICECLI_MCP_ALLOWED_HOSTS` - the MCP SDK's DNS-rebinding guard returns **421 "Invalid Host header"** to any `Host` it isn't told about. The compose file already sets it to `officecli-mcp:8765,localhost:8765,127.0.0.1:8765`; if your service name or port differs, edit it (or set `OFFICECLI_MCP_DNS_REBINDING_PROTECTION=0` to disable the guard).

**2. Set the auto-sync env vars (or paste the tool manually).**

Add these env vars to your docker-compose or env file (the compose file has commented placeholders):

| Var | Example | What it is |
|---|---|---|
| `OFFICECLI_MCP_OWUI_URL` | `http://open-webui:8080` | OpenWebUI internal base (for self-sync) |
| `OFFICECLI_MCP_OWUI_API_KEY` | `sk-...` | OpenWebUI **admin** API key (for self-sync -- keep secret, use an env file or secrets, never commit) |

The server auto-creates/updates the `officecli_file` native tool in OpenWebUI on boot when both vars are set. If you'd rather paste it manually, use [`examples/openwebui_officecli_file.py`](examples/openwebui_officecli_file.py) in Workspace -> Tools (set `owui_sync=0` to disable auto-sync).

**3. In OpenWebUI: attach the tool and chat.**

Find the auto-created `officecli_file` tool in Workspace -> Tools -> make it **Public** -> attach it to a model. No MCP connection is required -- the tool drives officecli-mcp over plain HTTP (`/tools/call`). The MCP streamable-HTTP endpoint (`/mcp`) stays available for debugging but is no longer the primary path.

In a chat with that model: attach a `.docx`/`.xlsx`/`.pptx` and ask it to edit; the model calls `officecli_file(action="upload")` to get a `file_id`, edits via the `officecli_*` tools over HTTP, then `officecli_file(action="download")` to hand back a downloadable file chip.

### Admin API key

The `OFFICECLI_MCP_OWUI_API_KEY` is sensitive -- it grants tool create/update access to your OpenWebUI. **Use an env file or a secrets manager; never commit it to version control.** The compose file has the var commented out for safety.

---

## How it works

```
OpenWebUI (pod A)                         officecli-mcp (pod B)
┌──────────────────────────────┐          ┌─────────────────────────────────────────────────┐
│ LLM ──► officecli_file       │  HTTP    │ HTTP /tools/call (dispatch)     │
│         (native tool) ───────┼─────────►  POST {"name","arguments"}     │
│                                │          │   → same handlers as /mcp     │
│ Native Tool "officecli_file"  │          │                                 │
│   reads __files__, fetches    │  HTTP    │ HTTP /files  (upload → file_id)│
│   bytes, POSTs ──────────────┼─────────► /files/{id} (download)        │
│   returns file_id to LLM      │          │                                 │
│                                │          │                                 │
│ LLM ──► MCP client            │          │ FastMCP (streamable-HTTP)       │
│         (debugging only) ──────┼─────────►  tools: create, view_html…    │
└──────────────────────────────┘          │ officecli binary (auto-pulled) │
                                          └─────────────────────────────────────────────────┘
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
| `OFFICECLI_MCP_SCREENSHOT_MAX_EDGE` | 1024 | screenshot downscale longest edge (px); 0=off |
| `OFFICECLI_MCP_OWUI_SYNC` | 1 | push the officecli_file tool into OpenWebUI on boot |
| `OFFICECLI_MCP_OWUI_URL` | "" | OpenWebUI internal base (for self-sync) |
| `OFFICECLI_MCP_OWUI_API_KEY` | "" | OpenWebUI **admin** API key (for self-sync; keep secret) |
| `OFFICECLI_MCP_OWUI_TOOL_ID` | `officecli_file` | tool id to create/update |

## `officecli_file` actions

The native tool (installed in Quick start step 3) takes these `action` values:

- `action="upload"` - push chat-attached office docs into officecli-mcp; returns a `file_id` to pass to the `officecli_*` MCP tools.
- `action="download"` - pull a finished doc back out into OpenWebUI storage and return a browser-reachable download URL. Also emits a `files` event so OpenWebUI shows a **downloadable file chip** on the assistant message (no need to copy the URL out of the tool call).
- `action="stage"` - drop a generated/uploaded image or CSV into a document's workdir; returns an asset filename for `officecli_add type=picture` (`src=<asset>`) or `officecli_import` (`source=<asset>`). Pictures must be staged first - officecli's SSRF guard blocks passing a URL as `src=`.
- `action="run"` - call ANY document tool by name: pass `tool=<name>` and `arguments=<JSON object as a STRING>`. The action dispatches to the same FastMCP handlers as the `/mcp` endpoint, so validation and error handling are identical.
- `action="tools"` - fetch the live tool manifest from `/tools`. Use this as a fallback if the pasted shim file is stale; returns the current tool list, signatures, and descriptions so the model can construct correct calls.

## License

Apache-2.0 (same as OfficeCLI).
