# officecli-mcp Design

**Date:** 2026-07-15
**Status:** Draft (awaiting user review)
**Author:** xyonium (with Claude)

## Problem

[OfficeCLI](https://github.com/iOfficeAI/OfficeCLI) is a single-binary Office suite for AI agents: it reads/edits/creates `.docx`/`.xlsx`/`.pptx` and renders them back to HTML/PNG, closing the _render -> look -> fix_ loop. It ships a built-in MCP server (`officecli mcp`, stdio, JSON-RPC 2.0).

That built-in MCP server is unusable from **OpenWebUI** because:

1. **Path-only files.** OfficeCLI's MCP exposes one tool, `officecli`, taking a raw `command` string. Files are referenced by **local filesystem path** embedded in that string (e.g. `view /home/u/doc.docx`). There is no base64 input, no stdin file content (stdin is the JSON-RPC channel), no `-o -`/`--stdout` convention.
2. **Remote client, no shared filesystem.** In the target deployment OpenWebUI and the MCP server are separate containers (docker-compose now) / pods (k8s later). The file the user uploads lives in OpenWebUI's storage, unreachable by path from the MCP server's filesystem.
3. **The LLM never holds the bytes.** OpenWebUI does **not** inject file bytes into MCP/OpenAPI tool parameters, and the model cannot supply bytes it does not have. So base64-in-a-tool-param cannot be the LLM's job.

What **already works** natively: OfficeCLI renders `view html` -> HTML to stdout, and `view screenshot` -> PNG (its own MCP auto-injects a temp path and returns a base64 `ImageContent`). So **output back to the LLM is already solved** — the only gap is getting the user's binary file **onto the MCP server's disk** in the first place.

## Goal

A self-contained MCP server, `officecli-mcp`, that:

- Lets a remote OpenWebUI client push an office document in and operate on it via handle-based MCP tools.
- Returns rendered HTML (as MCP text) and screenshots (as base64 images) to the LLM.
- Auto-downloads the `officecli` binary (latest release) on startup; image stays small and decoupled from OfficeCLI version churn.
- Works in both docker-compose and k8s with **no shared volume** — pure HTTP between pods.
- Exposes streamable-HTTP (primary, for OpenWebUI native MCP v0.6.31+) **and** stdio (fallback, for mcpo).

## Non-goals (v1)

- No multi-tenant auth/RBAC beyond a single optional API key on the HTTP surface. (OpenWebUI native MCP handles its own auth at the connection.)
- No persistent document store / knowledge-base sync. Files are ephemeral, TTL-cleaned.
- No re-implementation of OfficeCLI verbs — we shell out to the upstream binary unchanged.
- No editing of the officecli binary or patching of its MCP. We do not use `officecli mcp` at all.

## Architecture

One Python process, two surfaces sharing one workdir:

```
OpenWebUI (pod A)                              officecli-mcp (pod B)
┌────────────────────────────────────┐         ┌──────────────────────────────────────┐
│ User uploads doc.docx in chat      │         │ FastMCP + Starlette, one process      │
│                                    │         │                                       │
│ Native Tool "officecli_upload"     │         │ HTTP surface (same proc):             │
│   __files__ -> [{id,url,name,...}] │  1.HTTP │   POST /files        -> {file_id}     │
│   GET  {OWUI}/api/v1/files/{id}    │ ───────►│   GET  /files/{id}    (download)      │
│        /content  (Bearer key)      │         │   DELETE /files/{id}                  │
│   POST {MCP}/files  (raw bytes)    │         │                                       │
│   -> returns file_id to LLM        │         │ MCP surface (FastMCP):                │
│                                    │         │   streamable-HTTP  (primary)          │
│ LLM calls MCP tools w/ file_id ────┼────────►│   stdio            (fallback/mcpo)    │
│   view_html(file_id)               │         │   tools: create, view_html,           │
│   view_screenshot(file_id, page)   │         │     view_screenshot, view_text,       │
│   edit(file_id, ...), get, set ... │         │     edit, get, set, add, remove,      │
│                                    │         │     validate, batch, get_result_file  │
│ LLM receives: HTML (text) /        │ ◄───────│   runner: resolve file_id -> path,    │
│   PNG (base64 image)               │         │     build argv, exec officecli,       │
└────────────────────────────────────┘         │     intercept html->text, shot->b64   │
                                               │                                       │
                                               │ binary bootstrap: on start, if        │
                                               │   /data/officecli missing/stale,      │
                                               │   fetch latest release asset, chmod+x │
                                               │ workdir: /work/{file_id}/  TTL 1h     │
                                               └──────────────────────────────────────┘
```

**Why one process, two surfaces.** The HTTP `/files` endpoint (where the native tool pushes bytes) and the MCP tools (where the LLM operates) must share the same `file_id` workdir. Co-locating them in one process means a `file_id` from `/files` is immediately usable by MCP tools, with no cross-process state. FastMCP mounts cleanly on a Starlette/FastAPI app, so the HTTP file router and the MCP endpoint coexist on one port.

**Why the native-tool shim.** It is the *only* component in OpenWebUI that can read `__files__` and fetch raw bytes (via `GET /api/v1/files/{id}/content` with a Bearer API key). It is the upload half of the bridge; `officecli-mcp` is the rest. The shim is ~40 lines of Python shipped in `examples/` and pasted into OpenWebUI Workspace > Tools.

## Components

### `binary.py` — officecli bootstrap
- On startup, resolve the asset name for the host platform (`officecli-linux-x64`, `-linux-arm64`, `-alpine-*`, `-mac-*`, `-win-*.exe`).
- Check `/data/officecli` (version-pinned path). If missing, or `OFFICECLI_VERSION` env forces a version and the cached one differs, download from `https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/<asset>` (or `/download/<tag>/<asset>` for a pin), verify it's executable, `chmod +x`.
- Optional `OFFICECLI_SHA256` to verify integrity. Failure to download is fatal (log clearly); a stale cached binary is used as fallback if offline.
- Expose `get_binary() -> Path` and a `version()` helper (`officecli --version`).

### `files.py` — HTTP file store
- `POST /files` — multipart upload (`UploadFile`) **or** JSON `{filename, data_base64}`. Validates extension ∈ {docx,xlsx,pptx}. Writes to `/work/{file_id}/{safe_filename}`, returns `{file_id, filename, size, mime}`. `file_id` = UUID4.
- `GET /files/{id}` — stream the original uploaded bytes back (used by the shim or for debugging).
- `DELETE /files/{id}` — remove the workdir.
- `POST /files/{id}/clone` — optional; returns a new file_id copying the bytes (so edits don't clobber the original).
- Background task: sweep `/work/`, delete dirs whose mtime exceeds `WORK_TTL_SECONDS` (default 3600).
- Optional `API_KEY` env: if set, require `Authorization: Bearer <key>` on `/files` and the MCP endpoint. Default off for trusted in-cluster nets.

### `runner.py` — officecli subprocess layer
- `resolve(file_id) -> Path` to the uploaded file; 404 if unknown/expired.
- `run(argv: list[str], file_id: str, *, capture_bytes=False) -> RunResult(stdout, stderr, exit, out_path)`.
- Always run from the file's workdir as cwd so relative outputs land there. Set `OFFICECLI_NO_AUTO_RESIDENT=1`.
- **Interceptions** (mirroring officecli's own MCP behavior, so output is LLM-friendly):
  - `view ... html` → no `-o`; capture stdout → return as MCP `TextContent`.
  - `view ... screenshot` → if no `-o`, inject `/work/{file_id}/shot.png`; after run, read the PNG, return as MCP `ImageContent` (mimeType `image/png`, base64).
  - `view ... svg` → stdout → `TextContent`.
  - `get`/`query`/`validate`/`view issues` with `--json` → stdout (already JSON) → `TextContent`.
- Captures a nonzero exit as an error result with stderr in the tool response (never raises into the MCP layer).

### `tools.py` — MCP tools (handle-based)
Each tool takes `file_id` (and the verb-specific params), resolves via the runner, and returns MCP content blocks. Tools set `ToolAnnotations` (readOnlyHint/destructiveHint/openWorldHint=false). Initial set:

| Tool | Maps to | Returns |
|---|---|---|
| `create` | `create <file_id>/<name>.<ext> --type ...` | new file_id (text) |
| `view_text` | `view <path> text [--page]` | text |
| `view_html` | `view <path> html` | HTML (text) |
| `view_screenshot` | `view <path> screenshot [--page]` | base64 PNG (image) |
| `view_annotated`/`outline`/`stats`/`issues` | `view <path> <mode> [--json]` | text/json |
| `get` | `get <path> <selector> [--depth --json]` | text/json |
| `set` | `set <path> <selector> --prop ...` | text |
| `add`/`remove`/`move`/`swap` | same verbs | text |
| `edit` | `set <path> /find-replace` (find/replace text) | text |
| `validate` | `validate <path> [--json]` | text/json |
| `batch` | `batch <path> --commands <json>` | text/json |
| `get_result_file` | (read an output file the runner produced, e.g. an exported PDF) | bytes or base64 |

`file_id`-as-handle is the whole point: the LLM passes a short opaque id, never a path or bytes.

### `examples/openwebui_officecli_upload.py` — the upload shim (native OpenWebUI tool)
- Declares `__files__`, a `Valves` with `officecli_mcp_url` and `openwebui_api_key` (+ optional `openwebui_url` defaulting to the in-cluster service).
- For each file in `__files__`: GET `{openwebui_url}/api/v1/files/{id}/content` with `Authorization: Bearer <openwebui_api_key>`, then POST the bytes to `{officecli_mcp_url}/files`.
- Returns a JSON string the LLM can read: `{"file_id": "...", "filename": "...", "hint": "Pass file_id to the officecli MCP tools."}`.
- Pure stdlib `urllib`/`requests` (OpenWebUI tools can use `requests`).

### `config.py` / `__main__.py`
- Env: `TRANSPORT` (http|stdio), `HOST`, `PORT` (8765), `WORK_DIR` (/work), `DATA_DIR` (/data), `WORK_TTL_SECONDS` (3600), `OFFICECLI_VERSION` (latest), `OFFICECLI_SHA256`, `API_KEY`, `MAX_UPLOAD_MB` (50).
- `python -m officecli_mcp --transport http --port 8765` or `--transport stdio`.

## Data flow (end-to-end)

1. User uploads `report.docx` in an OpenWebUI chat; the model has `officecli_upload` (native tool) + the officecli-mcp server (native MCP connection) attached.
2. Model calls `officecli_upload(__files__)`.
3. Shim fetches bytes from `{OWUI}/api/v1/files/{id}/content` (Bearer key) and POSTs them to `{MCP}/files` → gets `file_id`.
4. Shim returns `file_id` to the model.
5. Model calls MCP tool `view_html(file_id)`.
6. `officecli-mcp` resolves `file_id` → `/work/{file_id}/report.docx`, runs `officecli view … html`, captures stdout HTML, returns MCP `TextContent`.
7. Model calls `view_screenshot(file_id, page=1)`; runner injects `-o /work/{file_id}/shot.png`, reads PNG, returns `ImageContent` (base64). Model "sees" the slide.
8. Model calls `edit(file_id, …)` to fix something, then `view_screenshot` again to verify. (render → look → fix loop)
9. After `WORK_TTL_SECONDS` idle, `/work/{file_id}/` is swept.

## OpenWebUI setup checklist (what the admin must do)

This is the answer to "does OpenWebUI's file API need settings?" — yes, a few:

1. **Enable API keys** — Admin Settings or env `ENABLE_API_KEYS=true`. Generate a key in **Account Settings → API Keys** (or admin-generate for a service account). This key is what the upload shim uses to call `GET /api/v1/files/{id}/content`. Keys are scoped to the owning user, so the account must have access to the uploaded files (same user, or admin).
2. **Allow office doc uploads** — ensure `.docx/.xlsx/.pptx` aren't blocked. These are allowed by default; only relevant if `RAG_ALLOWED_FILE_EXTENSIONS` was restricted. No file-size change needed for normal docs.
3. **Install the native tool** — paste `examples/openwebui_officecli_upload.py` into **Workspace → Tools**, set its Valves (`officecli_mcp_url`, `openwebui_api_key`, `openwebui_url`), make it Public (or share with the group), attach it to the model.
4. **Add the MCP connection** — **Settings → Connections → Add MCP server**, URL `http://officecli-mcp:8765/mcp` (streamable-HTTP). OpenWebUI native MCP is streamable-HTTP-only since v0.6.31. (No mcpo needed.) If using the mcpo path instead: add it as an OpenAPI/tool server pointed at the mcpo wrapper.
5. **Network** — OpenWebUI pod must reach the officecli-mcp pod's port (compose service / k8s Service). Both call directions are in-cluster HTTP; CORS is irrelevant for server-side calls.

> The changelog item the user recalled is most likely OpenWebUI's **native MCP support (v0.6.31)** and/or the `__files__`/tool-server file-access work tracked in issue #12228 (files available to native Tools, **not** to external Tool Servers) — which is exactly why a native-tool shim is required rather than direct file injection into MCP params.

## Error handling

- **Unknown/expired file_id** → tool returns a clear text error (`file_id not found or expired`), not a crash.
- **officecli nonzero exit** → return stderr as text content with `isError=true`; include the command that failed (minus secrets).
- **Upload too large** → `413` from `/files`; shim surfaces the message.
- **Binary download failure at startup** → if a cached binary exists, log a warning and continue; else exit non-zero with a clear message (container restarts will retry).
- **Bad extension / corrupt office file** → `/files` rejects at upload (415) or officecli's own error propagates as a tool error.

## Security

- `file_id` is unguessable (UUID4); the workdir is per-file, so one file's edits can't leak into another's path.
- Filenames are sanitized before writing to disk (strip path separators).
- Optional `API_KEY` gates the HTTP surface for non-trusted networks.
- `officecli` runs with the process's privileges only over `/work`; we never pass user input as a shell string — argv list, no `shell=True`.
- The upload shim's `openwebui_api_key` is a secret — stored in the tool's Valves (admin-controlled), documented as such.

## Testing

- **Unit (runner)**: argv construction per verb; html→TextContent and screenshot→ImageContent interception; exit-code/error mapping. Use a fixture `officecli` stub script that emits canned stdout/writes a canned PNG, so tests don't depend on the real binary.
- **Unit (files)**: upload/download/delete; extension validation; TTL sweep with faked mtime; base64 and multipart paths.
- **Unit (binary)**: asset-name resolution per platform; download-or-use-cache logic with a mocked HTTP fetch.
- **Integration**: spin the server in-process, POST a real tiny `.docx` to `/files`, call `view_html` and `view_screenshot` through the MCP client, assert HTML text + base64 PNG. (Requires the real binary — gated behind a `OFFICECLI_BIN` env so CI without it skips.)
- **Shim**: test the `examples/` tool against a mocked OpenWebUI file endpoint + a real `/files`.

## Verification (definition of done)

The feature is "done" when, end-to-end against a real `officecli` binary:
1. `POST /files` with a real `.docx` returns a `file_id`.
2. MCP `view_html(file_id)` returns non-empty HTML.
3. MCP `view_screenshot(file_id, page=1)` returns a decodable base64 PNG (write to disk, confirm it opens as an image).
4. MCP `edit(file_id, …)` then `view_text` reflects the change.
5. `DELETE /files/{id}` then `view_html(file_id)` returns the "expired" error.
All five driven by an automated integration test, not just manual curls.

## Deployment artifacts

- `Dockerfile` — slim python:3.11-slim base, copy `src/`, install deps, `ENTRYPOINT ["python","-m","officecli_mcp"]`. Binary fetched at runtime (not baked) per the chosen strategy; `/data` and `/work` are volumes.
- `docker-compose.yml` — service `officecli-mcp` (ports 8765, volumes for /data /work), plus an example OpenWebUI service snippet showing the network relationship.
- `pyproject.toml` — deps: `mcp[cli]` (FastMCP), `starlette`/`uvicorn`, `httpx` (for bootstrap download), `pydantic`. Dev: `pytest`, `pytest-asyncio`.

## Alternatives considered (rejected)

- **Reuse `officecli mcp` + mcpo + shared volume.** Rejected: UUID-prefixed filenames mean the LLM still can't know the path (shim needed anyway), k8s needs RWX volumes, breaks entirely under S3/GCS storage. Strictly worse for the stated compose→k8s migration.
- **Base64 inline as the LLM's tool param.** Rejected as primary: the LLM has no bytes to fill it, and multi-MB base64 in context is token-prohibitive. Kept only as an *optional* `/files` upload format for tiny files / restricted nets.
- **Code-interpreter (Pyodide) pushing bytes.** Rejected: Python-only, no native libs (can't even read .docx), CORS-blocked to sibling containers, model must hand-write fragile upload code.

## Open questions for review

1. Is the initial tool set right, or should we expose a single generic `officecli(command, file_id)` passthrough tool (closer to upstream's single-tool model) instead of many typed tools? Trade-off: typed tools are safer/clearer for the LLM; passthrough is less code and always covers every verb. **Recommendation: typed tools for v1; add a raw passthrough later if gaps appear.**
2. Default `WORK_TTL_SECONDS` = 3600 acceptable, or should files live for the whole chat session (which we can't easily detect from the server)?
3. `MAX_UPLOAD_MB` = 50 — ok, or match OfficeCLI's own limits?
