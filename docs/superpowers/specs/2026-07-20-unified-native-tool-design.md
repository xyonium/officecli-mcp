# Unified native tool: one OpenWebUI tool for all of officecli

**Date:** 2026-07-20
**Status:** Approved (design), pending implementation plan
**Supersedes/extends:** 2026-07-18-merged-officecli-file-tool-design.md (file actions stay; adds generic exec + self-sync)

## Problem

Using officecli from OpenWebUI today requires enabling **two** things:

1. An **MCP connection** to officecli-mcp (`/mcp`) — exposes the `officecli_*` document tools.
2. The **`officecli_file` native Workspace Tool** — moves file bytes in/out (OpenWebUI never injects file bytes into MCP tool params, so this cannot be folded into MCP).

We want **one** feature: the native tool alone covers everything by HTTP-calling the officecli-mcp container. After this, the MCP connection becomes optional (kept for debugging / advanced users).

**Hard requirement:** adding/removing tools or changing tool descriptions/schemas server-side must **not** require editing the native tool's Python code. The single source of truth for tool definitions is `src/officecli_mcp/tools.py`.

**Discovery requirement:** the model must be able to *see* which tools exist without first calling a discovery endpoint — otherwise it never knows to call it.

## Design overview

Three pieces:

1. **Server: generic exec + manifest endpoints** — `GET /tools` (manifest) and `POST /tools/call` (generic dispatch) on the existing FastMCP app, next to `/files`. Tool definitions stay only in `tools.py`.
2. **Shim: static signature, generic `run`** — `officecli_file` gains `action="run"` (forward any tool call) and `action="tools"` (fetch manifest at runtime, as a fallback). Its docstring embeds the **full** tool manifest — every tool's complete description (the same docstring text the MCP server serves) plus a compact argument schema — so the model sees everything statically and never needs the fallback round trip.
3. **Server: self-sync of the shim into OpenWebUI** — on startup the container regenerates the shim source (template + current manifest embedded) and pushes it to OpenWebUI's Tools API (`POST /api/v1/tools/id/{id}/update`, or `/create` if missing) using an admin API key. The embedded manifest is therefore always fresh; the model always sees the current tool list. No key configured → sync skipped, manual paste still works.

```
OpenWebUI model
   │  calls officecli_file(action=..., ...)          (one native tool)
   ▼
shim (examples/openwebui_officecli_file.py, auto-synced)
   │  upload/download/stage ──► POST /files, /files/stage, GET /files/{id}
   │  run(tool, arguments)  ──► POST /tools/call {name, arguments}
   │  tools                 ──► GET  /tools
   ▼
officecli-mcp container
   tools.py (single source of truth) ── runner.py ── officecli binary
   on boot: GET /tools → render shim → OpenWebUI /api/v1/tools/id/... (admin key)
```

## 1. Server: `/tools` and `/tools/call`

Added in `server.py` as `mcp.custom_route`s, sharing the process/workdir with `/files`.

### `GET /tools`

Returns the manifest the shim embeds and the model can fetch at runtime:

```json
{
  "instructions": "<FastMCP instructions string from build_mcp>",
  "revision": "<sha1 of canonical tool list>",
  "tools": [
    {"name": "officecli_view_text", "description": "...", "inputSchema": {...}, "readOnly": true},
    ...
  ]
}
```

- Derived from the FastMCP instance (`build_mcp`) so it can never drift from the real tools. FastMCP's own `list_tools` machinery produces name/description/inputSchema from the same decorators that serve MCP.
- `revision` = sha1 over the sorted `(name, description, json.dumps(inputSchema))` tuples. Used for the shim version stamp (§3).

### `POST /tools/call`

Request: `{"name": "officecli_set", "arguments": {"file_id": "...", "selector": "...", "prop": [...]}}`

Response (MCP-content-shaped, JSON-serializable):

```json
{"content": [{"type": "text", "text": "..."}], "isError": false}
```

- Dispatches to the same registered tool functions the MCP endpoint uses (FastMCP `call_tool`), so behavior is identical to the MCP path. Validation errors / `ToolError` come back as `isError: true` with the message in a text content block; unknown tool names → 404.
- **Auth:** same `OFFICECLI_MCP_API_KEY` Bearer check as `/files` (`_check_api_key`). Empty key = open (current behavior on the docker network).

### `view_screenshot` over `/tools/call`

MCP's `officecli_view_screenshot` returns an `Image` content block, which JSON can carry as `{"type": "image", "data": "<base64>", "mimeType": "image/png"}` — the shim turns that into a data URL string for the model (§2). To bound model-context tokens, the server **downscales the PNG before base64-encoding**:

- New ENV `OFFICECLI_MCP_SCREENSHOT_MAX_EDGE` (int, default `1024`). Longest edge is clamped to this many px (aspect preserved); `0` disables resizing.
- Applied inside the `officecli_view_screenshot` tool itself (so MCP clients benefit too) via Pillow (`Image.open` → `thumbnail` → re-encode PNG). Pillow is added as a dependency.
- Config: `config.py` gains `screenshot_max_edge: int = _env_int("OFFICECLI_MCP_SCREENSHOT_MAX_EDGE", 1024)`; `server.py` passes it into `build_mcp(...)` like `view_html_mode` (with `getattr` default so test fakes keep working).

## 2. Shim: new actions

`examples/openwebui_officecli_file.py` keeps its existing actions and gains two. Signature stays fixed — **tool evolution never touches this file** (it's regenerated by the server, §3).

| action | Purpose | Params (beyond existing) |
|---|---|---|
| `upload` | (unchanged) push `__files__` bytes → `/files` → file_id | — |
| `download` | (unchanged) file_id → bytes → OpenWebUI storage → chip via `__event_emitter__` | — |
| `stage` | (unchanged) asset bytes → `/files/stage` → asset name | — |
| `run` | forward a document tool call | `tool: str`, `arguments: str` (JSON object as string) |
| `tools` | fetch live manifest from `GET /tools` (fallback/debug) | — |

`arguments` is a **JSON string** (not a dict): OpenWebUI builds the tool schema from the Python signature, and a free-form JSON object parameter is most reliably expressed as a string param the shim parses with `json.loads`. The docstring states this explicitly with an example.

`run` mechanics:

1. `json.loads(arguments)` → `POST {officecli_mcp_url}/tools/call {"name": tool, "arguments": ...}` (via `anyio.to_thread.run_sync`, like all blocking calls in this file).
2. Response content blocks are flattened to a single JSON string for the model:
   - `text` blocks → concatenated text.
   - `image` blocks → converted to `{"image": "data:image/png;base64,...", "note": "rendered screenshot"}` entries in the returned JSON so the model receives the base64 inline (server already downscaled it; §1). If OpenWebUI/the model chokes on large inline base64 in practice, the documented fallback is to instead POST the PNG to OpenWebUI storage and emit a files-chip (same mechanism as `download`) — decided at implementation verification time.
3. `isError: true` → the error text is returned verbatim so the model can self-correct (same information the MCP path gives today).

### Docstring = discovery surface

The module docstring (what the model sees as the tool description) contains:

1. The workflow guidance (today's FastMCP `instructions`, which the server already maintains as the single source of truth for cross-tool guidance).
2. **The complete embedded manifest**: for each tool, its **full description text** (verbatim from the tool's docstring — all the guidance we already maintain: batch schema, prop list-vs-map, sizing, SSRF rules, etc.) plus a **compact argument schema** (e.g. `officecli_set(file_id: str, selector: str, prop?: list[str])`). Generated from `GET /tools` at sync time.
3. A pointer: "call tools via action=\"run\"; this manifest is auto-synced from the server".

Because the sync pushes the **full** descriptions, the model never needs `action="tools"` for understanding — that action remains only as a fallback (e.g. sync disabled / manual paste gone stale). Current size is small enough for this to be free: ~5.4K chars of docstrings today, ~3K tokens total with schemas. `action="tools"` returns the same full manifest JSON (with complete `inputSchema`) for cases where structured introspection is wanted.

## 3. Server: shim self-sync into OpenWebUI

New module `src/officecli_mcp/shim_sync.py`, invoked from `build_app` (best-effort, after routes are up — never blocks startup; failures log a warning).

### Shim generation

- The shim source is a **template** (`src/officecli_mcp/shim_template.py`, also copied to `examples/openwebui_officecli_file.py` for manual installs) with a placeholder block where the full manifest + revision stamp go:
  - First line of the file: `# officecli-shim-rev: <revision>` where `<revision>` is the manifest sha1 from §1.
  - The docstring's manifest section is generated from the current tool list (full descriptions + compact schemas, §2).
- Because generation is deterministic, comparing revisions = comparing embedded stamps.

### Sync flow (on container start, when configured)

1. If `OFFICECLI_MCP_OWUI_SYNC=0` or no API key → skip entirely (log once). Default is on-when-key-present.
2. `GET {owui}/api/v1/tools/id/{tool_id}` with `Authorization: Bearer <admin key>`.
   - **404** → `POST /api/v1/tools/create` with `ToolForm{id, name, content, meta:{description}, access_grants: null}`. Creates the tool **private** to the admin user; the README tells the admin to flip it to Public in the UI (same manual step as today — we deliberately don't hardcode an access model).
   - **200** → extract `# officecli-shim-rev:` from the stored content; if != current revision → `POST /api/v1/tools/id/{tool_id}/update` with the same `ToolForm` shape (preserving the tool's existing `access_grants` read from the GET response so a Public tool stays Public).
   - Equal revision → no-op.
3. Sync updates `content` and `meta.description` only; user-set name/visibility are untouched (id-keyed).

Verified against the OpenWebUI container source (`routers/tools.py`): create requires admin or `workspace.tools` permission, id must be a valid identifier (lowercased), update re-loads the module and regenerates specs immediately, and content edits by admins are permitted. An admin API key (`sk-...`) via `Authorization: Bearer` satisfies both.

### Config (`config.py`)

| ENV | Default | Meaning |
|---|---|---|
| `OFFICECLI_MCP_OWUI_SYNC` | `1` | `0` disables self-sync (manual paste mode). |
| `OFFICECLI_MCP_OWUI_URL` | `""` | Internal OpenWebUI base, e.g. `http://open-webui:8080`. Required for sync. |
| `OFFICECLI_MCP_OWUI_API_KEY` | `""` | OpenWebUI **admin** API key. Required for sync. Sensitive — document as secret. |
| `OFFICECLI_MCP_OWUI_TOOL_ID` | `officecli_file` | Tool id to create/update (valid identifier). |
| `OFFICECLI_MCP_SCREENSHOT_MAX_EDGE` | `1024` | Screenshot downscale, longest edge px; `0` = off. |

`docker-compose.yml` gains these (commented placeholders), and `README` documents the one-time step of creating an admin API key (Settings > Account) and putting it in the environment — after which shim updates are fully automatic.

## 4. What happens to the MCP connection

Nothing is removed: `/mcp` stays up with the same tools. Users who only install the native tool get full functionality; the MCP connection becomes an optional second client (useful for `mcpo`/stdio debugging). README's quick start is rewritten around the single-tool setup.

## Error handling

- `/tools/call` unknown tool → 404 `{"error": "unknown tool '...'"}`; shim surfaces it verbatim so the model picks a different tool.
- Tool raising `ToolError` (e.g. expired file_id, officecli exit != 0) → `isError: true` + message; shim returns the message string unchanged (model self-corrects as today).
- Sync failures (bad key, OpenWebUI unreachable, non-2xx) → log warning, server continues; the shim in OpenWebUI is simply stale (embedded manifest may lag, but `action="tools"`/`action="run"` still work against the live server).
- Screenshot resize failure (corrupt PNG) → log and return the original image unresized rather than failing the call.
- Shim `run` with invalid `arguments` JSON → returned immediately as `{"error": "arguments must be a JSON object string: ..."}` without an HTTP round trip.

## Testing

Following repo convention (unit with fakes + real-binary e2e, `ruff check .` + full pytest before push):

- **`/tools` manifest**: names/schemas match the FastMCP-registered tools; revision changes when a description changes; includes instructions.
- **`/tools/call`**: dispatches to a fake runner (create/set/view_text happy path); unknown tool → 404; ToolError → `isError: true` message; api-key gate honored.
- **Screenshot resize**: fake PNG larger than max edge → returned image ≤ max edge, aspect preserved; `0` disables; non-PNG bytes pass through with a logged warning.
- **Shim sync**: fake OpenWebUI HTTP (existing tests already monkeypatch HTTP in shim tests — same pattern): 404 → create called with valid ToolForm; stale rev → update preserving access_grants; equal rev → no call; sync disabled / no key → no HTTP at all; non-2xx → warning, no exception.
- **Shim `run`/`tools` actions**: monkeypatched `requests` — JSON-string arguments parsed, text blocks flattened, image block → data URL entry, error propagation; invalid JSON short-circuits.
- **E2E (real binary, `test_e2e_real.py`)**: boot app, `GET /tools` lists `officecli_create`; `POST /tools/call` create → view_text round trip; screenshot call returns image block whose decoded PNG dimensions respect max edge.
- **Template ↔ example parity**: a test renders the template with a fake manifest and asserts `examples/openwebui_officecli_file.py` is byte-identical to the generated output (prevents the two drifting).

## Out of scope (YAGNI)

- Auto-making the tool Public via access_grants (admin flips it in the UI; one-time).
- Watching tool changes without container restart (revision check happens at boot; restart on deploy is the norm).
- Folding upload/download/stage into `run` (they need OpenWebUI credentials + `__files__`/`__event_emitter__`, which only the native shim has).
- Removing the MCP endpoint.
