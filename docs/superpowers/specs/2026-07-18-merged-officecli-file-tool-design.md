# Design: merged `officecli_file` tool (upload + download in one)

**Date:** 2026-07-18
**Status:** Approved (pending spec review)
**Goal:** Reduce the OpenWebUI tool count by merging the existing `officecli_upload`
native tool with a new download capability into a single `officecli_file(action, ...)`
method, and make generated office documents downloadable by the user from inside the chat.

## Motivation

Today there is one OpenWebUI native tool, `officecli_upload`, that pushes chat-attached
docs into officecli-mcp and returns a `file_id`. There is no inverse: once the model
creates/edits a document with the `officecli_*` MCP tools, the user has no in-chat way to
get the file back (the model never holds bytes by design, and OpenWebUI native tools cannot
return file attachments natively - confirmed via research, see References). The user must
`curl` the officecli-mcp `/files/{id}` endpoint directly.

Two problems to solve together:
1. Add download capability (currently missing).
2. Do it without growing the tool list - merge upload + download into one tool, controlled
   by an `action` parameter. The user explicitly wants fewer tool entries.

## Constraint that drives the design

officecli-mcp runs on the **internal docker network only** and is not reachable from the
user's browser. Therefore a download action cannot simply return an `http://officecli-mcp:...`
link - the browser could not load it. OpenWebUI itself (`https://ai.savorcare.com` in this
deployment) IS browser-reachable. So the download action pushes the file bytes into
OpenWebUI's own file storage and returns an OpenWebUI URL the browser can load.

Verified against the running `open-webui` container: `POST /api/v1/files/` and
`POST /api/v1/files/?process=false` both return **401** without auth (endpoint exists and
requires a Bearer token), not 404. `process=false` skips RAG ingestion of the binary.

## Tool surface

One `Tools` class, one method:

```python
def officecli_file(
    self,
    action: str,                 # "upload" | "download"
    __files__: list[dict] = [],  # upload: OpenWebUI-injected attached-file dicts
    __request__: Any = None,     # both: carries the current user's Bearer token / cookie
    file_id: str = "",           # download: the officecli-mcp file_id to fetch
    filename: str = "",          # download (optional): override the saved filename
) -> str:                        # always returns a JSON string
```

- **`action="upload"`** - identical behavior to the current `officecli_upload` shim:
  for each entry in `__files__`, fetch its bytes from OpenWebUI via the user's forwarded
  credentials (`GET {openwebui_url}/api/v1/files/{id}/content`), POST them to
  `{officecli_mcp_url}/files`, and collect `file_id`s. Returns
  `{"files":[{"file_id":...,"filename":...}], "hint":"Pass each file_id to officecli_* MCP tools ..."}`.
- **`action="download"`** - `GET {officecli_mcp_url}/files/{file_id}` (bytes + filename
  from the `content-disposition` header), then `POST {openwebui_url}/api/v1/files/?process=false`
  with the multipart file body and the user's forwarded Bearer token, receive
  `{"id": "<owui_file_id>"}`, and return
  `{"url": "{openwebui_browser_url}/api/v1/files/{owui_file_id}/content", "filename":..., "size":...}`.
  The model renders `url` as a markdown download link.

Net effect on the tool list: **2 entries -> 1 entry** (`officecli_upload` replaced by
`officecli_file`). The `officecli_*` MCP tools are unchanged.

## Valves

```python
officecli_mcp_url      = "http://officecli-mcp:8765"  # internal: tool -> officecli-mcp
openwebui_url          = "http://open-webui:8080"     # internal: tool -> OWUI storage API
openwebui_browser_url  = "https://ai.savorcare.com"   # browser-reachable OWUI base; used in returned download URLs
```

- `officecli_mcp_url` and `openwebui_url` are the internal docker addresses the tool uses
  to make HTTP calls (same as today's upload shim; `openwebui_url` is what `GET /content`
  and `POST /files` hit from inside the cluster).
- `openwebui_browser_url` is new and is the value prepended to the download URL returned to
  the user, because the user's browser reaches OWUI at a different (public) address than the
  tool does internally. Default empty -> falls back to `openwebui_url` (only correct when
  browser and tool share an address). For this deployment it is set to
  `https://ai.savorcare.com`.

## Files & structure

