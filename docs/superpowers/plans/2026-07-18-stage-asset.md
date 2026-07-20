# Unified File-Transfer `stage` Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a remote LLM insert generated/uploaded images and CSV data into Office documents by handle, by adding a `stage` action to `officecli_file`, an `officecli_import` MCP tool, and multi-prop `add`/`set`.

**Architecture:** `officecli_file(action="stage", file_id=<target doc>, source_file_id=<OWUI asset> or __files__=<user-attached>, filename=...)` fetches bytes via the OpenWebUI native tool (the only place that can), POSTs them to a new officecli-mcp endpoint `POST /files/stage`, which writes the asset into the target document's workdir and returns `{"asset": <name>}`. The model then passes `asset` as `src=` to `officecli_add(type=picture)` or as `source` to `officecli_import`. Assets and documents share one workdir; officecli references assets by relative filename because `runner.run` sets `cwd = Path(path).parent`.

**Tech Stack:** Python 3.11+, FastMCP + Starlette, pydantic, `requests` + `anyio` (OpenWebUI shim), pytest.

## Global Constraints

- The LLM never holds raw bytes; only the OpenWebUI native tool fetches bytes (forwarded user credential via `GET /api/v1/files/{id}/content`) and pushes to officecli-mcp. (spec §Background, CLAUDE.md core constraint)
- Document upload (`FileStore.put`) stays restricted to `{docx,xlsx,pptx}`. Asset staging uses a separate whitelist `STAGE_EXT = {"png","jpg","jpeg","gif","webp","bmp","svg","csv","tsv"}`. (spec §3.1)
- All `requests` calls in the OpenWebUI shim go through `anyio.to_thread.run_sync` (OpenWebUI runs sync tools in its single uvicorn loop). (spec §2)
- `path_for` must return only document-extension files (docx/xlsx/pptx); the `!= "shot.png"` special-case is removed. (spec §3.2)
- `merge` and `media`/`ole` are out of scope this iteration. (spec §Scope)
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

## File Structure

- **Modify** `src/officecli_mcp/files.py` — add `STAGE_EXT`, `FileStore.stage_asset`, `path_for` fix, `stage` route handler, register in `build_files_router`.
- **Modify** `src/officecli_mcp/server.py` — register `POST /files/stage` custom route (before `POST /files/{file_id}`).
- **Modify** `src/officecli_mcp/tools.py` — `prop` -> `list[str]` for `officecli_add`/`officecli_set`; add `officecli_import`; update instructions.
- **Modify** `examples/openwebui_officecli_file.py` — add `source_file_id` param, `_fetch_bytes` helper, `_mcp_stage` helper, `stage` action branch.
- **Modify** `tests/test_files.py` — `stage_asset`, `path_for` regression, `/files/stage` endpoint tests.
- **Modify** `tests/test_tools.py` — multi-prop, `officecli_import` tests.
- **Modify** `tests/test_runner.py` — `path_for`-after-stage regression.
- **Modify** `tests/test_officecli_file.py` — `stage` action tests (both sources).
- **Modify** `tests/test_e2e_real.py` — end-to-end stage->add picture->screenshot.

---

## Task 1: `FileStore.stage_asset` + `STAGE_EXT` + `path_for` fix

**Files:**
- Modify: `src/officecli_mcp/files.py:17` (add `STAGE_EXT`), `:50-59` (`path_for`), `:28-72` (add `stage_asset` to `FileStore`)
- Test: `tests/test_files.py`

**Interfaces:**
- Consumes: existing `_SAFE_EXT`, `_safe_filename`, `FileStore._dir`.
- Produces: `STAGE_EXT` (module set), `FileStore.stage_asset(target_file_id, filename, data) -> {"asset": str, "target": str}`; tightened `path_for`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_files.py`)

```python
def test_stage_asset_writes_into_target_workdir(settings):
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    # Target document must exist first.
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    info = store.stage_asset(doc["file_id"], "kimi.png", b"\x89PNG\r\n\x1a\nfake")
    assert info["asset"] == "kimi.png"
    assert info["target"] == doc["file_id"]
    assert Path(settings.work_dir, doc["file_id"], "kimi.png").exists()
    # The document is untouched and still discoverable.
    assert store.path_for(doc["file_id"]).name == "deck.pptx"


def test_stage_asset_rejects_bad_extension(settings):
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    with pytest.raises(ValueError):
        store.stage_asset(doc["file_id"], "evil.exe", b"nope")


def test_stage_asset_unknown_target_raises_keyerror(settings):
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    with pytest.raises(KeyError):
        store.stage_asset("ghost", "kimi.png", b"x")


