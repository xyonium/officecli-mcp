# Unified Native Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the OpenWebUI `officecli_file` native tool cover ALL officecli functionality by HTTP-calling the officecli-mcp container, with tool definitions living only server-side and the shim auto-synced into OpenWebUI — so users enable one feature instead of two.

**Architecture:** Server gains `GET /tools` (manifest from the FastMCP registry — single source of truth) and `POST /tools/call` (generic dispatch through FastMCP's request handlers, so behavior is byte-identical to the MCP path). The shim gains `action="run"`/`action="tools"`; its docstring embeds the full manifest, auto-regenerated and pushed to OpenWebUI's Tools API on container start. Screenshot output is downscaled server-side to bound model tokens.

**Tech Stack:** Python 3.12, FastMCP (`mcp[cli]>=1.27,<2`), Starlette, Pillow (new), requests (shim), pytest.

**Spec:** `docs/superpowers/specs/2026-07-20-unified-native-tool-design.md` — read it first.

## Global Constraints

- Never commit the officecli binary, `/data`, `/work`, or `.codegraph/`.
- TDD: failing test first, then implement. Every task ends green.
- Before EVERY commit run: `ruff check .` AND `pytest -q` (CI runs both; `ruff check` — not `ruff format`).
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- `Settings` is a frozen dataclass whose fields are read from env **at class-definition time** (`config.py`). Tests construct `Settings(...)` directly with kwargs (see `tests/conftest.py`) — new fields MUST have defaults so that fixture keeps working.
- New settings are passed through `build_app` → `build_mcp` with `getattr(settings, "field", default)` (existing pattern in `server.py:50-51`) so test fakes without the new fields still work.
- The shim (`examples/openwebui_officecli_file.py`) must remain a single self-contained paste-able file using only `anyio`, `requests`, `pydantic` (OpenWebUI tool sandbox limits). Every blocking `requests` call goes through `anyio.to_thread.run_sync` (OpenWebUI event-loop deadlock otherwise).
- Pillow (`Pillow>=10`) is added to `pyproject.toml` `dependencies` (not just dev) — the container needs it at runtime.

## File Structure

| File | Responsibility |
|---|---|
| `src/officecli_mcp/manifest.py` (new) | Build the tool manifest from the FastMCP registry: `get_manifest(mcp) -> dict`, `manifest_revision(tools) -> str`, `_compact_sig(schema) -> str` |
| `src/officecli_mcp/tools.py` | Add `screenshot_max_edge` kwarg to `build_mcp`; downscale in `officecli_view_screenshot` |
| `src/officecli_mcp/config.py` | New Settings fields (screenshot + OWUI sync) |
| `src/officecli_mcp/server.py` | `/tools` + `/tools/call` custom routes; invoke shim sync |
| `src/officecli_mcp/shim_template.py` (new) | Shim source template with `{REV}`/`{MANIFEST}`/`{INSTRUCTIONS}` placeholders |
| `src/officecli_mcp/shim.py` (new) | Render template → `examples/openwebui_officecli_file.py` (`render_shim`, `sync_example`, `SHIM_HEADER`) |
| `src/officecli_mcp/shim_sync.py` (new) | `sync_shim(settings, mcp)` — push rendered shim to OpenWebUI Tools API |
| `examples/openwebui_officecli_file.py` | Regenerated from template (adds run/tools actions) |
| `pyproject.toml` | Add `Pillow>=10` dependency |
| `tests/test_manifest.py` (new) | Manifest/revision/compact-sig tests |
| `tests/test_tools.py` | Screenshot downscale tests |
| `tests/test_tools_http.py` (new) | `/tools` + `/tools/call` route tests |
| `tests/test_shim.py` (new) | Template render + example parity tests |
| `tests/test_shim_sync.py` (new) | Sync flow tests (create/update/no-op/disabled) |
| `tests/test_shim_actions.py` (new) | Shim `run`/`tools` action tests (imports generated example) |
| `README.md` | Single-tool quick start; sync env vars |
| `docker-compose.yml` | New env var placeholders |

---

### Task 1: Manifest builder (`manifest.py`)

**Files:**
- Create: `src/officecli_mcp/manifest.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `build_mcp()` from `officecli_mcp.tools`; `create_connected_server_and_client_session` from `mcp.shared.memory` (existing test pattern in `tests/test_tools.py:5`).
- Produces: `async def get_manifest(mcp) -> dict` returning `{"revision": str, "instructions": str, "tools": [{"name": str, "description": str, "inputSchema": dict, "readOnly": bool, "signature": str}]}`; `def manifest_revision(tools: list[dict]) -> str`; `def _compact_sig(schema: dict) -> str`. Used by Task 4 (`/tools`), Task 5 (`shim.py`), Task 6 (`shim_sync.py`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the server-side tool manifest builder."""
from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session


@pytest.fixture
def mcp_server(settings, tmp_path):
    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho OK\n")
    stub.chmod(0o755)
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    return tools_mod.build_mcp(runner=runner, file_store=store)


async def test_manifest_contains_all_tools_with_full_descriptions(mcp_server):
    from officecli_mcp.manifest import get_manifest

    manifest = await get_manifest(mcp_server)
    names = {t["name"] for t in manifest["tools"]}
    assert "officecli_view_html" in names
    assert "officecli_batch" in names
    assert all(n.startswith("officecli_") for n in names)
    by_name = {t["name"]: t for t in manifest["tools"]}
    # Full description, not a summary: the batch tool's multi-paragraph
    # schema guidance must survive verbatim.
    assert "props" in by_name["officecli_batch"]["description"]
    assert "verbatim" in by_name["officecli_batch"]["description"]
    # Signature is a compact one-liner built from the input schema.
    assert by_name["officecli_set"]["signature"] == (
        "officecli_set(file_id: str, selector: str, prop?: list[str])"
    )
    # Read-only flags come from the ToolAnnotations.
    assert by_name["officecli_view_text"]["readOnly"] is True
    assert by_name["officecli_set"]["readOnly"] is False
    # Instructions are the FastMCP server instructions (workflow guidance).
    assert "file_id" in manifest["instructions"]


async def test_revision_changes_with_description(mcp_server):
    from officecli_mcp.manifest import get_manifest, manifest_revision

    manifest = await get_manifest(mcp_server)
    rev = manifest["revision"]
    assert len(rev) == 40  # sha1 hex
    mutated = [dict(t) for t in manifest["tools"]]
    mutated[0]["description"] += " changed"
    assert manifest_revision(mutated) != rev


def test_compact_sig_defaults_and_maps():
    from officecli_mcp.manifest import _compact_sig

    assert _compact_sig(
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "commands_json": {"type": "string"},
                "page": {"type": "integer", "default": None},
                "json": {"type": "boolean", "default": False},
            },
            "required": ["file_id", "commands_json"],
        }
    ) == "(file_id: str, commands_json: str, page?: int, json?: bool)"
    assert _compact_sig({"type": "object", "properties": {"x": {"type": "null"}}}) == "(x: Any)"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'officecli_mcp.manifest'`

- [ ] **Step 3: Implement `manifest.py`**

```python
"""Build the tool manifest from the FastMCP registry.

Single source of truth: the manifest is derived from the same FastMCP
instance that serves /mcp, so the shim's embedded docs can never drift from
the real tools. Consumed by the /tools endpoint (server.py), the shim
renderer (shim.py) and the OpenWebUI self-sync (shim_sync.py).
"""
from __future__ import annotations

import hashlib
import json
import logging

from mcp.shared.memory import create_connected_server_and_client_session

log = logging.getLogger(__name__)

_JSON_TYPE = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _compact_type(schema: dict) -> str:
    t = schema.get("type")
    if t == "array":
        inner = _JSON_TYPE.get((schema.get("items") or {}).get("type"), "Any")
        return f"list[{inner}]"
    return _JSON_TYPE.get(t, "Any")


def _compact_sig(schema: dict) -> str:
    """'(file_id: str, selector: str, prop?: list[str])' from an inputSchema."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = [
        f"{name}{'' if name in required else '?'}: {_compact_type(s)}"
        for name, s in props.items()
    ]
    return f"({', '.join(parts)})"


def manifest_revision(tools: list[dict]) -> str:
    """sha1 over the sorted (name, description, canonical schema) tuples."""
    h = hashlib.sha1()  # noqa: S324 - identity stamp, not security
    for t in sorted(tools, key=lambda x: x["name"]):
        h.update(t["name"].encode())
        h.update(b"\0")
        h.update((t.get("description") or "").encode())
        h.update(b"\0")
        h.update(
            json.dumps(t.get("inputSchema") or {}, sort_keys=True).encode()
        )
        h.update(b"\0")
    return h.hexdigest()


async def get_manifest(mcp) -> dict:
    """Return {revision, instructions, tools} for a built FastMCP instance."""
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.list_tools()
    tools = []
    for t in result.tools:
        schema = t.inputSchema or {}
        ann = t.annotations
        tools.append(
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": schema,
                "readOnly": bool(getattr(ann, "readOnlyHint", False)),
                "signature": f"{t.name}{_compact_sig(schema)}",
            }
        )
    return {
        "revision": manifest_revision(tools),
        "instructions": mcp.instructions or "",
        "tools": tools,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_manifest.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
ruff check . && pytest -q
git add src/officecli_mcp/manifest.py tests/test_manifest.py
git commit -m "feat: manifest builder deriving tool docs from the FastMCP registry

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Screenshot downscale in `officecli_view_screenshot`

**Files:**
- Modify: `src/officecli_mcp/tools.py:27-35` (build_mcp signature), `:125-135` (screenshot tool)
- Modify: `pyproject.toml:13-19` (add Pillow)
- Test: `tests/test_tools.py` (append)

**Interfaces:**
- Consumes: Pillow `PIL.Image`.
- Produces: `build_mcp(..., screenshot_max_edge: int = 1024)`; `_downscale_png(png: bytes, max_edge: int) -> bytes` (module-level, importable for tests). Task 3 passes the setting through; Task 4's `/tools/call` automatically returns downscaled images.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tools.py`; uses the existing `mcp_server` fixture there)

