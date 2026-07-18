# Design: unified file-transfer entry (`stage` action) for assets into documents

**Date:** 2026-07-18
**Status:** Approved (pending spec review)
**Goal:** Let a remote LLM insert generated/uploaded images and CSV data into Office
documents by handle, without ever holding raw bytes. Extends the existing
`officecli_file` native tool with a third action `stage`, plus an `officecli_import`
MCP tool, reusing the current upload/download shape so the model learns one entry.

## Background & problem

The trigger: a user asked the OpenWebUI image-generation tool to make a "Kimi K3"
picture and insert it into a PPT. The image was generated successfully, but the
"insert into PPT" step could not complete. Root cause (investigated, not a guess):

- `officecli` itself **does** support inserting images: `add <file> /slide[N]
  --type picture --prop src=<path|url|data-uri> --prop width=5in ...`.
- The officecli-mcp bridge exposes `officecli_add`, but with a **single `prop`
  string** (`tools.py:124-129`), so multi-prop picture adds (`src`+`width`+
  `height`+`x`+`y`) cannot be expressed.
- `FileStore.put` only accepts `{docx,xlsx,pptx}` extensions (`files.py:17,40-42`),
  so image/CSV bytes cannot be stored at all.
- The OpenWebUI native shim `officecli_file` has only `upload` (from `__files__`)
  and `download`; there is no way to drop an asset into a document's workdir.
- Core constraint (CLAUDE.md): OpenWebUI does not inject file bytes into
  MCP/OpenAPI tool params, and the LLM never holds raw bytes. Only a native
  OpenWebUI tool can fetch bytes (via `GET /api/v1/files/{id}/content` with the
  forwarded user credential) and push them to officecli-mcp.

Note: the `read tcp ... connection reset by peer` error the user saw is a Go
network error from OpenWebUI -> DeepSeek-proxy on the *response* hop (after the
image tool already returned success). It is unrelated to this feature (proxy
transient reset) and resolved by retrying the turn. This design addresses the
real gap: asset bytes have no path into a document workdir.

## officecli file-input surfaces surveyed

| Surface | Source form | Use |
|---|---|---|
| `add --type picture` (docx/pptx/xlsx) | `src=` path / URL / data-URI | insert image |
| `add --type media` (pptx) | `src=` path / URL / data-URI | insert video/audio |
| `add --type ole` | file path | embed OLE object |
| `import <file> /Sheet1 <source>` | CSV/TSV path / `--stdin` | Excel data import |
| `merge <template> <output> --data <json\|path>` | JSON path / inline JSON | template merge |

## Scope (this iteration)

**In scope:**
- `stage` action covering **images** (png/jpg/jpeg/gif/webp/bmp/svg) and
  **CSV/TSV**.
- Both source paths: `source_file_id` (generated-image products, fetched from
  OpenWebUI storage) and `__files__` (user-attached files).
- New `officecli_import` MCP tool (CSV/TSV -> Excel), since staged CSV needs a
  consumer.
- `officecli_add` / `officecli_set` `prop` changed to `list[str]` (multi-prop).
- `path_for` fix so staged assets are not mistaken for the document.

**Out of scope (later):**
- `merge` (its `--data` can be inline JSON, so it does not need the file-transfer
  entry; add as a separate MCP tool later).
- `media`/`ole` (same staging mechanism, but not requested now).
- TTL handling for assets: assets live in the target document's workdir and are
  swept together with it; no separate lifecycle.

## §1 Architecture & end-to-end data flow

`stage` becomes the third action of `officecli_file`, isomorphic to existing
`upload`/`download`. The model only ever holds handles, never bytes.

**A. Generated image -> PPT**
```
image-gen tool  ->  OpenWebUI storage (/api/v1/files/{owui_img_id}/content)
      | model receives owui_img_id
officecli_file(action="stage", file_id=<pptx>, source_file_id=owui_img_id,
               filename="kimi.png", __request__)
      | shim: GET OpenWebUI bytes (forwarded cred) -> POST officecli-mcp /files/stage
      | officecli-mcp writes <pptx workdir>/kimi.png -> {"asset":"kimi.png"}
officecli_add(file_id=<pptx>, selector="/slide[1]", type="picture",
              prop=["src=kimi.png","width=5in","x=1cm","y=1cm"])
      | runner: {path}->pptx abs path, cwd=workdir, officecli resolves rel name
officecli_view_screenshot(file_id=<pptx>, page=1)   -> verify
officecli_file(action="download", file_id=<pptx>)   -> download link
```