def test_stage_asset_strips_path_traversal(settings):
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    info = store.stage_asset(doc["file_id"], "../escape.png", b"x")
    # _safe_filename keeps only the basename.
    assert info["asset"] == "escape.png"
    assert not Path(settings.work_dir, "escape.png").exists()


def test_path_for_prefers_document_over_staged_png(settings):
    """Regression: after staging kimi.png next to deck.pptx, path_for must
    still return deck.pptx, not the png."""
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    store.stage_asset(doc["file_id"], "kimi.png", b"\x89PNG")
    # Also drop a screenshot product (legacy shot.png) to confirm it's ignored.
    Path(settings.work_dir, doc["file_id"], "shot.png").write_bytes(b"\x89PNG")
    assert store.path_for(doc["file_id"]).name == "deck.pptx"


def test_path_for_with_only_non_doc_file_raises(settings):
    """A workdir whose only file is not a document extension now KeyErrors
    (tightened behavior)."""
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    # Manually create a workdir with only a png, no doc.
    d = Path(settings.work_dir, "lonely")
    d.mkdir(parents=True)
    (d / "kimi.png").write_bytes(b"\x89PNG")
    with pytest.raises(KeyError):
        store.path_for("lonely")
```

Add `import pytest` at top of `tests/test_files.py` if not present (it is not currently imported there).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_files.py -v -k "stage_asset or path_for"`
Expected: FAIL — `AttributeError: 'FileStore' object has no attribute 'stage_asset'` and `path_for` returns png.

- [ ] **Step 3: Implement `STAGE_EXT` and `stage_asset`, fix `path_for`**

In `src/officecli_mcp/files.py`, replace the `_SAFE_EXT` line and `path_for`, and add `stage_asset`:

```python
_SAFE_EXT = {"docx", "xlsx", "pptx"}
STAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "csv", "tsv"}
```

Replace the `path_for` method body (currently lines ~50-59) with:

```python
    def path_for(self, file_id: str, filename: str | None = None) -> Path:
        d = self._dir(file_id)
        if not d.exists():
            raise KeyError(file_id)
        if filename:
            return d / _safe_filename(filename)
        # Return only document-extension files; staged assets (png/csv/...) and
        # the screenshot product (shot.png) are never the document itself.
        docs = [
            p for p in d.iterdir()
            if p.is_file() and p.suffix.lower().lstrip(".") in _SAFE_EXT
        ]
        if not docs:
            raise KeyError(file_id)
        return docs[0]
```

Add `stage_asset` method to `FileStore` (after `put`, before `path_for`):

```python
    def stage_asset(self, target_file_id: str, filename: str, data: bytes) -> dict:
        """Write an asset (image/CSV/TSV) into an EXISTING document's workdir.

        Unlike put(), this does not create a new file_id; it drops the asset
        alongside the target document so officecli can reference it by relative
        filename (src=kimi.png).
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in STAGE_EXT:
            raise ValueError(f"extension .{ext} not allowed for staging")
        d = self._dir(target_file_id)
        if not d.exists():
            raise KeyError(target_file_id)
        safe = _safe_filename(filename)
        (d / safe).write_bytes(data)
        return {"asset": safe, "target": target_file_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_files.py -v -k "stage_asset or path_for"`
Expected: PASS (all 6 new tests).