```python
def _make_png(width: int, height: int) -> bytes:
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _png_size(png: bytes) -> tuple[int, int]:
    import io

    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(png)) as im:
        return im.size


async def test_screenshot_downscales_to_max_edge(settings, tmp_path):
    """A 2000x1000 screenshot with max_edge=1024 comes back 1024x512."""
    import base64
    import os

    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    png = _make_png(2000, 1000)
    stub = tmp_path / "officecli"
    stub.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then shift; cp "$SCREENSHOT_SRC" "$1"; fi\n'
        "  shift\n"
        "done\n"
    )
    stub.chmod(0o755)
    src = tmp_path / "src.png"
    src.write_bytes(png)
    os.environ["SCREENSHOT_SRC"] = str(src)
    try:
        runner = OfficeRunner(binary_path=str(stub), file_store=store)
        mcp = tools_mod.build_mcp(runner=runner, file_store=store, screenshot_max_edge=1024)
        info = store.put("d.pptx", b"pptx-bytes")
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            res = await session.call_tool("officecli_view_screenshot", {"file_id": info["file_id"]})
        images = [c for c in res.content if getattr(c, "type", None) == "image"]
        assert images, f"expected an image content block, got {res.content!r}"
        assert _png_size(base64.b64decode(images[0].data)) == (1024, 512)
    finally:
        os.environ.pop("SCREENSHOT_SRC", None)


async def test_screenshot_max_edge_zero_disables_resize(settings, tmp_path):
    """max_edge=0 returns the original PNG bytes untouched."""
    import base64
    import os

    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    png = _make_png(2000, 1000)
    stub = tmp_path / "officecli"
    stub.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then shift; cp "$SCREENSHOT_SRC" "$1"; fi\n'
        "  shift\n"
        "done\n"
    )
    stub.chmod(0o755)
    src = tmp_path / "src.png"
    src.write_bytes(png)
    os.environ["SCREENSHOT_SRC"] = str(src)
    try:
        runner = OfficeRunner(binary_path=str(stub), file_store=store)
        mcp = tools_mod.build_mcp(runner=runner, file_store=store, screenshot_max_edge=0)
        info = store.put("d.pptx", b"pptx-bytes")
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            res = await session.call_tool("officecli_view_screenshot", {"file_id": info["file_id"]})
        images = [c for c in res.content if getattr(c, "type", None) == "image"]
        assert base64.b64decode(images[0].data) == png
    finally:
        os.environ.pop("SCREENSHOT_SRC", None)


def test_downscale_png_corrupt_input_returns_original():
    from officecli_mcp.tools import _downscale_png

    garbage = b"not-a-png"
    assert _downscale_png(garbage, 1024) == garbage
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tools.py -k screenshot -v`
Expected: FAIL — `build_mcp() got an unexpected keyword argument 'screenshot_max_edge'` / `ImportError: cannot import name '_downscale_png'`

- [ ] **Step 3: Implement**

In `pyproject.toml` dependencies, add `"Pillow>=10",` after `"pydantic>=2.7",`.

In `src/officecli_mcp/tools.py`:

1. Add `"screenshot"` to the imports-from-nothing — actually just add to the top: `import base64`, `import binascii`, `import io` are not needed at top level; keep them function-local inside `_downscale_png` (Pillow import stays lazy so a missing Pillow can't break unrelated tools). Add this module-level helper after `_err`:

```python
def _downscale_png(png: bytes, max_edge: int) -> bytes:
    """Clamp a PNG's longest edge to max_edge px (aspect preserved).

    Screenshots go straight into the model context as base64, so unbounded
    resolution = unbounded tokens. max_edge=0 disables resizing. A corrupt
    image is returned unchanged (logged) rather than failing the tool call.
    """
    if max_edge <= 0:
        return png
    import io

    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(png)) as im:
            w, h = im.size
            if max(w, h) <= max_edge:
                return png
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        log.warning("screenshot downscale failed; returning original image", exc_info=True)
        return png
```

2. Change `build_mcp` signature (line 27-35) to add `screenshot_max_edge: int = 1024` after `view_html_max_chars`.

3. In `officecli_view_screenshot`, replace `png = runner.read_image(res.image_path)` with:

```python
        png = _downscale_png(runner.read_image(res.image_path), screenshot_max_edge)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tools.py -v`
Expected: all pass (including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
ruff check . && pytest -q
git add pyproject.toml src/officecli_mcp/tools.py tests/test_tools.py
git commit -m "feat: downscale screenshots to OFFICECLI_MCP_SCREENSHOT_MAX_EDGE (default 1024px)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: New Settings fields

**Files:**
- Modify: `src/officecli_mcp/config.py:41-63`
- Modify: `src/officecli_mcp/server.py:45-52`
- Test: `tests/test_config.py` (append; create with the existing import style if absent)

**Interfaces:**
- Consumes: `_env_int`, `_env_bool` in `config.py`.
- Produces: `Settings.screenshot_max_edge: int` (default 1024), `Settings.owui_sync: bool` (default True), `Settings.owui_url: str` (default ""), `Settings.owui_api_key: str` (default ""), `Settings.owui_tool_id: str` (default "officecli_file"). Consumed by Task 4 (`server.py` routes) and Task 6 (`shim_sync.sync_shim(settings, mcp)`).

- [ ] **Step 1: Write the failing test** (append to `tests/test_config.py` — if the file does not exist, create it with `from __future__ import annotations` at top)

```python
def test_new_settings_defaults(monkeypatch):
    for var in (
        "OFFICECLI_MCP_SCREENSHOT_MAX_EDGE",
        "OFFICECLI_MCP_OWUI_SYNC",
        "OFFICECLI_MCP_OWUI_URL",
        "OFFICECLI_MCP_OWUI_API_KEY",
        "OFFICECLI_MCP_OWUI_TOOL_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    import importlib

    import officecli_mcp.config as cfg

    importlib.reload(cfg)
    s = cfg.Settings()
    assert s.screenshot_max_edge == 1024
    assert s.owui_sync is True
    assert s.owui_url == ""
    assert s.owui_api_key == ""
    assert s.owui_tool_id == "officecli_file"


def test_new_settings_from_env(monkeypatch):
    monkeypatch.setenv("OFFICECLI_MCP_SCREENSHOT_MAX_EDGE", "512")
    monkeypatch.setenv("OFFICECLI_MCP_OWUI_SYNC", "0")
    monkeypatch.setenv("OFFICECLI_MCP_OWUI_URL", "http://open-webui:8080")
    monkeypatch.setenv("OFFICECLI_MCP_OWUI_API_KEY", "sk-test")
    import importlib

    import officecli_mcp.config as cfg

    importlib.reload(cfg)
    s = cfg.Settings()
    assert s.screenshot_max_edge == 512
    assert s.owui_sync is False
    assert s.owui_url == "http://open-webui:8080"
    assert s.owui_api_key == "sk-test"
```

NOTE: `Settings` reads env at class-definition time, so these tests must `importlib.reload(cfg)` after monkeypatching env. Reload in both tests leaves the module with the last monkeypatched state undone — add `importlib.reload(cfg)` one more time at the end of each test (outside assertions) so later tests see a clean module. Alternatively put the reload in a `try/finally`. Use this pattern in both tests:

```python
    try:
        importlib.reload(cfg)
        s = cfg.Settings()
        ...
    finally:
        importlib.reload(cfg)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -k new_settings -v`
Expected: FAIL — `TypeError: Settings.__init__() got an unexpected keyword argument` … actually `AttributeError: 'Settings' object has no attribute 'screenshot_max_edge'`

- [ ] **Step 3: Implement**

In `config.py`, add to the `Settings` dataclass after `view_html_max_chars` (line 60):

```python
    # Screenshots enter the model context as base64; clamp the longest edge
    # (px, aspect preserved) to bound tokens. 0 disables resizing.
    screenshot_max_edge: int = _env_int("OFFICECLI_MCP_SCREENSHOT_MAX_EDGE", 1024)
    # Self-sync of the officecli_file shim into OpenWebUI's Tools API on boot.
    # owui_sync=0 or missing url/key disables it (manual paste mode).
    owui_sync: bool = _env_bool("OFFICECLI_MCP_OWUI_SYNC", True)
    owui_url: str = os.environ.get("OFFICECLI_MCP_OWUI_URL", "")
    owui_api_key: str = os.environ.get("OFFICECLI_MCP_OWUI_API_KEY", "")
    owui_tool_id: str = os.environ.get("OFFICECLI_MCP_OWUI_TOOL_ID", "officecli_file")
```

In `server.py` `build_app`, add to the `build_mcp(...)` call after `view_html_max_chars`:

```python
        screenshot_max_edge=getattr(settings, "screenshot_max_edge", 1024),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py tests/test_tools.py -v`
Expected: all pass (conf.py reloads + existing tools tests unaffected by the new kwarg default)

- [ ] **Step 5: Commit**

```bash
ruff check . && pytest -q
git add src/officecli_mcp/config.py src/officecli_mcp/server.py tests/test_config.py
git commit -m "feat: settings for screenshot max edge and OpenWebUI shim self-sync

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `/tools` and `/tools/call` HTTP routes

**Files:**
- Modify: `src/officecli_mcp/server.py:79-89` (after the `/health` route)
- Test: `tests/test_tools_http.py` (new)

**Interfaces:**
- Consumes: `manifest.get_manifest(mcp)` (Task 1); `mcp.request_handlers` from `mcp.types` (FastMCP lowlevel: `mcp.request_handlers[types.ListToolsRequest]` / `[types.CallToolRequest]`); `_check_api_key(request, settings.api_key)` from `officecli_mcp.files:128`.
- Produces: `GET /tools` → manifest JSON (Task 1 shape); `POST /tools/call` body `{"name": str, "arguments": dict}` → `{"content": [...], "isError": bool}` where content blocks are `{"type":"text","text":...}` or `{"type":"image","data":<base64>,"mimeType":"image/png"}`. The shim's `run`/`tools` actions (Task 7) call these.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the generic /tools manifest and /tools/call dispatch routes."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def app_client(settings, tmp_path, monkeypatch):
    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho 'CLI-OUT'\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    app = server_mod.build_app(settings)
    return TestClient(app), app


def test_tools_endpoint_returns_manifest(app_client):
    client, _ = app_client
    resp = client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["tools"]}
    assert "officecli_view_text" in names
    assert "officecli_create" in names
    assert len(body["revision"]) == 40
    assert "file_id" in body["instructions"]
    sig = {t["name"]: t["signature"] for t in body["tools"]}
    assert sig["officecli_set"] == "officecli_set(file_id: str, selector: str, prop?: list[str])"


def test_tools_call_dispatches_and_returns_text(app_client):
    client, app = app_client
    store = app.state.file_store
    info = store.put("r.docx", b"docx-bytes")
    resp = client.post(
        "/tools/call",
        json={"name": "officecli_view_text", "arguments": {"file_id": info["file_id"]}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["isError"] is False
    assert body["content"][0]["type"] == "text"
    assert "CLI-OUT" in body["content"][0]["text"]


def test_tools_call_unknown_tool_404(app_client):
    client, _ = app_client
    resp = client.post("/tools/call", json={"name": "officecli_nope", "arguments": {}})
    assert resp.status_code == 404


def test_tools_call_bad_file_id_returns_iserror(app_client):
    client, _ = app_client
    resp = client.post(
        "/tools/call",
        json={"name": "officecli_view_text", "arguments": {"file_id": "ghost"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["isError"] is True
    assert "not found" in body["content"][0]["text"]


def test_tools_call_invalid_arguments_returns_iserror(app_client):
    """Missing required arg -> validation error surfaces as isError, not 500."""
    client, app = app_client
    store = app.state.file_store
    info = store.put("r.docx", b"docx-bytes")
    resp = client.post(
        "/tools/call",
        json={"name": "officecli_set", "arguments": {"file_id": info["file_id"]}},
    )
    assert resp.status_code == 200
    assert resp.json()["isError"] is True


def test_tools_endpoints_respect_api_key(settings, tmp_path, monkeypatch):
    from dataclasses import replace

    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho OK\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    keyed = replace(settings, api_key="secret")
    app = server_mod.build_app(keyed)
    client = TestClient(app)
    assert client.get("/tools").status_code == 401
    assert client.post("/tools/call", json={"name": "x", "arguments": {}}).status_code == 401
    assert client.get("/tools", headers={"Authorization": "Bearer secret"}).status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tools_http.py -v`
Expected: FAIL — 404/405 on `/tools` (route missing)

- [ ] **Step 3: Implement**

In `server.py`, add after the `/health` custom_route (before `app = mcp.streamable_http_app()`):

```python
    # Generic manifest + dispatch so the OpenWebUI native tool can drive every
    # officecli_* tool over plain HTTP. These go through the SAME FastMCP
    # request handlers as /mcp, so behavior (validation, errors, content
    # blocks) is identical to the MCP path.
    import mcp.types as mcp_types

    from officecli_mcp.files import _check_api_key
    from officecli_mcp.manifest import get_manifest

    @mcp.custom_route("/tools", methods=["GET"])
    async def _tools_manifest(request):
        err = _check_api_key(request, settings.api_key)
        if err:
            return err
        from starlette.responses import JSONResponse

        return JSONResponse(await get_manifest(mcp))

    @mcp.custom_route("/tools/call", methods=["POST"])
    async def _tools_call(request):
        err = _check_api_key(request, settings.api_key)
        if err:
            return err
        import base64 as _b64

        from starlette.responses import JSONResponse

        try:
            payload = await request.json()
            name = payload["name"]
            arguments = payload.get("arguments") or {}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

        list_handler = mcp.request_handlers[mcp_types.ListToolsRequest]
        listed = await list_handler(mcp_types.ListToolsRequest())
        if name not in {t.name for t in listed.tools}:
            return JSONResponse({"error": f"unknown tool '{name}'"}, status_code=404)

        call_handler = mcp.request_handlers[mcp_types.CallToolRequest]
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        try:
            result = await call_handler(req)
        except Exception as e:  # noqa: BLE001
            # Validation/execution errors surface as content, not HTTP errors,
            # so the model can read and self-correct (same as the MCP path).
            return JSONResponse(
                {"content": [{"type": "text", "text": str(e)}], "isError": True}
            )
        if isinstance(result, mcp_types.ServerResult):
            result = result.root
        content = []
        for block in getattr(result, "content", []) or []:
            if getattr(block, "type", None) == "image":
                content.append(
                    {
                        "type": "image",
                        "data": block.data if isinstance(block.data, str) else _b64.b64encode(block.data).decode(),
                        "mimeType": getattr(block, "mimeType", "image/png"),
                    }
                )
            elif hasattr(block, "text"):
                content.append({"type": "text", "text": block.text})
        return JSONResponse({"content": content, "isError": bool(getattr(result, "isError", False))})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tools_http.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
ruff check . && pytest -q
git add src/officecli_mcp/server.py tests/test_tools_http.py
git commit -m "feat: /tools manifest + /tools/call generic dispatch over plain HTTP

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Shim template + renderer (`shim_template.py`, `shim.py`)

**Files:**
- Create: `src/officecli_mcp/shim_template.py`
- Create: `src/officecli_mcp/shim.py`
- Modify: `examples/openwebui_officecli_file.py` (regenerated — do NOT hand-edit after this task)
- Test: `tests/test_shim.py` (new)

**Interfaces:**
- Consumes: manifest dict shape from Task 1 (`revision`, `instructions`, `tools[].signature/description`).
- Produces: `shim.SHIM_HEADER = "# officecli-shim-rev: "`; `shim.render_shim(manifest: dict) -> str`; `shim.sync_example(manifest: dict) -> None` (renders + writes `examples/openwebui_officecli_file.py`). Consumed by Task 6 (`shim_sync`) and by `python -m officecli_mcp.shim` for manual regeneration.

This is the biggest task. The template is the CURRENT shim with: (a) the module docstring replaced by an auto-generated one, (b) two new params (`tool`, `arguments`) + two new actions (`run`, `tools`) + one helper (`_mcp_call`), (c) a revision header line.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for shim rendering and template/example parity."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fake_manifest() -> dict:
    return {
        "revision": "ab" * 20,
        "instructions": "WORKFLOW: upload first.",
        "tools": [
            {
                "name": "officecli_view_text",
                "description": "Plain text of the document. Read-only.",
                "inputSchema": {},
                "readOnly": True,
                "signature": "officecli_view_text(file_id: str, page?: int)",
            },
            {
                "name": "officecli_set",
                "description": 'Set properties. Quote "test" intact.',
                "inputSchema": {},
                "readOnly": False,
                "signature": "officecli_set(file_id: str, selector: str, prop?: list[str])",
            },
        ],
    }


def test_render_embeds_revision_manifest_and_instructions():
    from officecli_mcp.shim import render_shim

    src = render_shim(_fake_manifest())
    assert src.splitlines()[0] == f"# officecli-shim-rev: {'ab' * 20}"
    assert "WORKFLOW: upload first." in src
    # Full descriptions embedded verbatim (quotes escaped inside the docstring).
    assert "officecli_view_text(file_id: str, page?: int)" in src
    assert "Plain text of the document. Read-only." in src
    assert "officecli_set(file_id: str, selector: str, prop?: list[str])" in src
    # The generic actions exist.
    assert 'if action == "run":' in src
    assert 'if action == "tools":' in src
    assert "_mcp_call" in src


def test_render_output_is_valid_python():
    from officecli_mcp.shim import render_shim

    src = render_shim(_fake_manifest())
    compile(src, "shim.py", "exec")  # raises SyntaxError if invalid


def test_example_matches_rendered_template():
    """examples/openwebui_officecli_file.py must equal the template rendered
    with the CURRENT server manifest. Regenerate with:
        python -m officecli_mcp.shim
    """
    import anyio

    from officecli_mcp.shim import render_shim
    from tests.test_shim_helpers import live_manifest  # local helper below

    expected = render_shim(anyio.run(live_manifest))
    actual = (ROOT / "examples" / "openwebui_officecli_file.py").read_text()
    assert actual == expected, (
        "example shim is stale; run: python -m officecli_mcp.shim"
    )


def test_example_starts_with_revision_header():
    from officecli_mcp.shim import SHIM_HEADER

    first = (ROOT / "examples" / "openwebui_officecli_file.py").read_text().splitlines()[0]
    assert first.startswith(SHIM_HEADER)
    assert re.fullmatch(r"# officecli-shim-rev: [0-9a-f]{40}", first)
```

Create the helper `tests/test_shim_helpers.py`:

```python
"""Build the manifest from a stub-backed FastMCP instance (no real binary)."""
from __future__ import annotations


async def live_manifest(tmp_work: str | None = None) -> dict:
    import tempfile

    from officecli_mcp.files import FileStore
    from officecli_mcp.manifest import get_manifest
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.tools import build_mcp

    work = tmp_work or tempfile.mkdtemp(prefix="shim-test-")
    store = FileStore(work_dir=work, ttl_seconds=3600)
    runner = OfficeRunner(binary_path="/bin/true", file_store=store)
    mcp = build_mcp(runner=runner, file_store=store)
    return await get_manifest(mcp)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'officecli_mcp.shim'`

- [ ] **Step 3: Implement**

**`src/officecli_mcp/shim_template.py`** — the template. Placeholders: `{REV}`, `{INSTRUCTIONS}`, `{MANIFEST}` (filled by `str.replace`, NOT `.format`, so the shim's own braces survive untouched). The body below is the current shim with the new pieces; everything in `__SHIM_BODY__` is verbatim shim code except the three placeholders:

```python
"""The officecli_file OpenWebUI tool as a template.

Placeholders {REV} {INSTRUCTIONS} {MANIFEST} are filled by shim.render_shim
via str.replace (never .format - the shim code itself contains braces).
The rendered output is written to examples/openwebui_officecli_file.py and,
when self-sync is enabled, pushed into OpenWebUI's Tools API.
"""
from __future__ import annotations

TEMPLATE = '''# officecli-shim-rev: {REV}
"""OpenWebUI native Tool: ALL officecli document operations through officecli-mcp.

THIS FILE IS AUTO-GENERATED by officecli-mcp (shim.py) from the server's live
tool manifest. Do not hand-edit; regenerate with `python -m officecli_mcp.shim`
or let the container self-sync it into OpenWebUI on boot.

ONE tool does everything - no MCP connection needed:
  - action="upload":   push chat-attached office docs INTO officecli-mcp -> file_id.
  - action="download": pull a finished doc OUT -> downloadable chip on the message.
  - action="stage":    drop an image/CSV asset into a document's workdir -> asset name.
  - action="run":      call ANY document tool below (view/edit/create/...).
  - action="tools":    fetch the live tool manifest (fallback if this file is stale).

WORKFLOW (from the server):
{INSTRUCTIONS}

AVAILABLE TOOLS for action="run" (full docs; auto-synced from the server):
{MANIFEST}

HOW TO CALL action="run": pass tool=<name> and arguments=<JSON object as a
STRING>. Example: officecli_file(action="run", tool="officecli_set",
arguments="{{\\"file_id\\":\\"abc123\\",\\"selector\\":\\"/slide[1]/shape[1]\\",\\"prop\\":[\\"x=2cm\\",\\"width=21cm\\"]}}")

Auth model: file actions use the CURRENT user's credentials forwarded from the
injected __request__ (no stored key; works as a shared Public tool).

Install: Workspace > Tools > paste this file. Set Valves:
  - officecli_mcp_url:       internal officecli-mcp base, e.g. http://officecli-mcp:8765
  - openwebui_url:           internal OpenWebUI base used for API calls, e.g. http://open-webui:8080
  - openwebui_browser_url:   browser-reachable OpenWebUI base for returned download URLs,
                             e.g. https://openwebui.example.com (default "" -> falls back to openwebui_url)
"""
from __future__ import annotations

import json
from typing import Any

import anyio
import requests
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        # Pydantic BaseModel (NOT a plain class) so OpenWebUI can call
        # Valves.schema() to render the Valves editor and Valves(**form_data)
        # to apply saved values. A plain class with __init__ has no .schema()
        # and crashes GET /api/v1/tools/id/<id>/valves/spec with 500.
        officecli_mcp_url: str = "http://officecli-mcp:8765"
        openwebui_url: str = "http://open-webui:8080"
        openwebui_browser_url: str = ""  # browser-reachable OWUI base; "" -> openwebui_url

    def __init__(self):
        self.valves = self.Valves()

    # --- swappable HTTP helpers (monkeypatched in tests) ---
    def _owui_headers(self, __request__: Any) -> dict[str, str]:
        """Forward the current user's credentials so we touch only their files."""
        headers: dict[str, str] = {}
        try:
            auth = __request__.headers.get("authorization")
            if auth:
                headers["Authorization"] = auth
            cookie = __request__.headers.get("cookie")
            if cookie:
                headers["Cookie"] = cookie
        except Exception:
            pass
        return headers

    def _owui_get(self, file_id: str, __request__: Any) -> bytes:
        """Fetch an attached file's bytes from OpenWebUI (upload action)."""
        url = f"{self.valves.openwebui_url}/api/v1/files/{file_id}/content"
        resp = requests.get(url, headers=self._owui_headers(__request__), timeout=60)
        resp.raise_for_status()
        return resp.content

    def _mcp_post(self, filename: str, data: bytes) -> dict:
        """Push bytes into officecli-mcp /files (upload action) -> file_id."""
        url = f"{self.valves.officecli_mcp_url}/files"
        files = {"file": (filename, data, "application/octet-stream")}
        resp = requests.post(url, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def _mcp_stage(self, target_file_id: str, filename: str, data: bytes) -> dict:
        """Push an asset into officecli-mcp /files/stage (stage action) -> asset name."""
        url = f"{self.valves.officecli_mcp_url}/files/stage"
        files = {"file": (filename, data, "application/octet-stream")}
        data_field = {"target_file_id": target_file_id}
        resp = requests.post(url, data=data_field, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def _mcp_get(self, file_id: str) -> requests.Response:
        """Pull a finished file's bytes from officecli-mcp /files/{id} (download)."""
        url = f"{self.valves.officecli_mcp_url}/files/{file_id}"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        return resp

    def _mcp_call(self, path: str, payload: dict | None = None) -> dict:
        """GET/POST a JSON endpoint on officecli-mcp (run/tools actions)."""
        url = f"{self.valves.officecli_mcp_url}{path}"
        if payload is None:
            resp = requests.get(url, timeout=60)
        else:
            resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()

    def _owui_post(self, filename: str, data: bytes, mime: str, __request__: Any) -> dict:
        """POST bytes into OpenWebUI storage (download action) -> owui file id."""
        url = f"{self.valves.openwebui_url}/api/v1/files/?process=false"
        files = {"file": (filename, data, mime)}
        resp = requests.post(
            url, headers=self._owui_headers(__request__), files=files, timeout=120
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _resolve_attached(f: dict[str, Any], fallback_name: str = "asset.bin") -> tuple[str, str]:
        """Extract (file_id, name) from a single __files__ entry dict."""
        file_id = f.get("id") or (f.get("file") or {}).get("id")
        name = f.get("name") or f.get("filename") or fallback_name
        return file_id, name

    @staticmethod
    def _filename_from_disposition(resp: requests.Response, fallback: str) -> str:
        """Pull the filename out of a content-disposition header, else fallback."""
        cd = resp.headers.get("content-disposition", "")
        # attachment; filename="Kimi_K3.pptx"
        for part in cd.split(";"):
            part = part.strip()
            if part.lower().startswith("filename="):
                name = part.split("=", 1)[1].strip().strip('"')
                if name:
                    return name
        return fallback or "download.docx"

    @staticmethod
    def _infer_asset_name(filename: str, data: bytes) -> str:
        """Pick a stgable asset filename, inferring the extension from image
        magic bytes when the caller gave no usable filename.

        The STAGE_EXT whitelist rejects unknown extensions (e.g. a bare
        'asset.bin'), so when the model omits filename (common for generated
        images, which only have an OpenWebUI file id) we MUST derive a real
        extension from the bytes themselves. PNG/JPEG/GIF/WebP are detected by
        magic bytes; SVG by leading text. Returns a basename like 'asset.png'.
        """
        if filename and "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
            if ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "csv", "tsv"}:
                return filename
        ext = Tools._infer_ext(data)
        if not ext:
            raise ValueError(
                "could not infer asset extension from bytes and no filename given; "
                "pass filename= with a stgable extension (png/jpg/gif/webp/bmp/svg/csv/tsv)"
            )
        return f"asset.{ext}"

    @staticmethod
    def _infer_ext(data: bytes) -> str:
        """Sniff the image/asset extension from magic bytes, or '' if unknown."""
        if data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
            return "png"
        if data.startswith(b"\\xff\\xd8\\xff"):
            return "jpg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "gif"
        if data.startswith(b"RIFF") and len(data) > 11 and data[8:12] == b"WEBP":
            return "webp"
        if data.startswith(b"BM"):
            return "bmp"
        stripped = data.lstrip()
        if stripped.startswith(b"<svg") or b"<svg" in data[:512]:
            return "svg"
        return ""

    async def officecli_file(
        self,
        action: str,
        __files__: list[dict[str, Any]] = [],  # noqa: B006
        __request__: Any = None,
        __event_emitter__: Any = None,
        file_id: str = "",
        filename: str = "",
        source_file_id: str = "",
        tool: str = "",
        arguments: str = "",
    ) -> str:
        """Drive officecli-mcp by handle: file actions + generic tool dispatch.

        Must be `async` and offload every blocking `requests` call to a worker
        thread via `anyio.to_thread.run_sync`. OpenWebUI runs sync tool methods
        directly in its single uvicorn event loop (utils/tools.py:228), so a
        blocking HTTP call back to OpenWebUI itself (download's storage POST)
        would deadlock the only worker until the 120s read timeout. `async def`
        takes the `iscoroutinefunction` branch which `await`s us, and the
        thread offload keeps the loop free to service the self-call.

        Args:
            action: "upload" | "download" | "stage" | "run" | "tools".
            __files__: upload/stage only - OpenWebUI-injected attached-file dicts.
            __request__: OpenWebUI-injected FastAPI Request; its Authorization/cookie
                are forwarded so we act as the current user (no stored key).
            __event_emitter__: download only - emits a {type:"files"} event so
                OpenWebUI renders a downloadable FileItem chip on the message.
            file_id: download/stage - the officecli-mcp file_id to fetch/target.
            filename: download/stage, optional - override saved filename / asset name.
            source_file_id: stage only - OpenWebUI file id of a generated image.
            tool: run only - a tool name from the module docstring list, e.g.
                "officecli_set".
            arguments: run only - the tool's arguments as a JSON OBJECT STRING,
                e.g. '{"file_id":"abc","selector":"/slide[1]","prop":["x=2cm"]}'.
                Not a dict - OpenWebUI builds this tool's schema from this
                signature, so nested objects must arrive as a JSON string.

        Returns:
            JSON string. run returns {"content":[{"type":"text","text":...}],
            "isError":bool} flattened: text blocks joined; image blocks become
            {"type":"image","data":"data:image/png;base64,..."} entries.
        """
        if action == "upload":
            return await self._upload(__files__, __request__)
        if action == "download":
            return await self._download(
                file_id, filename, __request__, __event_emitter__
            )
        if action == "stage":
            return await self._stage(
                file_id, filename, source_file_id, __files__, __request__
            )
        if action == "run":
            return await self._run(tool, arguments)
        if action == "tools":
            return await self._tools()
        return json.dumps({"error": f"unknown action '{action}'"})

    async def _run(self, tool: str, arguments: str) -> str:
        if not tool:
            return json.dumps({"error": "tool required (see tool list in description)"})
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"arguments must be a JSON object string: {e}"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must decode to a JSON object"})
        try:
            body = await anyio.to_thread.run_sync(
                self._mcp_call, "/tools/call", {"name": tool, "arguments": args}
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return json.dumps(
                    {"error": f"unknown tool '{tool}'; call action=\\"tools\\" for the live list"}
                )
            return json.dumps({"error": f"officecli-mcp call failed: {e}"})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"officecli-mcp call failed: {e}"})
        out = []
        for block in body.get("content", []):
            if block.get("type") == "image":
                out.append(
                    {
                        "type": "image",
                        "data": f"data:{block.get('mimeType', 'image/png')};base64,{block.get('data', '')}",
                        "note": "rendered screenshot (downscaled server-side)",
                    }
                )
            else:
                out.append({"type": "text", "text": block.get("text", "")})
        return json.dumps({"content": out, "isError": bool(body.get("isError"))})

    async def _tools(self) -> str:
        try:
            return json.dumps(await anyio.to_thread.run_sync(self._mcp_call, "/tools"))
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"officecli-mcp tools fetch failed: {e}"})

    async def _upload(self, __files__: list[dict[str, Any]], __request__: Any) -> str:
        if not __files__:
            return json.dumps({"error": "no files attached"})
        out = []
        for f in __files__:
            file_id, name = self._resolve_attached(f, "upload.docx")
            if not file_id:
                continue
            try:
                data = await anyio.to_thread.run_sync(self._owui_get, file_id, __request__)
                info = await anyio.to_thread.run_sync(self._mcp_post, name, data)
                out.append({"file_id": info["file_id"], "filename": info.get("filename", name)})
            except Exception as e:  # noqa: BLE001
                out.append({"filename": name, "error": str(e)})
        return json.dumps(
            {
                "files": out,
                "hint": "Pass each file_id to action=\\"run\\" tools (e.g. officecli_view_text).",
            }
        )

    async def _download(
        self,
        file_id: str,
        filename: str,
        __request__: Any,
        __event_emitter__: Any = None,
    ) -> str:
        if not file_id:
            return json.dumps({"error": "file_id required"})
        try:
            resp = await anyio.to_thread.run_sync(self._mcp_get, file_id)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return json.dumps(
                    {
                        "error": (
                            "file_id not found or expired (TTL ~1h); re-create the "
                            "document or increase OFFICECLI_MCP_WORK_TTL_SECONDS"
                        )
                    }
                )
            return json.dumps({"error": f"officecli-mcp fetch failed: {e}"})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"officecli-mcp fetch failed: {e}"})

        data = resp.content
        name = filename or self._filename_from_disposition(resp, "download.docx")
        mime = "application/octet-stream"

        try:
            info = await anyio.to_thread.run_sync(self._owui_post, name, data, mime, __request__)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"openwebui upload failed: {e}"})

        owui_id = info.get("id")
        if not owui_id:
            return json.dumps({"error": f"openwebui upload returned no id: {info}"})
        base = self.valves.openwebui_browser_url or self.valves.openwebui_url
        # The FileItem chip component appends '/content' itself
        # (FileItem.svelte: window.open(`${url}/content`)), so the chip url is
        # the bare file base. The JSON url keeps '/content' for the model to
        # print as a text link. Emitting .../content would open
        # .../content/content -> 404.
        chip_url = f"{base}/api/v1/files/{owui_id}"
        content_url = f"{chip_url}/content"

        if __event_emitter__ is not None:
            try:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "file",
                                    "url": chip_url,
                                    "name": name,
                                    "size": len(data),
                                }
                            ]
                        },
                    }
                )
            except Exception:  # noqa: BLE001
                # A failed chip event must not break the download result the
                # model depends on; the JSON url is still returned below.
                pass

        return json.dumps({"url": content_url, "filename": name, "size": len(data)})

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
            fid, name = self._resolve_attached(files[0], "asset.bin")
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
        try:
            name = Tools._infer_asset_name(filename or fallback_name, data)
        except ValueError as e:
            return json.dumps({"error": str(e)})
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
'''
```

IMPORTANT template detail — the byte-literal escapes: inside the triple-single-quoted `TEMPLATE` string, sequences like `b"\x89PNG\r\n\x1a\n"` must be written as `b"\\x89PNG\\r\\n\\x1a\\n"` so the RENDERED shim contains the original `\x89` escape (as shown above). Same for the `\\"` sequences inside docstring examples. After writing the file, sanity check: `python -c "from officecli_mcp.shim_template import TEMPLATE; compile(TEMPLATE.replace('{REV}','x'*40).replace('{INSTRUCTIONS}','i').replace('{MANIFEST}','m'),'t','exec')"`.

**`src/officecli_mcp/shim.py`**:

```python
"""Render the officecli_file shim from the live manifest; keep the example in sync."""
from __future__ import annotations

import logging
from pathlib import Path

from officecli_mcp.shim_template import TEMPLATE

log = logging.getLogger(__name__)

SHIM_HEADER = "# officecli-shim-rev: "
EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "examples" / "openwebui_officecli_file.py"


def _manifest_section(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        lines.append(f"* {t['signature']}")
        desc = (t.get("description") or "").strip()
        if desc:
            lines.extend(f"    {line}" for line in desc.splitlines())
    return "\n".join(lines)


def render_shim(manifest: dict) -> str:
    """Fill the template with the manifest. str.replace, never .format."""
    return (
        TEMPLATE.replace("{REV}", manifest["revision"])
        .replace("{INSTRUCTIONS}", manifest.get("instructions", ""))
        .replace("{MANIFEST}", _manifest_section(manifest.get("tools", [])))
    )


def sync_example(manifest: dict) -> None:
    """Regenerate examples/openwebui_officecli_file.py from the template."""
    EXAMPLE_PATH.write_text(render_shim(manifest))
    log.info("regenerated %s", EXAMPLE_PATH)


async def _amain() -> None:
    import tempfile

    from officecli_mcp.files import FileStore
    from officecli_mcp.manifest import get_manifest
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.tools import build_mcp

    store = FileStore(work_dir=tempfile.mkdtemp(prefix="shim-gen-"), ttl_seconds=3600)
    runner = OfficeRunner(binary_path="/bin/true", file_store=store)
    mcp = build_mcp(runner=runner, file_store=store)
    sync_example(await get_manifest(mcp))


if __name__ == "__main__":
    import anyio

    anyio.run(_amain)
```

- [ ] **Step 4: Regenerate the example and run tests**

```bash
python -m officecli_mcp.shim
pytest tests/test_shim.py -v
```
Expected: 4 passed (parity test passes because we just regenerated)

- [ ] **Step 5: Run the FULL suite** — the regenerated shim now has new params; existing shim tests (`tests/test_officecli_file.py`) must still pass since upload/download/stage behavior is unchanged.

Run: `ruff check . && pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/officecli_mcp/shim_template.py src/officecli_mcp/shim.py examples/openwebui_officecli_file.py tests/test_shim.py tests/test_shim_helpers.py
git commit -m "feat: shim template + renderer; example shim gains run/tools actions

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Shim self-sync (`shim_sync.py`)

**Files:**
- Create: `src/officecli_mcp/shim_sync.py`
- Modify: `src/officecli_mcp/server.py` (invoke sync in `build_app`)
- Test: `tests/test_shim_sync.py` (new)

**Interfaces:**
- Consumes: `Settings.owui_sync/owui_url/owui_api_key/owui_tool_id` (Task 3); `manifest.get_manifest` (Task 1); `shim.render_shim`, `shim.SHIM_HEADER` (Task 5); `httpx` (already a dependency).
- Produces: `async def sync_shim(settings, mcp) -> None` — never raises; logs and returns on any failure. Called from `build_app` (fire-and-forget via `anyio` task or direct await inside the app lifespan — see step 3 note).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the OpenWebUI shim self-sync (container boot -> Tools API)."""
from __future__ import annotations

import httpx
import pytest


def _settings(settings, **kw):
    from dataclasses import replace

    base = replace(settings, owui_url="http://owui.test", owui_api_key="sk-admin")
    return replace(base, **kw) if kw else base


@pytest.fixture
def mcp_server(settings, tmp_path):
    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho OK\n")
    stub.chmod(0o755)
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    return tools_mod.build_mcp(runner=runner, file_store=store)


def _transport(rec: list[dict], *, existing: dict | None = None):
    """An httpx MockTransport emulating the OpenWebUI Tools API."""

    def handler(request: httpx.Request) -> httpx.Response:
        rec.append({"method": request.method, "url": str(request.url), "body": request.read()})
        assert request.headers.get("authorization") == "Bearer sk-admin"
        if request.url.path.endswith("/create"):
            return httpx.Response(200, json={"id": "officecli_file"})
        if request.url.path.endswith("/update"):
            return httpx.Response(200, json={"id": "officecli_file"})
        # GET /id/{id}
        if existing is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=existing)

    return httpx.MockTransport(handler)


async def test_sync_creates_tool_when_missing(settings, mcp_server, monkeypatch):
    from officecli_mcp import shim_sync

    rec: list[dict] = []
    monkeypatch.setattr(shim_sync, "_client", lambda url, key: httpx.AsyncClient(
        base_url=url, transport=_transport(rec), headers={"Authorization": f"Bearer {key}"}))
    await shim_sync.sync_shim(_settings(settings), mcp_server)
    assert len(rec) == 2  # GET (404) then POST create
    assert rec[1]["method"] == "POST" and rec[1]["url"].endswith("/create")
    import json

    body = json.loads(rec[1]["body"])
    assert body["id"] == "officecli_file"
    assert body["content"].startswith("# officecli-shim-rev: ")


async def test_sync_updates_stale_tool_preserving_access_grants(settings, mcp_server, monkeypatch):
    from officecli_mcp import shim_sync

    existing = {
        "id": "officecli_file",
        "name": "Office CLI",
        "content": "# officecli-shim-rev: stale\nold",
        "meta": {"description": "old"},
        "access_grants": [{"principal_type": "group", "principal_id": "*", "permission": "read"}],
    }
    rec: list[dict] = []
    monkeypatch.setattr(shim_sync, "_client", lambda url, key: httpx.AsyncClient(
        base_url=url, transport=_transport(rec, existing=existing),
        headers={"Authorization": f"Bearer {key}"}))
    await shim_sync.sync_shim(_settings(settings), mcp_server)
    assert [r["method"] for r in rec] == ["GET", "POST"]
    assert rec[1]["url"].endswith("/update")
    import json

    body = json.loads(rec[1]["body"])
    assert body["access_grants"] == existing["access_grants"]  # preserved
    assert not body["content"].startswith("# officecli-shim-rev: stale")


async def test_sync_noop_when_revision_matches(settings, mcp_server, monkeypatch):
    from officecli_mcp import shim_sync
    from officecli_mcp.manifest import get_manifest
    from officecli_mcp.shim import render_shim

    current = render_shim(await get_manifest(mcp_server))
    existing = {"id": "officecli_file", "name": "t", "content": current,
                "meta": {"description": "d"}, "access_grants": None}
    rec: list[dict] = []
    monkeypatch.setattr(shim_sync, "_client", lambda url, key: httpx.AsyncClient(
        base_url=url, transport=_transport(rec, existing=existing),
        headers={"Authorization": f"Bearer {key}"}))
    await shim_sync.sync_shim(_settings(settings), mcp_server)
    assert [r["method"] for r in rec] == ["GET"]  # no write


async def test_sync_skipped_when_disabled_or_unconfigured(settings, mcp_server, monkeypatch):
    from officecli_mcp import shim_sync

    def _boom(*a, **k):
        raise AssertionError("no HTTP expected")

    monkeypatch.setattr(shim_sync, "_client", _boom)
    await shim_sync.sync_shim(_settings(settings, owui_sync=False), mcp_server)
    from dataclasses import replace

    await shim_sync.sync_shim(replace(_settings(settings), owui_api_key=""), mcp_server)
    await shim_sync.sync_shim(replace(_settings(settings), owui_url=""), mcp_server)


async def test_sync_swallows_http_errors(settings, mcp_server, monkeypatch, caplog):
    from officecli_mcp import shim_sync

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    monkeypatch.setattr(shim_sync, "_client", lambda url, key: httpx.AsyncClient(
        base_url=url, transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {key}"}))
    with caplog.at_level("WARNING"):
        await shim_sync.sync_shim(_settings(settings), mcp_server)  # must not raise
    assert any("shim" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shim_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'officecli_mcp.shim_sync'`

- [ ] **Step 3: Implement `shim_sync.py`**

```python
"""Push the rendered officecli_file shim into OpenWebUI's Tools API on boot.

Best-effort: any failure logs a warning and the server keeps running (the
shim in OpenWebUI is simply stale; action="run"/"tools" still hit the live
server). Requires an OpenWebUI ADMIN API key - create/update of tool content
is an admin-or-workspace.tools-permission operation (routers/tools.py).
"""
from __future__ import annotations

import logging

import httpx

from officecli_mcp.manifest import get_manifest
from officecli_mcp.shim import SHIM_HEADER, render_shim

log = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _client(url: str, key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=_TIMEOUT,
    )


def _stored_revision(content: str) -> str:
    first = (content or "").splitlines()[0] if content else ""
    return first.removeprefix(SHIM_HEADER).strip() if first.startswith(SHIM_HEADER) else ""


async def sync_shim(settings, mcp) -> None:
    """Create or update the officecli_file tool in OpenWebUI to match the
    current manifest. No-op when disabled, unconfigured, or up to date."""
    if not getattr(settings, "owui_sync", True):
        log.info("shim self-sync disabled (OFFICECLI_MCP_OWUI_SYNC=0)")
        return
    url = getattr(settings, "owui_url", "")
    key = getattr(settings, "owui_api_key", "")
    if not url or not key:
        log.info("shim self-sync skipped: OFFICECLI_MCP_OWUI_URL / _API_KEY not set")
        return
    tool_id = getattr(settings, "owui_tool_id", "officecli_file")
    try:
        manifest = await get_manifest(mcp)
        content = render_shim(manifest)
        async with _client(url, key) as client:
            resp = await client.get(f"/api/v1/tools/id/{tool_id}")
            if resp.status_code == 404:
                await client.post(
                    "/api/v1/tools/create",
                    json={
                        "id": tool_id,
                        "name": "officecli_file",
                        "content": content,
                        "meta": {
                            "description": (
                                "All officecli document operations through "
                                "officecli-mcp (upload/download/stage/run/tools)."
                            )
                        },
                        "access_grants": None,
                    },
                )
                log.info("shim self-sync: created OpenWebUI tool '%s'", tool_id)
                return
            resp.raise_for_status()
            existing = resp.json()
            if _stored_revision(existing.get("content", "")) == manifest["revision"]:
                log.info("shim self-sync: OpenWebUI tool '%s' up to date", tool_id)
                return
            resp = await client.post(
                f"/api/v1/tools/id/{tool_id}/update",
                json={
                    "id": tool_id,
                    "name": existing.get("name", tool_id),
                    "content": content,
                    "meta": {
                        **(existing.get("meta") or {}),
                        "description": (
                            "All officecli document operations through "
                            "officecli-mcp (upload/download/stage/run/tools)."
                        ),
                    },
                    "access_grants": existing.get("access_grants"),
                },
            )
            resp.raise_for_status()
            log.info("shim self-sync: updated OpenWebUI tool '%s'", tool_id)
    except Exception:  # noqa: BLE001
        log.warning("shim self-sync failed (continuing without it)", exc_info=True)
```

In `server.py` `build_app`, after `app.state.mcp = mcp` (before `return app`), kick off the sync as a background task — `build_app` is sync, so run it on the app's lifespan. Simplest correct approach: run it in a thread so startup is never blocked:

```python
    # Best-effort: push the regenerated officecli_file shim into OpenWebUI.
    # Runs in a thread so boot is never blocked by OpenWebUI being down.
    if getattr(settings, "owui_sync", True) and getattr(settings, "owui_api_key", ""):
        import threading

        from officecli_mcp.shim_sync import sync_shim

        def _sync() -> None:
            import anyio

            anyio.run(sync_shim, settings, mcp)

        threading.Thread(target=_sync, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shim_sync.py -v`
Expected: 5 passed. Also `pytest -q` — full suite green (server tests unaffected: sync is skipped without an API key).

- [ ] **Step 5: Commit**

```bash
ruff check . && pytest -q
git add src/officecli_mcp/shim_sync.py src/officecli_mcp/server.py tests/test_shim_sync.py
git commit -m "feat: self-sync the officecli_file shim into OpenWebUI on container boot

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Shim `run`/`tools` action tests

**Files:**
- Test: `tests/test_shim_actions.py` (new)
- Modify: none (the shim code landed in Task 5; this task locks its behavior)

**Interfaces:**
- Consumes: the generated `examples/openwebui_officecli_file.py` (import via importlib like `tests/test_officecli_file.py` does); `_mcp_call(path, payload)` seam.

- [ ] **Step 1: Write the tests**

```python
"""Behavior tests for the shim's run/tools actions (against the generated example)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "officecli_file_tool", ROOT / "examples" / "openwebui_officecli_file.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_run_forwards_tool_and_arguments(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    seen = {}

    def fake_call(path, payload=None):
        seen["path"] = path
        seen["payload"] = payload
        return {"content": [{"type": "text", "text": "OK"}], "isError": False}

    monkeypatch.setattr(tools, "_mcp_call", fake_call)
    out = json.loads(
        await tools.officecli_file(
            action="run",
            tool="officecli_set",
            arguments='{"file_id":"f1","selector":"/slide[1]","prop":["x=2cm"]}',
        )
    )
    assert seen["path"] == "/tools/call"
    assert seen["payload"] == {
        "name": "officecli_set",
        "arguments": {"file_id": "f1", "selector": "/slide[1]", "prop": ["x=2cm"]},
    }
    assert out == {"content": [{"type": "text", "text": "OK"}], "isError": False}


async def test_run_rejects_bad_arguments_json():
    mod = _load()
    out = json.loads(await mod.Tools().officecli_file(action="run", tool="t", arguments="{nope"))
    assert "JSON" in out["error"]


async def test_run_unknown_tool_surfaces_404(monkeypatch):
    import requests

    mod = _load()
    tools = mod.Tools()

    def fake_call(path, payload=None):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("nf", response=resp)

    monkeypatch.setattr(tools, "_mcp_call", fake_call)
    out = json.loads(await tools.officecli_file(action="run", tool="officecli_nope"))
    assert "unknown tool" in out["error"]


async def test_run_converts_image_blocks_to_data_urls(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    monkeypatch.setattr(
        tools,
        "_mcp_call",
        lambda path, payload=None: {
            "content": [{"type": "image", "data": "QUJD", "mimeType": "image/png"}],
            "isError": False,
        },
    )
    out = json.loads(await tools.officecli_file(action="run", tool="officecli_view_screenshot", arguments="{}"))
    assert out["content"][0]["data"] == "data:image/png;base64,QUJD"
    assert out["content"][0]["type"] == "image"


async def test_run_propagates_iserror_text(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    monkeypatch.setattr(
        tools,
        "_mcp_call",
        lambda path, payload=None: {
            "content": [{"type": "text", "text": "officecli exited 2: UNSUPPORTED prop"}],
            "isError": True,
        },
    )
    out = json.loads(await tools.officecli_file(action="run", tool="officecli_set", arguments="{}"))
    assert out["isError"] is True
    assert "UNSUPPORTED" in out["content"][0]["text"]


async def test_tools_action_returns_manifest(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    monkeypatch.setattr(
        tools, "_mcp_call", lambda path, payload=None: {"revision": "x", "tools": []}
    )
    out = json.loads(await tools.officecli_file(action="tools"))
    assert out["revision"] == "x"
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_shim_actions.py -v`
Expected: 6 passed (shim already implements these from Task 5 — if any fail, fix the TEMPLATE in `shim_template.py`, regenerate with `python -m officecli_mcp.shim`, and re-run)

- [ ] **Step 3: Commit**

```bash
ruff check . && pytest -q
git add tests/test_shim_actions.py
git commit -m "test: cover shim run/tools actions against the generated example

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: E2E coverage + docs + compose

**Files:**
- Modify: `tests/test_e2e_real.py` (append)
- Modify: `README.md`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: everything above. E2E tests use the real officecli binary at `/tmp/officecli` (existing pattern in `test_e2e_real.py`; skipped when absent).

- [ ] **Step 1: Append e2e tests** (follow the file's existing fixtures/skip pattern)

```python
def test_tools_endpoint_lists_real_tools(real_app_client):
    resp = real_app_client.get("/tools")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["tools"]}
    assert "officecli_create" in names


def test_tools_call_create_and_view_roundtrip(real_app_client):
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_create", "arguments": {"name": "http.pptx", "type": "pptx", "file_id": "x"}},
    )
    body = resp.json()
    assert body["isError"] is False
    file_id = body["content"][0]["text"].splitlines()[0]
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_view_text", "arguments": {"file_id": file_id}},
    )
    assert resp.json()["isError"] is False


def test_tools_call_screenshot_respects_max_edge(real_app_client):
    import base64
    import io

    from PIL import Image

    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_create", "arguments": {"name": "shot.pptx", "type": "pptx", "file_id": "x"}},
    )
    file_id = resp.json()["content"][0]["text"].splitlines()[0]
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_view_screenshot", "arguments": {"file_id": file_id}},
    )
    body = resp.json()
    assert body["isError"] is False
    img_block = next(b for b in body["content"] if b["type"] == "image")
    with Image.open(io.BytesIO(base64.b64decode(img_block["data"]))) as im:
        assert max(im.size) <= 1024
```

NOTE: `officecli_create`'s schema has `file_id` in its signature (vestigial — the tool generates its own). Pass a placeholder like `"x"`. If the real binary rejects the extra arg, check the actual inputSchema via `GET /tools` first and pass exactly what it requires (adjust the test to match the live schema — do NOT change the tool signature in this task).

- [ ] **Step 2: Run e2e**

Run: `pytest tests/test_e2e_real.py -v`
Expected: all pass (or skip when the binary is absent)

- [ ] **Step 3: README** — rewrite the quick start around the single tool; document the new env vars in the env table:

Add rows to the env table:
- `OFFICECLI_MCP_SCREENSHOT_MAX_EDGE` | `1024` | screenshot downscale longest edge (px); 0=off
- `OFFICECLI_MCP_OWUI_SYNC` | `1` | push the officecli_file tool into OpenWebUI on boot
- `OFFICECLI_MCP_OWUI_URL` | `""` | OpenWebUI internal base (for self-sync)
- `OFFICECLI_MCP_OWUI_API_KEY` | `""` | OpenWebUI **admin** API key (for self-sync; keep secret)
- `OFFICECLI_MCP_OWUI_TOOL_ID` | `officecli_file` | tool id to create/update

Update the quick start: step 2 becomes "set the sync env vars (or paste `examples/openwebui_officecli_file.py` manually)"; step 3 drops the MCP connection — note it stays available for debugging but is no longer required. State that the admin API key is sensitive (use an env file / secret, never commit).

- [ ] **Step 4: docker-compose.yml** — add commented placeholders under the officecli-mcp service environment:

```yaml
      # Optional: auto-create/update the officecli_file tool in OpenWebUI.
      # OFFICECLI_MCP_OWUI_URL: "http://open-webui:8080"
      # OFFICECLI_MCP_OWUI_API_KEY: "sk-..."   # admin API key - keep secret
      # OFFICECLI_MCP_SCREENSHOT_MAX_EDGE: "1024"
```

- [ ] **Step 5: Verify + commit**

```bash
ruff check . && pytest -q
git add tests/test_e2e_real.py README.md docker-compose.yml
git commit -m "docs: single-tool quick start; e2e for /tools + /tools/call

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review Notes (already applied)

- Spec §1 (screenshot resize): Task 2 + e2e in Task 8. §1 (/tools, /tools/call, api-key gate): Task 4. §2 (shim run/tools, full manifest in docstring, JSON-string arguments): Tasks 5+7. §3 (template, revision stamp, create/update/no-op/disabled sync): Tasks 5+6. §4 (MCP stays, README): Task 8.
- Image-over-/tools/call path: covered by Task 4's content-block conversion + Task 7's data-URL test + Task 8's real-binary screenshot e2e.
- `Settings` env-at-class-definition-time pitfall: Task 3 tests use `importlib.reload`.
- Template escaping pitfall (`\x89` etc. in `_infer_ext`): called out in Task 5 step 3 with a sanity-check command.