**B. User-uploaded CSV -> Excel**
```
user attaches kpi.csv (__files__)
officecli_file(action="stage", file_id=<xlsx>, __files__=[...], filename="kpi.csv")
      | bytes -> <xlsx workdir>/kpi.csv -> {"asset":"kpi.csv"}
officecli_import(file_id=<xlsx>, sheet="/Sheet1", source="kpi.csv", header=true)
```

**Key invariant:** the asset file always lives in the **same workdir** as the
target document, and officecli references it by **relative filename**
(`src=kimi.png`). This reuses `runner.run`'s existing `cwd = Path(path).parent`
(`runner.py:48`), so `add`/`import` need no absolute-path awareness.

## §2 `officecli_file` `stage` action (OpenWebUI shim side)

Signature (add `source_file_id`; existing params unchanged):
```python
async def officecli_file(
    self,
    action: str,
    __files__: list[dict] = [],        # upload / stage (user-attached source)
    __request__: Any = None,
    file_id: str = "",                  # upload: ignored / download: source / stage: target doc
    filename: str = "",                 # download / stage: asset save name
    source_file_id: str = "",           # stage (generated-image source): OpenWebUI asset id
) -> str
```

`action == "stage"`:
1. **Fetch bytes** (one of two sources):
   - `source_file_id` present -> reuse `_owui_get(source_file_id, __request__)`
     (generated-image path).
   - else -> take first of `__files__` (user-attached path), same byte-fetch as
     `_upload`.
   - Shared fetch logic extracted to a `_fetch_bytes` helper used by both
     `_upload` and `_stage`.
2. **POST to officecli-mcp** new endpoint `POST /files/stage` with target
   `file_id` + `filename` + bytes.
3. **Return:** `{"asset": "kimi.png", "target": "<file_id>",
   "hint": "Pass asset as src= to officecli_add (type=picture) or as source to officecli_import."}`

**Why a new endpoint, not `POST /files`:** existing `POST /files` creates a new
file_id + new workdir (`FileStore.put`); stage writes into an **existing**
target document's workdir. Different semantics -> dedicated `POST /files/stage`
that produces no new file_id.

**Async/threading:** all `requests` calls via `anyio.to_thread.run_sync`, same as
existing `_upload`/`_download`, to avoid blocking OpenWebUI's single uvicorn
worker.

**Source selection:** `source_file_id` takes precedence; `__files__` is the
fallback. This lets a single action serve both generated images and user
uploads without an exploded signature.

## §3 officecli-mcp side: `/files/stage` endpoint + `FileStore` whitelist

### 3.1 `FileStore` extension
- New module constant `STAGE_EXT = {"png","jpg","jpeg","gif","webp","bmp","svg","csv","tsv"}`.
- New method `stage_asset(self, target_file_id, filename, data) -> dict`:
  - validate extension in `STAGE_EXT`
  - `_safe_filename` (path-traversal guard, reused)
  - resolve target workdir `self._dir(target_file_id)`; raise `KeyError` if absent
    (target document must already exist)
  - write `<target workdir>/<safe filename>`, overwriting same-name
  - return `{"asset": <safe filename>, "target": target_file_id}`
- `put` unchanged (document upload still only docx/xlsx/pptx).

### 3.2 `path_for` fix (necessary regression fix)
`shot.png` is the `view screenshot` product (`runner.py:50-54` injects
`-o <workdir>/shot.png`). After stage, a workdir may contain `deck.pptx` +
`shot.png` + `kimi.png`. Current `path_for` (`files.py:56`) picks "first file
that isn't shot.png" -> could return `kimi.png` and make the runner operate on
the asset instead of the document.

Fix (option A, approved): `path_for` returns only **document-extension** files
(ext in `_SAFE_EXT` = docx/xlsx/pptx); the `!= "shot.png"` special-case is
removed (subsumed by the extension whitelist - shot.png is ext png, excluded).
If no document file exists, raise `KeyError` (unchanged).
```python
docs = [p for p in d.iterdir()
        if p.is_file() and p.suffix.lower().lstrip(".") in _SAFE_EXT]
if not docs:
    raise KeyError(file_id)
return docs[0]
```
Note: this slightly tightens existing behavior (a workdir with only a non-doc
file now KeyErrors instead of returning it) - acceptable since a valid document
workdir always has its docx/xlsx/pptx.