Then run the full files suite to confirm no regression:
Run: `pytest tests/test_files.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/files.py tests/test_files.py
git commit -m "$(cat <<'EOF'
feat(files): add stage_asset + STAGE_EXT, fix path_for to prefer doc ext

stage_asset writes an image/CSV asset into an existing document workdir.
path_for now returns only docx/xlsx/pptx files so staged assets and the
screenshot product (shot.png) are never mistaken for the document.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `POST /files/stage` HTTP endpoint

**Files:**
- Modify: `src/officecli_mcp/files.py` (`stage` handler, `build_files_router`)
- Modify: `src/officecli_mcp/server.py:32-48` (register custom route)
- Test: `tests/test_files.py`

**Interfaces:**
- Consumes: `FileStore.stage_asset` (Task 1), `_check_api_key`, `settings.max_upload_mb`.
- Produces: `POST /files/stage` accepting multipart (`target_file_id`, `filename`, `file`) or JSON (`{target_file_id, filename, data_base64}`); returns `{"asset","target"}` or 404/415/413.

- [ ] **Step 1: Write failing tests** (append to `tests/test_files.py`)

```python
def test_stage_endpoint_multipart(settings):
    app, store = _make_app(settings)
    # Seed a target document.
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    client = TestClient(app)
    resp = client.post(
        "/files/stage",
        data={"target_file_id": doc["file_id"], "filename": "kimi.png"},
        files={"file": ("kimi.png", b"\x89PNGfake", "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["asset"] == "kimi.png"
    assert body["target"] == doc["file_id"]
    assert Path(settings.work_dir, doc["file_id"], "kimi.png").exists()


def test_stage_endpoint_base64(settings):
    app, store = _make_app(settings)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    client = TestClient(app)
    data = base64.b64encode(b"\x89PNGfake").decode()
    resp = client.post(
        "/files/stage",
        json={"target_file_id": doc["file_id"], "filename": "kimi.png", "data_base64": data},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["asset"] == "kimi.png"


def test_stage_endpoint_unknown_target_404(settings):
    app, store = _make_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/files/stage",
        data={"target_file_id": "ghost", "filename": "kimi.png"},
        files={"file": ("kimi.png", b"x", "image/png")},
    )
    assert resp.status_code == 404


def test_stage_endpoint_bad_extension_415(settings):
    app, store = _make_app(settings)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    client = TestClient(app)
    resp = client.post(
        "/files/stage",
        data={"target_file_id": doc["file_id"], "filename": "evil.exe"},
        files={"file": ("evil.exe", b"nope", "application/octet-stream")},
    )
    assert resp.status_code == 415


def test_stage_endpoint_too_large_413(settings):
    app, store = _make_app(settings)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    client = TestClient(app)
    # settings.max_upload_mb is 50 in conftest; push 51MB.
    big = b"\x89PNG" + b"x" * (51 * 1024 * 1024)
    resp = client.post(
        "/files/stage",
        data={"target_file_id": doc["file_id"], "filename": "big.png"},
        files={"file": ("big.png", big, "image/png")},
    )
    assert resp.status_code == 413
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_files.py -v -k "stage_endpoint"`
Expected: FAIL — 404 on `/files/stage` (route not registered).

- [ ] **Step 3: Add `stage` handler + register in router**

In `src/officecli_mcp/files.py`, add a new async handler after the `download` handler (before `delete`):

```python
async def stage(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            payload = await request.json()
            target_file_id = payload["target_file_id"]
            filename = payload["filename"]
            data = base64.b64decode(payload["data_base64"])
        else:
            form = await request.form()
            target_file_id = form["target_file_id"]
            upload_file = form["file"]
            filename = upload_file.filename or "asset.bin"
            data = await upload_file.read()
    except (KeyError, ValueError) as e:
        return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return JSONResponse({"error": "file too large"}, status_code=413)

    try:
        info = store.stage_asset(target_file_id, filename, data)
    except KeyError:
        return JSONResponse(
            {"error": "target file_id not found or expired"}, status_code=404
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=415)
    return JSONResponse(info)
```

Register it in `build_files_router` (add the stage route **before** the `{file_id}` routes so Starlette matches the literal path first):

```python
def build_files_router(store: FileStore, settings) -> Router:
    routes = [
        Route("/files", upload, methods=["POST"]),
        Route("/files/stage", stage, methods=["POST"]),
        Route("/files/{file_id}", download, methods=["GET"]),
        Route("/files/{file_id}", delete, methods=["DELETE"]),
    ]
    return Router(routes)
```

- [ ] **Step 4: Register the route in `build_app` (server.py)**

In `src/officecli_mcp/server.py`, import `stage` and add the custom route. Update the import line:

```python
from officecli_mcp.files import FileStore, delete, download, stage, upload
```

Add the custom route after the `_upload` route and **before** `_download` (literal path before the `{file_id}` parameter route):

```python
    @mcp.custom_route("/files/stage", methods=["POST"])
    async def _stage(request):
        request.app.state.settings = settings
        request.app.state.file_store = file_store
        return await stage(request)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_files.py -v -k "stage_endpoint"`
Expected: PASS (all 5).

Run full files suite:
Run: `pytest tests/test_files.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/officecli_mcp/files.py src/officecli_mcp/server.py tests/test_files.py
git commit -m "$(cat <<'EOF'
feat(files): add POST /files/stage endpoint for asset staging

Accepts multipart or base64 JSON; writes asset into the target document's
workdir. Registered before /files/{file_id} so the literal path matches.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Multi-prop `officecli_add` / `officecli_set`

**Files:**
- Modify: `src/officecli_mcp/tools.py:109-129` (`officecli_set`, `officecli_add`)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `_run_text`, `runner`.
- Produces: `officecli_add(file_id, selector, type, prop: list[str] | None)` and `officecli_set(file_id, selector, prop: list[str] | None)`; each `prop` item becomes its own `--prop` argv entry.

- [ ] **Step 1: Write failing tests** (append to `tests/test_tools.py`)

These tests use a stub binary that records its argv so we can assert `--prop` repetition. Add a recorder stub helper and tests:

```python
async def test_add_multi_prop_emits_multiple_dash_prop(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.pptx", b"pptx-bytes")
    # Recorder: write argv to a file for assertion.
    rec = tmp_path / "argv.txt"
    stub = Path(mcp._runner.binary_path)
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {rec}\necho OK\n")
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_add",
            {
                "file_id": info["file_id"],
                "selector": "/slide[1]",
                "type": "picture",
                "prop": ["src=kimi.png", "width=5in", "x=1cm", "y=1cm"],
            },
        )
        assert not res.isError, res.content
    argv = rec.read_text().splitlines()
    # Four --prop tokens, each followed by its value.
    prop_indices = [i for i, a in enumerate(argv) if a == "--prop"]
    assert len(prop_indices) == 4
    assert argv[prop_indices[0] + 1] == "src=kimi.png"
    assert argv[prop_indices[1] + 1] == "width=5in"


async def test_add_no_prop_omits_flag(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.pptx", b"pptx-bytes")
    rec = tmp_path / "argv.txt"
    stub = Path(mcp._runner.binary_path)
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {rec}\necho OK\n")
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool(
            "officecli_add",
            {"file_id": info["file_id"], "selector": "/", "type": "slide"},
        )
    argv = rec.read_text().splitlines()
    assert "--prop" not in argv


async def test_set_multi_prop(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.docx", b"docx-bytes")
    rec = tmp_path / "argv.txt"
    stub = Path(mcp._runner.binary_path)
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {rec}\necho OK\n")
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool(
            "officecli_set",
            {
                "file_id": info["file_id"],
                "selector": "/body/p[1]",
                "prop": ["bold=true", "size=14"],
            },
        )
    argv = rec.read_text().splitlines()
    assert argv.count("--prop") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tools.py -v -k "multi_prop or no_prop or set_multi"`
Expected: FAIL — FastMCP rejects `prop` as a list because the signature is `prop: str | None`; or the single `--prop` is emitted once. Tests assert `count == 2`/`== 4`, so they fail.

- [ ] **Step 3: Change `prop` to `list[str] | None`**

In `src/officecli_mcp/tools.py`, replace `officecli_set` (lines ~109-112):

```python
    @mcp.tool(annotations=_WRITE)
    def officecli_set(file_id: str, selector: str, prop: list[str] | None = None) -> str:
        """Set a property on matched elements. prop is a list of 'key=value'."""
        argv = ["set", "{path}", selector]
        if prop:
            for p in prop:
                argv += ["--prop", p]
        return _run_text(runner, file_id, argv)
```

Replace `officecli_add` (lines ~123-129):

```python
    @mcp.tool(annotations=_WRITE)
    def officecli_add(file_id: str, selector: str, type: str, prop: list[str] | None = None) -> str:
        """Add an element. selector=/ for top-level (e.g. add a slide with type=slide).

        prop is a list of 'key=value' (e.g. ["src=kimi.png","width=5in"] for a picture).
        """
        argv = ["add", "{path}", selector, "--type", type]
        if prop:
            for p in prop:
                argv += ["--prop", p]
        return _run_text(runner, file_id, argv)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tools.py -v -k "multi_prop or no_prop or set_multi"`
Expected: PASS.

Run full tools suite to catch regressions:
Run: `pytest tests/test_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(tools): officecli_add/set prop now a list for multiple --prop

Picture adds need src+width+height+x+y. Each prop item emits its own --prop.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `officecli_import` MCP tool

**Files:**
- Modify: `src/officecli_mcp/tools.py` (add tool, after `officecli_add`)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `_run_text`, `runner`.
- Produces: `officecli_import(file_id, sheet, source, header=False, start_cell="A1", format=None) -> str`. Maps to `officecli import {path} <sheet> <source> [--header] --start-cell <c> [--format <f>]`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_tools.py`)

```python
async def test_import_tool_argv(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.xlsx", b"xlsx-bytes")
    rec = tmp_path / "argv.txt"
    stub = Path(mcp._runner.binary_path)
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {rec}\necho IMPORTED\n")
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_import",
            {
                "file_id": info["file_id"],
                "sheet": "/Sheet1",
                "source": "kpi.csv",
                "header": True,
                "start_cell": "B2",
                "format": "csv",
            },
        )
        assert not res.isError, res.content
    argv = rec.read_text().splitlines()
    assert argv[0] == "import"
    assert "/Sheet1" in argv
    assert "kpi.csv" in argv
    assert "--header" in argv
    assert "--start-cell" in argv
    assert argv[argv.index("--start-cell") + 1] == "B2"
    assert "--format" in argv
    assert argv[argv.index("--format") + 1] == "csv"


async def test_import_tool_listed(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert "officecli_import" in {t.name for t in tools.tools}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tools.py -v -k "import_tool"`
Expected: FAIL — `officecli_import` not found / not listed.

- [ ] **Step 3: Add `officecli_import` tool**

In `src/officecli_mcp/tools.py`, insert after the `officecli_add` function (before `officecli_remove`):

```python
    @mcp.tool(annotations=_WRITE)
    def officecli_import(
        file_id: str,
        sheet: str,
        source: str,
        header: bool = False,
        start_cell: str = "A1",
        format: str | None = None,
    ) -> str:
        """Import CSV/TSV into an Excel sheet. source is a staged asset filename
        (drop it first via officecli_file action='stage'). sheet e.g. /Sheet1."""
        argv = ["import", "{path}", sheet, source]
        if header:
            argv.append("--header")
        argv += ["--start-cell", start_cell]
        if format:
            argv += ["--format", format]
        return _run_text(runner, file_id, argv)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tools.py -v -k "import_tool"`
Expected: PASS.

Run full tools suite:
Run: `pytest tests/test_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(tools): add officecli_import MCP tool (CSV/TSV -> Excel)

Consumes a staged asset filename as source; runner cwd resolves it.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update MCP server instructions

**Files:**
- Modify: `src/officecli_mcp/tools.py:28-42` (instructions string)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: none.
- Produces: instructions mentioning `stage`, `picture`, `src=`, `officecli_import`.

- [ ] **Step 1: Write failing test** (append to `tests/test_tools.py`)

```python
async def test_instructions_teach_stage_workflow(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.initialize()
    text = (getattr(result, "instructions", None) or "").lower()
    assert "stage" in text
    assert "picture" in text
    assert "officecli_import" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tools.py -v -k "instructions_teach_stage"`
Expected: FAIL — `"stage" not in text`.

- [ ] **Step 3: Update instructions**

In `src/officecli_mcp/tools.py`, append a sentence to the `instructions` tuple (after the "...discover them." sentence, before the closing paren). Add this as a new string segment in the tuple:

```python
            "Selectors are officecli DOM/CSS paths like /slide[1] or /body/p[2]; run "
            "officecli_view_annotated or officecli_view_outline to discover them. "
            "ASSETS: to insert an image or import CSV, first call `officecli_file` "
            "with action=\"stage\" (source_file_id= an OpenWebUI image id, or rely on "
            "__files__) to drop the asset into the document's workdir and get an "
            "asset filename; then call `officecli_add` with type=picture and "
            "prop=[\"src=<asset>\",\"width=...\",\"x=...\",\"y=...\"] (or "
            "`officecli_import` with source=<asset>)."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tools.py -v -k "instructions_teach_stage"`
Expected: PASS.

Run full tools suite:
Run: `pytest tests/test_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
docs(tools): teach stage->add picture / import workflow in instructions

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `officecli_file` `stage` action (OpenWebUI shim)

**Files:**
- Modify: `examples/openwebui_officecli_file.py` (`officecli_file` signature, add `_fetch_bytes`, `_mcp_stage`, `_stage` branch)
- Test: `tests/test_officecli_file.py`

**Interfaces:**
- Consumes: `_owui_get`, `_owui_headers`, `anyio.to_thread.run_sync`; officecli-mcp `POST /files/stage` (Task 2).
- Produces: `officecli_file(action="stage", file_id=<target>, source_file_id=<owui asset> or __files__=[...], filename=..., __request__=...)` -> `{"asset","target","hint"}`. Helper `_mcp_stage(target_file_id, filename, data) -> dict` (monkeypatchable in tests).

- [ ] **Step 1: Write failing tests** (append to `tests/test_officecli_file.py`)

```python
async def test_stage_action_from_source_file_id(monkeypatch):
    """stage(source_file_id): fetch OWUI image bytes, POST to /files/stage, return asset."""
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata", "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        })()
    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    # Seed a target pptx in officecli-mcp.
    target = mcp_client.post(
        "/files", files={"file": ("deck.pptx", b"PK\x03\x04pptx", "application/octet-stream")}
    ).json()["file_id"]

    image_bytes = b"\x89PNG\r\n\x1a\ngenerated-image"
    received_auth = {}

    async def fake_content(request):
        received_auth["auth"] = request.headers.get("authorization")
        return Response(image_bytes, media_type="image/png")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui"
    )
    monkeypatch.setattr(
        tools, "_owui_get",
        lambda fid, __request__: owui_client.get(
            f"/api/v1/files/{fid}/content", headers=tools._owui_headers(__request__)
        ).content,
    )
    monkeypatch.setattr(
        tools, "_mcp_stage",
        lambda target_fid, fname, data: mcp_client.post(
            "/files/stage",
            data={"target_file_id": target_fid, "filename": fname},
            files={"file": (fname, data, "image/png")},
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="stage",
            file_id=target,
            source_file_id="owui-img-1",
            filename="kimi.png",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
        )
    )
    assert result["asset"] == "kimi.png", result
    assert result["target"] == target, result
    assert "officecli_add" in result["hint"]
    assert received_auth["auth"] == "Bearer current-user-token", received_auth
    # Asset actually landed in the target workdir.
    from pathlib import Path as P
    assert P("/tmp/_fwork", target, "kimi.png").exists()


async def test_stage_action_from_files(monkeypatch):
    """stage(__files__): take first attached file, POST to /files/stage."""
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test2")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata2", "work_dir": "/tmp/_fwork2",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test2",
        })()
    shutil.rmtree("/tmp/_fwork2", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata2", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    target = mcp_client.post(
        "/files", files={"file": ("deck.pptx", b"PK\x03\x04pptx", "application/octet-stream")}
    ).json()["file_id"]

    csv_bytes = b"a,b\n1,2\n"
    received = {}

    async def fake_content(request):
        received["fetched"] = True
        return Response(csv_bytes, media_type="text/csv")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui"
    )
    monkeypatch.setattr(
        tools, "_owui_get",
        lambda fid, __request__: owui_client.get(
            f"/api/v1/files/{fid}/content", headers=tools._owui_headers(__request__)
        ).content,
    )
    monkeypatch.setattr(
        tools, "_mcp_stage",
        lambda target_fid, fname, data: mcp_client.post(
            "/files/stage",
            data={"target_file_id": target_fid, "filename": fname},
            files={"file": (fname, data, "text/csv")},
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="stage",
            file_id=target,
            __files__=[{"id": "csv-1", "name": "kpi.csv"}],
            filename="kpi.csv",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
        )
    )
    assert result["asset"] == "kpi.csv", result
    assert result["target"] == target, result
    assert "officecli_import" in result["hint"]
    assert received.get("fetched")


async def test_stage_without_target_file_id_returns_error():
    mod = _load_tools()
    tools = mod.Tools()
    result = json.loads(await tools.officecli_file(action="stage"))
    assert result == {"error": "file_id (target document) required"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_officecli_file.py -v -k "stage_action or stage_without"`
Expected: FAIL — `{"error": "unknown action 'stage'"}`.

- [ ] **Step 3: Implement the `stage` action in the shim**

In `examples/openwebui_officecli_file.py`:

(a) Add `source_file_id: str = ""` to the `officecli_file` signature (after `filename: str = ""`):

```python
    async def officecli_file(
        self,
        action: str,
        __files__: list[dict[str, Any]] = [],  # noqa: B006
        __request__: Any = None,
        file_id: str = "",
        filename: str = "",
        source_file_id: str = "",
    ) -> str:
```

(b) Add the `stage` dispatch in the action router:

```python
        if action == "upload":
            return await self._upload(__files__, __request__)
        if action == "download":
            return await self._download(file_id, filename, __request__)
        if action == "stage":
            return await self._stage(
                file_id, filename, source_file_id, __files__, __request__
            )
        return json.dumps({"error": f"unknown action '{action}'"})
```

(c) Add the `_mcp_stage` helper (next to `_mcp_post`):

```python
    def _mcp_stage(self, target_file_id: str, filename: str, data: bytes) -> dict:
        """Push an asset into officecli-mcp /files/stage (stage action) -> asset name."""
        url = f"{self.valves.officecli_mcp_url}/files/stage"
        files = {"file": (filename, data, "application/octet-stream")}
        data_field = {"target_file_id": target_file_id, "filename": filename}
        resp = requests.post(url, data=data_field, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()
```

(d) Add the `_fetch_bytes` helper and `_stage` method (after `_download`):

```python
    async def _fetch_bytes(
        self,
        source_file_id: str,
        files: list[dict[str, Any]],
        __request__: Any,
    ) -> tuple[bytes, str]:
        """Resolve asset bytes from one of two sources.

        source_file_id wins (generated-image products in OpenWebUI storage);
        else fall back to the first __files__ entry (user-attached). Returns
        (bytes, name).
        """
        if source_file_id:
            data = await anyio.to_thread.run_sync(self._owui_get, source_file_id, __request__)
            return data, ""
        if files:
            f = files[0]
            fid = f.get("id") or (f.get("file") or {}).get("id")
            name = f.get("name") or f.get("filename") or "asset.bin"
            if not fid:
                raise ValueError("attached file has no id")
            data = await anyio.to_thread.run_sync(self._owui_get, fid, __request__)
            return data, name
        raise ValueError("no source: pass source_file_id or attach a file")

    async def _stage(
        self,
        file_id: str,
        filename: str,
        source_file_id: str,
        files: list[dict[str, Any]],
        __request__: Any,
    ) -> str:
        if not file_id:
            return json.dumps({"error": "file_id (target document) required"})
        try:
            data, fallback_name = await self._fetch_bytes(source_file_id, files, __request__)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"asset fetch failed: {e}"})
        name = filename or fallback_name or "asset.bin"
        try:
            info = await anyio.to_thread.run_sync(self._mcp_stage, file_id, name, data)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"officecli-mcp stage failed: {e}"})
        asset = info.get("asset")
        if not asset:
            return json.dumps({"error": f"stage returned no asset: {info}"})
        return json.dumps(
            {
                "asset": asset,
                "target": file_id,
                "hint": (
                    "Pass asset as src= to officecli_add (type=picture, "
                    'prop=["src=<asset>",...]) or as source to officecli_import.'
                ),
            }
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_officecli_file.py -v -k "stage_action or stage_without"`
Expected: PASS (all 3).

Run the full shim suite:
Run: `pytest tests/test_officecli_file.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/openwebui_officecli_file.py tests/test_officecli_file.py
git commit -m "$(cat <<'EOF'
feat(shim): add officecli_file stage action (assets into a doc workdir)

source_file_id (generated images) or __files__ (user-attached); POSTs to
officecli-mcp /files/stage and returns the asset filename for add/import.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: End-to-end verification with the real binary

**Files:**
- Modify: `tests/test_e2e_real.py` (add `test_stage_image_into_pptx`)

**Interfaces:**
- Consumes: real `officecli` binary via `OFFICECLI_BIN`; `POST /files`, `POST /files/stage`, MCP tools `officecli_create`/`officecli_add`/`officecli_view_screenshot`.

This task has no new failing-test-then-implement cycle — it is the spec's definition-of-done verification. It uses a real binary, so it is skipped unless `OFFICECLI_BIN` is set.

- [ ] **Step 1: Write the e2e test** (append to `tests/test_e2e_real.py`)

```python
def test_stage_image_into_pptx(app, tmp_path):
    """Definition of done: stage a real PNG -> add picture -> screenshot shows it.

    Mirrors the spec §5.3 verification: stage asset, add picture with src=asset,
    view_screenshot to confirm the image appears on the slide.
    """
    client = TestClient(app)
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = app.state.mcp

    # 1. Seed a host workdir, then create a real deck.pptx + a slide.
    up = client.post("/files", files={"file": ("seed.docx", b"PK", "application/octet-stream")})
    assert up.status_code == 200
    seed_id = up.json()["file_id"]

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            r = await session.call_tool(
                "officecli_create", {"file_id": seed_id, "name": "deck.pptx", "type": "pptx"}
            )
            new_id = [c.text for c in r.content if hasattr(c, "text")][0].strip()
            assert not new_id.startswith("ERROR"), new_id

            await session.call_tool(
                "officecli_add", {"file_id": new_id, "selector": "/", "type": "slide"}
            )
            return new_id

    new_id = asyncio.run(run())

    # 2. Stage a real PNG into the deck's workdir.
    # Use a minimal valid PNG (1x1) generated here so the test is self-contained.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    stage = client.post(
        "/files/stage",
        data={"target_file_id": new_id, "filename": "kimi.png"},
        files={"file": ("kimi.png", png, "image/png")},
    )
    assert stage.status_code == 200, stage.text
    assert stage.json()["asset"] == "kimi.png"

    # 3. add picture with src=kimi.png and sizing/position.
    async def add_and_view():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            r = await session.call_tool(
                "officecli_add",
                {
                    "file_id": new_id,
                    "selector": "/slide[1]",
                    "type": "picture",
                    "prop": ["src=kimi.png", "width=5in", "height=3in", "x=1in", "y=1in"],
                },
            )
            assert not r.isError, [c.text for c in r.content if hasattr(c, "text")]
            # 4. view_screenshot -> image content returned (picture is on the slide).
            r2 = await session.call_tool(
                "officecli_view_screenshot", {"file_id": new_id, "page": 1}
            )
            imgs = [c for c in r2.content if getattr(c, "type", None) == "image"]
            assert imgs, f"expected a screenshot image, got {r2.content}"

    asyncio.run(add_and_view())

    # 5. Download the pptx and confirm the image part is embedded.
    dl = client.get(f"/files/{new_id}")
    assert dl.status_code == 200
    # A pptx with an embedded picture contains an image part (png) in the zip.
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(dl.content))
    names = z.namelist()
    assert any("media" in n and n.endswith(".png") for n in names), names
```

- [ ] **Step 2: Run the e2e test against the real binary**

Find the real binary (already located at `/tmp/officecli` during research). Run:

```bash
OFFICECLI_BIN=/tmp/officecli pytest tests/test_e2e_real.py::test_stage_image_into_pptx -v
```

Expected: PASS — the screenshot returns an image and the downloaded pptx contains a `ppt/media/*.png` part.

If `officecli add picture` rejects `src=kimi.png` (e.g. wants an absolute path or a different prop), inspect the error text from `r.content` and adjust the `prop` values in Step 3 of the test only — do NOT change `runner.run` behavior. The cwd is the workdir, so a relative name should resolve; if officecli instead needs the absolute workdir path, that is a runner-level finding to surface to the user (not silently worked around).

- [ ] **Step 3: Run the full e2e suite to confirm no regression**

Run: `OFFICECLI_BIN=/tmp/officecli pytest tests/test_e2e_real.py -v`
Expected: PASS (including pre-existing `test_create_view_html_screenshot_delete_flow`).

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_real.py
git commit -m "$(cat <<'EOF'
test(e2e): stage real PNG into pptx, add picture, verify embed

Definition-of-done for the stage action: image lands in the deck and is
embedded in the downloaded pptx.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Full suite + README/docs touch-up

**Files:**
- Modify: `README.md` (if it documents the file layer / tool list)
- Run: full test suite

- [ ] **Step 1: Run the entire non-e2e suite**

Run: `pytest -v --ignore=tests/test_e2e_real.py`
Expected: PASS (all of test_files, test_tools, test_runner, test_officecli_file, test_server, test_binary).

If any pre-existing test breaks due to the `prop` -> `list[str]` change or `path_for` tightening, fix the **test** to the new contract (e.g. `prop="bold=true"` -> `prop=["bold=true"]`; a test that relied on `path_for` returning a non-doc file must now seed a real doc). Do not weaken the implementation to suit an outdated test.

- [ ] **Step 2: Update README tool list / workflow diagram**

Check `README.md` for any enumeration of `officecli_file` actions or the `officecli_*` tool list. Add `stage` to the actions and `officecli_import` to the tool list; add a one-line note in the workflow that images/CSV are staged then referenced by `src=`/`source`. If README has no such list, skip (YAGNI).

- [ ] **Step 3: Commit docs**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs: document stage action + officecli_import in README

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

(If README needed no changes, skip the commit.)

---

## Self-Review Notes

**Spec coverage:** §1 architecture (Tasks 1,2,6), §2 shim stage (Task 6), §3.1 stage_asset+whitelist (Task 1), §3.2 path_for fix (Task 1), §3.3 endpoint (Task 2), §4.1 multi-prop (Task 3), §4.2 import (Task 4), §4.3 instructions (Task 5), §5.1 errors (covered in Tasks 1,2 tests: 404/415/413), §5.2 tests (Tasks 1-6), §5.3 verification (Task 7). All spec sections have a task.

**Type consistency:** `stage_asset(target_file_id, filename, data) -> {"asset","target"}` (Task 1) matches the endpoint return (Task 2) and the shim's `info.get("asset")` (Task 6). `_mcp_stage(target_file_id, filename, data)` (Task 6) matches the monkeypatch in its tests. `officecli_add`/`set` `prop: list[str] | None` (Task 3) matches the e2e `prop=[...]` (Task 7). `officecli_import(file_id, sheet, source, header, start_cell, format)` (Task 4) matches its test.

**Placeholder scan:** none — every step has complete code or exact commands.

**Known risk flagged inline:** Task 7 Step 2 notes that if officecli rejects a relative `src`, surface it rather than silently work around (the cwd-relative resolution is a core design assumption from §1).