- **`examples/openwebui_officecli_upload.py`** is replaced by
  **`examples/openwebui_officecli_file.py`**: one `Tools` class, one `officecli_file`
  method, and swappable HTTP helpers (`_owui_get`, `_owui_post`, `_mcp_get`, `_mcp_post`)
  monkeypatched in tests, mirroring the existing pattern. The old file is removed (no
  deprecation stub - the user wants fewer tools/files, not more).
- **`tests/test_upload_shim.py`** is replaced by **`tests/test_officecli_file.py`**.

## Error handling

- `action` not in `{"upload","download"}` -> `{"error":"unknown action '<x>'"}`.
- `action="upload"` with no `__files__` -> `{"error":"no files attached"}` (existing).
- `action="download"` with no `file_id` -> `{"error":"file_id required"}`.
- officecli-mcp returns 404 on `GET /files/{file_id}` (file expired past TTL) ->
  `{"error":"file_id not found or expired (TTL ~1h); re-create or increase OFFICECLI_MCP_WORK_TTL_SECONDS"}`.
- OpenWebUI `POST /api/v1/files/` fails (non-2xx) -> `{"error":"openwebui upload failed: <status> <body>"}`.
- Each upload entry that errors is reported per-file in the `files` list (existing pattern).

## Out of scope (YAGNI)

- No `delete` action. Cleanup is handled by the officecli-mcp TTL sweep, or by the existing
  `DELETE /files/{id}` HTTP endpoint if ever needed. Not worth a third action now.
- No `__event_emitter__` `type:"files"` inline-attachment trick. It is undocumented and
  unverified against the deployment's OWUI version. Can be layered on later if desired.
- No changes to the officecli-mcp server (`src/`) or to the `officecli_*` MCP tools. This
  work is entirely the OpenWebUI native-tool shim in `examples/`.

## Testing

Unit tests in `tests/test_officecli_file.py` (mocked HTTP via in-process Starlette
`TestClient`s, matching the existing `test_upload_shim.py` style):

1. `test_upload_action` - the current upload assertions carried over: `__files__` with one
   entry -> returned `files[0].file_id` set, `filename` preserved, `hint` mentions
   `officecli_`, and the OWUI `GET /content` received the forwarded user Bearer.
2. `test_download_action_pushes_to_owui_storage` - stub officecli-mcp `GET /files/{id}`
   returns bytes with `content-disposition: attachment; filename="Kimi_K3.pptx"`; stub OWUI
   `POST /api/v1/files/?process=false` returns `{"id":"owui-xyz"}`. Assert the returned JSON
   `url` equals `{openwebui_browser_url}/api/v1/files/owui-xyz/content`, `filename` ==
   `Kimi_K3.pptx`, `size` matches, AND the OWUI POST received
   `Authorization: Bearer <user token>` and was sent to the `?process=false` path.
3. `test_unknown_action_returns_error` - `officecli_file(action="frobnicate")` ->
   `{"error":"unknown action 'frobnicate'"}`.
4. `test_owui_headers_forwards_authorization` - kept from today (both actions use it).

Manual verification (spec's definition-of-done): install `officecli_file` in the deployment's
OpenWebUI with Valves set (`officecli_mcp_url`, `openwebui_url`, `openwebui_browser_url=https://ai.savorcare.com`);
have the model `officecli_create` a doc, then call
`officecli_file(action="download", file_id=<id>)`; confirm the returned URL loads the file in
a browser at `https://ai.savorcare.com/api/v1/files/{owui_id}/content`.

## References

- OpenWebUI Tools docs (no file-return mechanism documented):
  https://docs.openwebui.com/features/extensibility/plugin/tools/
- OpenWebUI API endpoints (`POST /api/v1/files/`):
  https://docs.openwebui.com/reference/api-endpoints/
- Research: native tool file-return is unsupported; established workaround is external file
  server / push-to-OWUI-storage + URL return. `POST /api/v1/files/?process=false` skips RAG
  ingestion. GitHub discussions #11815, #23233, #14773; OWUI_File_Gen_Export project.
- Existing upload shim: `examples/openwebui_officecli_upload.py` (auth-forwarding pattern
  reused for both actions).
- officecli-mcp download endpoint: `GET /files/{file_id}` in `src/officecli_mcp/files.py`.
- TTL: `OFFICECLI_MCP_WORK_TTL_SECONDS` (default 3600s = 1h) in
  `src/officecli_mcp/config.py`.