### 3.3 HTTP endpoint
```python
async def stage(request: Request) -> Response:
    # auth via _check_api_key (same as upload)
    # accept multipart (target_file_id, filename, file) OR JSON ({target_file_id, filename, data_base64})
    # enforce max_upload_mb (same as upload)
    # call store.stage_asset(...); return JSON
```
Route: `Route("/files/stage", stage, methods=["POST"])`.

## §4 MCP tool layer (`tools.py`)

### 4.1 `prop` -> `list[str]` for `officecli_add` / `officecli_set`
```python
@mcp.tool(annotations=_WRITE)
def officecli_add(file_id, selector, type, prop: list[str] | None = None) -> str:
    argv = ["add", "{path}", selector, "--type", type]
    if prop:
        for p in prop:
            argv += ["--prop", p]
    return _run_text(runner, file_id, argv)
```
`officecli_set` identical pattern. Single-prop callers pass `["bold=true"]`.

### 4.2 New `officecli_import` tool
```python
@mcp.tool(annotations=_WRITE)
def officecli_import(file_id, sheet: str, source: str, header: bool = False,
                     start_cell: str = "A1", format: str | None = None) -> str:
    """Import CSV/TSV into an Excel sheet. source = staged asset filename."""
    argv = ["import", "{path}", sheet, source]
    if header: argv += ["--header"]
    argv += ["--start-cell", start_cell]
    if format: argv += ["--format", format]
    return _run_text(runner, file_id, argv)
```
`source` is the staged relative filename; runner's `cwd=workdir` resolves it.

### 4.3 Instructions update
Append to `build_mcp` instructions:
> "To insert an image or import CSV: first call `officecli_file` with
> action=\"stage\" (source_file_id= an OpenWebUI image id, or rely on __files__)
> to drop the asset into the document's workdir, then call `officecli_add` with
> type=picture and prop=[\"src=<asset>\", ...] (or `officecli_import` with
> source=<asset>)."

### 4.4 Not exposed this iteration
`merge` (deferred).

## §5 Error handling, testing, verification

### 5.1 Error handling
| Scenario | Behavior |
|---|---|
| stage target `file_id` missing/expired | `stage_asset` raises `KeyError` -> 404 `{"error":"target file_id not found or expired"}`; shim -> `{"error":...}` |
| asset ext not whitelisted | `stage_asset` raises `ValueError` -> 415 `{"error":"extension .xxx not allowed"}` (matches `put`) |
| byte fetch fails (OpenWebUI 404/timeout) | shim `except Exception` -> `{"filename":..., "error":...}` (matches `_upload`) |
| `path_for` finds no document file | `KeyError` -> `FileIDNotFound` -> `ToolError` (unchanged) |
| `add picture` fails (e.g. src not found) | `_run_text` exit_code!=0 -> `ToolError("officecli exited N: <stderr>")` (unchanged) |

### 5.2 Testing
- `test_files.py`: `stage_asset` write + ext validation; `path_for` prefers doc
  ext (regression: staged png not mistaken for doc); `/files/stage` multipart +
  JSON inputs; 404/415.
- `test_tools.py`: `add`/`set` multi-prop expands to multiple `--prop`;
  `officecli_import` argv correct.
- `test_runner.py`: after stage, `path_for` still returns the doc path
  (regression).
- E2E (`test_e2e_real.py`, real binary): stage a real png -> `add picture` ->
  `view_screenshot` shows the image.

### 5.3 Verification (drive the real flow, per CLAUDE.md)
1. Start officecli-mcp (http).
2. `POST /files` a real .pptx -> `file_id`.
3. `POST /files/stage` with that `file_id` + a real png + `filename=kimi.png` ->
   `{"asset":"kimi.png"}`.
4. `officecli_add(file_id, "/slide[1]", "picture", ["src=kimi.png","width=5in","x=1cm","y=1cm"])`.
5. `officecli_view_screenshot(file_id, page=1)` -> confirm the image appears.
6. `GET /files/{file_id}` download pptx, open locally, confirm image on slide1.

## Open questions
None. All five design decisions confirmed with the user:
1. Extend `officecli_file` with a `stage` action (isomorphic to upload/download).
2. Cover images + CSV/TSV; add `officecli_import`; defer `merge`.
3. Support both `source_file_id` and `__files__` sources.
4. stage writes into the target document's workdir, returns `asset` filename.
5. `add`/`set` `prop` -> `list[str]`.
