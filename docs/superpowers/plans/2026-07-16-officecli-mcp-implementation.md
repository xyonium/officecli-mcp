# officecli-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python MCP server that wraps the `officecli` binary, exposing handle-based MCP tools (prefixed `officecli_`) plus an HTTP `/files` upload endpoint, so OpenWebUI can push office documents and receive rendered HTML/screenshots.

**Architecture:** One Starlette process with two surfaces sharing one workdir: (1) an HTTP `/files` router for byte upload/download returning `file_id` handles, and (2) a FastMCP server mounted at `/mcp` (streamable-HTTP) whose tools resolve `file_id` → local path and shell out to `officecli`. A separate ~40-line native OpenWebUI tool (in `examples/`) reads `__files__`, fetches bytes from OpenWebUI's file API, and POSTs them to `/files`. The `officecli` binary is auto-downloaded on startup.

**Tech Stack:** Python ≥3.10, official MCP SDK `mcp[cli]` v1.x (bundled FastMCP), Starlette, uvicorn, httpx, pydantic v2, pytest + anyio. `officecli` binary (downloaded at runtime). Docker for packaging.

**Verified API facts (mcp v1.28.1, July 2026):**
- `from mcp.server.fastmcp import FastMCP`; `mcp = FastMCP("name")`; `@mcp.tool()` (parens required)
- `from mcp.server.fastmcp import Image` → `Image(data=raw_bytes, format="png")` auto-base64s into ImageContent
- `from mcp.types import ToolAnnotations` → `ToolAnnotations(readOnlyHint=True, openWorldHint=False)` (camelCase in v1.x)
- Mount: `app.mount("/mcp", mcp.streamable_http_app())` AND wrap `async with mcp.session_manager.run(): yield` in a Starlette lifespan (required when mounted)
- Standalone run: `mcp.run(transport="streamable-http")` (default mount `/mcp`, host 127.0.0.1:8000)
- In-memory test client: `from mcp.shared.memory import create_connected_server_and_client_session`

---

## File Structure

- `pyproject.toml` — project metadata, deps (`mcp[cli]`, `starlette`, `uvicorn[standard]`, `httpx`, `pydantic`), dev deps (`pytest`, `pytest-asyncio`, `anyio`), console script `officecli-mcp`
- `src/officecli_mcp/__init__.py` — package marker, version
- `src/officecli_mcp/config.py` — env-based settings (dataclass)
- `src/officecli_mcp/binary.py` — locate/download/verify the officecli binary
- `src/officecli_mcp/files.py` — HTTP file store (UploadFile/base64 → file_id, download, delete, TTL sweep)
- `src/officecli_mcp/runner.py` — resolve file_id→path, build argv, exec officecli, intercept html/screenshot
- `src/officecli_mcp/tools.py` — the `officecli_*` MCP tool definitions (FastMCP)
- `src/officecli_mcp/server.py` — assemble Starlette app (mount `/files` router + `/mcp`), entrypoint dispatch (http vs stdio)
- `src/officecli_mcp/__main__.py` — CLI arg parsing, call into server
- `examples/openwebui_officecli_upload.py` — native OpenWebUI tool shim
- `tests/conftest.py` — shared fixtures (tmp workdir, fake officecli stub)
- `tests/test_binary.py`, `tests/test_files.py`, `tests/test_runner.py`, `tests/test_tools.py`, `tests/test_server.py`
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`

**Responsibility boundaries:** `binary.py` owns only the executable path. `files.py` owns only `/work/{file_id}` and the HTTP routes. `runner.py` owns only subprocess execution + output interception (no HTTP, no MCP). `tools.py` owns only MCP tool definitions (calls runner). `server.py` wires them and owns transport. This keeps each file small and independently testable.

---

## Task 1: Project skeleton + pyproject + config

**Files:**
- Create: `pyproject.toml`
- Create: `src/officecli_mcp/__init__.py`
- Create: `src/officecli_mcp/config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "officecli-mcp"
version = "0.1.0"
description = "MCP server wrapping OfficeCLI for OpenWebUI: handle-based HTTP file layer + streamable-HTTP/stdio"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "Apache-2.0" }
authors = [{ name = "xyonium" }]
dependencies = [
    "mcp[cli]>=1.27,<2",
    "starlette>=0.37",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "anyio>=4",
    "trio>=0.25",
]

[project.scripts]
officecli-mcp = "officecli_mcp.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `src/officecli_mcp/__init__.py`**

```python
"""officecli-mcp: MCP server wrapping OfficeCLI for OpenWebUI."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Write `src/officecli_mcp/config.py`**

```python
"""Environment-based configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    transport: str = os.environ.get("OFFICECLI_MCP_TRANSPORT", "http")
    host: str = os.environ.get("OFFICECLI_MCP_HOST", "0.0.0.0")
    port: int = _env_int("OFFICECLI_MCP_PORT", 8765)
    data_dir: str = os.environ.get("OFFICECLI_MCP_DATA_DIR", "/data")
    work_dir: str = os.environ.get("OFFICECLI_MCP_WORK_DIR", "/work")
    work_ttl_seconds: int = _env_int("OFFICECLI_MCP_WORK_TTL_SECONDS", 3600)
    max_upload_mb: int = _env_int("OFFICECLI_MCP_MAX_UPLOAD_MB", 50)
    officecli_version: str = os.environ.get("OFFICECLI_VERSION", "latest")
    officecli_sha256: str = os.environ.get("OFFICECLI_SHA256", "")
    api_key: str = os.environ.get("OFFICECLI_MCP_API_KEY", "")
    allowed_extensions: tuple[str, ...] = ("docx", "xlsx", "pptx")

    @property
    def binary_path(self) -> str:
        return os.path.join(self.data_dir, "officecli")


def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Write `tests/__init__.py` (empty)** and `tests/conftest.py`**

```python
# tests/__init__.py
```

```python
# tests/conftest.py
"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make src importable without an installed package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def settings(tmp_path: Path) -> "object":
    from officecli_mcp.config import Settings

    return Settings(
        transport="http",
        host="127.0.0.1",
        port=8765,
        data_dir=str(tmp_path / "data"),
        work_dir=str(tmp_path / "work"),
        work_ttl_seconds=3600,
        max_upload_mb=50,
        officecli_version="latest",
        officecli_sha256="",
        api_key="",
        allowed_extensions=("docx", "xlsx", "pptx"),
    )
```

- [ ] **Step 5: Install the package in editable mode and verify import**

Run: `python3 -m pip install -e ".[dev]" 2>&1 | tail -5 && python3 -c "from officecli_mcp.config import get_settings; s=get_settings(); print(s.port, s.binary_path)"`
Expected: prints `8765` and a path ending in `/data/officecli` (env not set in shell, so defaults apply).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/officecli_mcp/__init__.py src/officecli_mcp/config.py tests/__init__.py tests/conftest.py
git commit -m "feat: project skeleton, pyproject, config"
```

---

## Task 2: binary.py — officecli bootstrap

**Files:**
- Create: `src/officecli_mcp/binary.py`
- Test: `tests/test_binary.py`

- [ ] **Step 1: Write the failing test `tests/test_binary.py`**

```python
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def test_asset_name_linux_x64(monkeypatch):
    from officecli_mcp import binary

    monkeypatch.setattr(binary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(binary.sys, "platform", lambda: "linux")
    assert binary.asset_name() == "officecli-linux-x64"


def test_asset_name_linux_arm64(monkeypatch):
    from officecli_mcp import binary

    monkeypatch.setattr(binary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(binary.sys, "platform", lambda: "linux")
    assert binary.asset_name() == "officecli-linux-arm64"


def test_ensure_binary_uses_cached(monkeypatch, tmp_path):
    from officecli_mcp import binary

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fake_bin = data_dir / "officecli"
    fake_bin.write_text("#!/bin/sh\necho hi\n")
    fake_bin.chmod(0o755)

    download_called = []

    def fake_download(_asset, _dest):
        download_called.append(True)

    monkeypatch.setattr(binary, "_download", fake_download)
    monkeypatch.setattr(binary, "_is_executable", lambda p: True)

    path = binary.ensure_binary(str(data_dir), version="latest")
    assert path == str(fake_bin)
    assert download_called == []  # cached, no download


def test_ensure_binary_downloads_when_missing(monkeypatch, tmp_path):
    from officecli_mcp import binary

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    def fake_download(_asset, dest):
        Path(dest).write_text("#!/bin/sh\necho hi\n")
        os.chmod(dest, 0o755)

    monkeypatch.setattr(binary, "_download", fake_download)
    monkeypatch.setattr(binary, "_is_executable", lambda p: True)

    path = binary.ensure_binary(str(data_dir), version="latest")
    assert Path(path).exists()
    mode = Path(path).stat().st_mode
    assert mode & stat.S_IXUSR
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_binary.py -v 2>&1 | tail -15`
Expected: FAIL with `ModuleNotFoundError: No module named 'officecli_mcp.binary'`

- [ ] **Step 3: Write `src/officecli_mcp/binary.py`**

```python
"""Locate, download, and verify the officecli binary."""
from __future__ import annotations

import logging
import os
import platform
import stat
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

RELEASE_BASE = "https://github.com/iOfficeAI/OfficeCLI/releases"
_DOWNLOAD_TIMEOUT = 120.0


def asset_name() -> str:
    """Return the release asset name for the current platform."""
    system = platform.system()
    machine = platform.machine().lower()
    is_alpine = "alpine" in str(sys.platform) or "musl" in os.environ.get("OFFICECLI_MCP_LIBC", "")

    if system == "Linux":
        arch = "x64" if machine in {"x86_64", "amd64"} else "arm64"
        prefix = "officecli-linux-alpine-" if is_alpine else "officecli-linux-"
        return f"{prefix}{arch}"
    if system == "Darwin":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return f"officecli-mac-{arch}"
    if system == "Windows":
        arch = "x64" if machine in {"x86_64", "amd64"} else "arm64"
        return f"officecli-win-{arch}.exe"
    raise RuntimeError(f"Unsupported platform: {system} {machine}")


def _is_executable(path: str) -> bool:
    p = Path(path)
    return p.exists() and bool(p.stat().st_mode & stat.S_IXUSR)


def _download(asset: str, dest: str) -> None:
    url = f"{RELEASE_BASE}/latest/download/{asset}"
    log.info("Downloading officecli from %s", url)
    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(resp.content)
    os.chmod(dest, 0o755)


def _verify_sha256(path: str, expected: str) -> None:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError(f"officecli sha256 mismatch: expected {expected}, got {actual}")


def ensure_binary(data_dir: str, version: str = "latest", sha256: str = "") -> str:
    """Ensure the officecli binary exists at data_dir/officecli; download if missing.

    Returns the absolute path to the binary. If a cached binary exists it is
    reused (we do not re-download on every start when version=='latest', to
    keep startup fast offline). If sha256 is set, verify it.
    """
    dest = str(Path(data_dir) / "officecli")
    if not _is_executable(dest):
        _download(asset_name(), dest)
    if sha256:
        _verify_sha256(dest, sha256)
    if not _is_executable(dest):
        os.chmod(dest, 0o755)
    return dest
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_binary.py -v 2>&1 | tail -15`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/binary.py tests/test_binary.py
git commit -m "feat(binary): locate/download/verify officecli binary"
```

---

## Task 3: files.py — HTTP file store

**Files:**
- Create: `src/officecli_mcp/files.py`
- Test: `tests/test_files.py`

- [ ] **Step 1: Write the failing test `tests/test_files.py`**

```python
from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


def _make_app(settings):
    from officecli_mcp.files import build_files_router, FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=settings.work_ttl_seconds)
    from starlette.applications import Starlette
    from starlette.routing import Mount

    app = Starlette(routes=[Mount("/", app=build_files_router(store, settings))])
    return app, store


def test_upload_and_download_multipart(settings):
    app, store = _make_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/files",
        files={"file": ("report.docx", b"PK\x03\x04fake-docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "report.docx"
    file_id = body["file_id"]
    assert Path(settings.work_dir, file_id, "report.docx").exists()

    dl = client.get(f"/files/{file_id}")
    assert dl.status_code == 200
    assert dl.content == b"PK\x03\x04fake-docx"


def test_upload_base64(settings):
    app, store = _make_app(settings)
    client = TestClient(app)
    data = base64.b64encode(b"hello-docx").decode()
    resp = client.post("/files", json={"filename": "x.docx", "data_base64": data})
    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == "x.docx"


def test_rejects_bad_extension(settings):
    app, store = _make_app(settings)
    client = TestClient(app)
    resp = client.post("/files", files={"file": ("evil.exe", b"nope", "application/octet-stream")})
    assert resp.status_code == 415


def test_download_unknown_returns_404(settings):
    app, store = _make_app(settings)
    client = TestClient(app)
    assert client.get("/files/does-not-exist").status_code == 404


def test_ttl_sweep_removes_old(settings, tmp_path):
    app, store = _make_app(settings)
    client = TestClient(app)
    resp = client.post("/files", files={"file": ("a.docx", b"x", "application/octet-stream")})
    file_id = resp.json()["file_id"]
    # Backdate the workdir mtime beyond TTL.
    d = Path(settings.work_dir, file_id)
    old = time.time() - (settings.work_ttl_seconds + 60)
    os_utime = __import__("os").utime
    os_utime(d, (old, old))
    store.sweep()
    assert not d.exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_files.py -v 2>&1 | tail -15`
Expected: FAIL with `ModuleNotFoundError: No module named 'officecli_mcp.files'`

- [ ] **Step 3: Write `src/officecli_mcp/files.py`**

```python
"""HTTP file store: upload/download/delete office docs by file_id handle."""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from pathlib import Path

from starlette.authentication import requires  # noqa: F401  (kept for clarity)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router

log = logging.getLogger(__name__)

_SAFE_EXT = {"docx", "xlsx", "pptx"}


def _safe_filename(name: str) -> str:
    """Strip path separators; keep only the basename."""
    base = os.path.basename(name).strip()
    # Reject anything that escapes after basename.
    if base in {"", ".", ".."}:
        raise ValueError("invalid filename")
    return base


class FileStore:
    """Owns /work/{file_id}/ and the TTL sweep."""

    def __init__(self, work_dir: str, ttl_seconds: int):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds

    def _dir(self, file_id: str) -> Path:
        return self.work_dir / file_id

    def put(self, filename: str, data: bytes) -> dict:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in _SAFE_EXT:
            raise ValueError(f"extension .{ext} not allowed")
        safe = _safe_filename(filename)
        file_id = uuid.uuid4().hex
        d = self._dir(file_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / safe).write_bytes(data)
        return {"file_id": file_id, "filename": safe, "size": len(data), "mime": _mime(ext)}

    def path_for(self, file_id: str, filename: str | None = None) -> Path:
        d = self._dir(file_id)
        if not d.exists():
            raise KeyError(file_id)
        if filename:
            return d / _safe_filename(filename)
        files = [p for p in d.iterdir() if p.is_file() and p.name != "shot.png"]
        if not files:
            raise KeyError(file_id)
        return files[0]

    def read(self, file_id: str) -> tuple[bytes, str]:
        p = self.path_for(file_id)
        return p.read_bytes(), p.name

    def delete(self, file_id: str) -> bool:
        d = self._dir(file_id)
        if not d.exists():
            return False
        for child in d.iterdir():
            child.unlink(missing_ok=True)
        d.rmdir()
        return True

    def sweep(self) -> int:
        """Delete workdirs older than ttl. Returns count removed."""
        cutoff = time.time() - self.ttl_seconds
        removed = 0
        for d in self.work_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                mtime = d.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                self.delete(d.name)
                removed += 1
        return removed


def _mime(ext: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }.get(ext, "application/octet-stream")


def _check_api_key(request: Request, api_key: str) -> JSONResponse | None:
    if not api_key:
        return None
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token != api_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


async def upload(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            payload = await request.json()
            filename = payload["filename"]
            data = base64.b64decode(payload["data_base64"])
        else:
            form = await request.form()
            upload_file = form["file"]
            filename = upload_file.filename or "upload.docx"
            data = await upload_file.read()
    except (KeyError, ValueError) as e:
        return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return JSONResponse({"error": "file too large"}, status_code=413)

    try:
        info = store.put(filename, data)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=415)
    return JSONResponse(info)


async def download(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    file_id = request.path_params["file_id"]
    try:
        data, name = store.read(file_id)
    except KeyError:
        return JSONResponse({"error": "file_id not found or expired"}, status_code=404)
    return Response(data, media_type="application/octet-stream",
                    headers={"content-disposition": f'attachment; filename="{name}"'})


async def delete(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    file_id = request.path_params["file_id"]
    removed = store.delete(file_id)
    return JSONResponse({"deleted": removed})


def build_files_router(store: FileStore, settings) -> Router:
    routes = [
        Route("/files", upload, methods=["POST"]),
        Route("/files/{file_id}", download, methods=["GET"]),
        Route("/files/{file_id}", delete, methods=["DELETE"]),
    ]
    return Router(routes)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_files.py -v 2>&1 | tail -20`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/files.py tests/test_files.py
git commit -m "feat(files): HTTP file store with upload/download/delete + TTL"
```

---

## Task 4: runner.py — officecli subprocess layer

**Files:**
- Create: `src/officecli_mcp/runner.py`
- Test: `tests/test_runner.py`

- [ ] **Step 1: Write the failing test `tests/test_runner.py`**

This uses a stub `officecli` script so tests don't depend on the real binary.

```python
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def _write_stub(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


def test_run_text_returns_stdout(settings, tmp_path, monkeypatch):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho 'VIEW TEXT OUTPUT'\n")

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "text"])
    assert res.exit_code == 0
    assert "VIEW TEXT OUTPUT" in res.stdout
    assert res.image_path is None


def test_run_html_intercept_returns_text(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho '<html><body>RENDERED</body></html>'\n")

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "html"])
    assert res.exit_code == 0
    assert "RENDERED" in res.stdout
    assert res.image_path is None  # html is stdout, not a file


def test_run_screenshot_intercept_writes_png(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.pptx", b"pptx-bytes")
    # Stub: when given -o, write a fake PNG to that path.
    stub = tmp_path / "officecli"
    _write_stub(
        stub,
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = '-o' ]; then out=\"$2\"; fi\n"
        "  shift\n"
        "done\n"
        "if [ -n \"$out\" ]; then printf '\\x89PNG\\r\\n\\x1a\\n' > \"$out\"; fi\n",
    )

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "screenshot", "--page", "1"])
    assert res.exit_code == 0
    assert res.image_path is not None
    assert Path(res.image_path).read_bytes().startswith(b"\x89PNG")


def test_run_substitutes_path_token(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho \"$@\" > /tmp/ocli_argv_test; echo ok\n")

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "text"])
    assert res.exit_code == 0
    argv = Path("/tmp/ocli_argv_test").read_text()
    expected_path = str(store.path_for(info["file_id"]))
    assert expected_path in argv


def test_run_unknown_file_id_raises(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner, FileIDNotFound
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho x\n")
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    with pytest.raises(FileIDNotFound):
        runner.run("nope", ["view", "{path}", "text"])


def test_run_nonzero_exit_captured(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho 'boom' 1>&2; exit 3\n")
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["validate", "{path}"])
    assert res.exit_code == 3
    assert "boom" in res.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_runner.py -v 2>&1 | tail -15`
Expected: FAIL with `ModuleNotFoundError: No module named 'officecli_mcp.runner'`

- [ ] **Step 3: Write `src/officecli_mcp/runner.py`**

```python
"""Run officecli as a subprocess; intercept html (stdout) and screenshot (PNG)."""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from officecli_mcp.files import FileStore

log = logging.getLogger(__name__)


class FileIDNotFound(Exception):
    """The file_id is unknown or expired."""


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    image_path: str | None  # set when a screenshot PNG was produced


class OfficeRunner:
    def __init__(self, binary_path: str, file_store: FileStore):
        self.binary_path = binary_path
        self.file_store = file_store

    def resolve(self, file_id: str) -> Path:
        try:
            return self.file_store.path_for(file_id)
        except KeyError as e:
            raise FileIDNotFound(file_id) from e

    def run(self, file_id: str, argv_template: list[str]) -> RunResult:
        """argv_template uses the literal token '{path}' where the file path goes.

        Special handling:
        - 'view ... html' / 'view ... svg' / text modes: no -o, capture stdout.
        - 'view ... screenshot': if no -o present, inject -o <workdir>/shot.png,
          then read the PNG into image_path.
        """
        path = str(self.resolve(file_id))
        argv = [a.replace("{path}", path) for a in argv_template]
        cwd = str(Path(path).parent)

        is_screenshot = "screenshot" in argv
        image_path: str | None = None
        if is_screenshot and "-o" not in argv:
            image_path = str(Path(cwd) / "shot.png")
            argv += ["-o", image_path]

        env = dict(os.environ)
        env["OFFICECLI_NO_AUTO_RESIDENT"] = "1"

        proc = subprocess.run(
            [self.binary_path, *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
        )
        return RunResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            image_path=image_path,
        )

    def read_image(self, image_path: str) -> bytes:
        return Path(image_path).read_bytes()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_runner.py -v 2>&1 | tail -20`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/runner.py tests/test_runner.py
git commit -m "feat(runner): officecli subprocess layer with html/screenshot intercept"
```

---

## Task 5: tools.py — the officecli_* MCP tools

**Files:**
- Create: `src/officecli_mcp/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write the failing test `tests/test_tools.py`**

Uses the in-memory MCP client against a server backed by a stub binary.

```python
from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session


def _write_stub(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


@pytest.fixture
async def mcp_server(settings, tmp_path):
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp import tools as tools_mod

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho 'TEXT-OUT'\n")
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    return tools_mod.build_mcp(runner=runner, file_store=store), store


async def test_list_tools_has_prefixed_names(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert "officecli_view_html" in names
        assert "officecli_view_screenshot" in names
        assert "officecli_create" in names
        # Ensure no unprefixed collisions
        assert all(n.startswith("officecli_") for n in names)


async def test_view_html_returns_text(mcp_server):
    mcp, store = mcp_server
    info = store.put("r.docx", b"docx-bytes")
    # Override stub to emit HTML for the html subcommand.
    Path(mcp._runner.binary_path).write_text("#!/bin/sh\necho '<html>HI</html>'\n")
    Path(mcp._runner.binary_path).chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool("officecli_view_html", {"file_id": info["file_id"]})
        texts = [c.text for c in res.content if hasattr(c, "text")]
        assert any("HI" in t for t in texts)


async def test_view_screenshot_returns_image(mcp_server, tmp_path, settings):
    mcp, store = mcp_server
    info = store.put("r.pptx", b"pptx-bytes")
    # Stub writes a fake PNG to -o path.
    stub = Path(mcp._runner.binary_path)
    stub.write_text(
        "#!/bin/sh\no='';while [ $# -gt 0 ];do [ \"$1\" = '-o' ]&&o=\"$2\";shift;done;"
        "[ -n \"$o\" ]&&printf '\\x89PNG\\r\\n\\x1a\\n'>\"$o\"\n"
    )
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_view_screenshot", {"file_id": info["file_id"], "page": 1}
        )
        imgs = [c for c in res.content if getattr(c, "type", None) == "image"]
        assert imgs, f"expected an image block, got {res.content}"


async def test_unknown_file_id_is_error(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool("officecli_view_html", {"file_id": "ghost"})
        assert res.isError
        texts = [c.text for c in res.content if hasattr(c, "text")]
        assert any("not found" in t.lower() for t in texts)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_tools.py -v 2>&1 | tail -15`
Expected: FAIL with `ModuleNotFoundError: No module named 'officecli_mcp.tools'`

- [ ] **Step 3: Write `src/officecli_mcp/tools.py`**

```python
"""officecli_* MCP tool definitions (handle-based)."""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from officecli_mcp.files import FileStore
from officecli_mcp.runner import FileIDNotFound, OfficeRunner

log = logging.getLogger(__name__)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def build_mcp(runner: OfficeRunner, file_store: FileStore) -> FastMCP:
    mcp = FastMCP("officecli-mcp")
    # Expose runner on the instance for tests; not part of the public API.
    mcp._runner = runner  # type: ignore[attr-defined]
    mcp._file_store = file_store  # type: ignore[attr-defined]

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_text(file_id: str, page: int | None = None) -> str:
        """View the plain text of an office document (docx/xlsx/pptx)."""
        argv = ["view", "{path}", "text"]
        if page is not None:
            argv += ["--page", str(page)]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_html(file_id: str) -> str:
        """Render an office document to HTML and return it (PPTX/DOCX)."""
        return _run_text(runner, file_id, ["view", "{path}", "html"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_screenshot(file_id: str, page: int | None = None) -> Image:
        """Render a page of an office document to a PNG screenshot."""
        argv = ["view", "{path}", "screenshot"]
        if page is not None:
            argv += ["--page", str(page)]
        res = _run(runner, file_id, argv)
        png = runner.read_image(res.image_path)
        return Image(data=png, format="png")

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_annotated(file_id: str, json: bool = False) -> str:
        """View annotated structure of the document."""
        argv = ["view", "{path}", "annotated"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_outline(file_id: str) -> str:
        """View the document outline (headings/slide titles)."""
        return _run_text(runner, file_id, ["view", "{path}", "outline"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_stats(file_id: str) -> str:
        """View document stats (counts, sizes)."""
        return _run_text(runner, file_id, ["view", "{path}", "stats"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_issues(file_id: str, json: bool = False) -> str:
        """View content/layout issues in the document."""
        argv = ["view", "{path}", "issues"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_get(file_id: str, selector: str, depth: int | None = None, json: bool = False) -> str:
        """Get an element by DOM/CSS selector."""
        argv = ["get", "{path}", selector]
        if depth is not None:
            argv += ["--depth", str(depth)]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_set(file_id: str, selector: str, prop: str) -> str:
        """Set a property on elements matched by selector. prop is 'key=value'."""
        return _run_text(runner, file_id, ["set", "{path}", selector, "--prop", prop])

    @mcp.tool(annotations=_WRITE)
    def officecli_edit(file_id: str, find: str, replace: str) -> str:
        """Find and replace text in the document."""
        return _run_text(runner, file_id, ["set", "{path}", "/find-replace", "--find", find, "--replace", replace])

    @mcp.tool(annotations=_WRITE)
    def officecli_add(file_id: str, selector: str, type: str, prop: str | None = None) -> str:
        """Add an element under the selector."""
        argv = ["add", "{path}", selector, "--type", type]
        if prop:
            argv += ["--prop", prop]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_remove(file_id: str, selector: str) -> str:
        """Remove elements matched by selector."""
        return _run_text(runner, file_id, ["remove", "{path}", selector])

    @mcp.tool(annotations=_WRITE)
    def officecli_move(file_id: str, selector: str, position: int) -> str:
        """Move an element to a new position."""
        return _run_text(runner, file_id, ["move", "{path}", selector, "--to", str(position)])

    @mcp.tool(annotations=_WRITE)
    def officecli_swap(file_id: str, selector_a: str, selector_b: str) -> str:
        """Swap two elements."""
        return _run_text(runner, file_id, ["swap", "{path}", selector_a, selector_b])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_validate(file_id: str, json: bool = False) -> str:
        """Validate the document against the OpenXML schema."""
        argv = ["validate", "{path}"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_batch(file_id: str, commands_json: str) -> str:
        """Run a batch of commands (JSON) in one open/save cycle."""
        return _run_text(runner, file_id, ["batch", "{path}", "--commands", commands_json])

    @mcp.tool(annotations=_WRITE)
    def officecli_create(file_id: str, name: str, type: str) -> str:
        """Create a blank document. name e.g. 'deck.pptx'; type in docx|xlsx|pptx.

        Returns a NEW file_id for the created document (the input file_id is
        only used to host the new file's workdir).
        """
        import os
        new_id = __import__("uuid").uuid4().hex
        new_dir = os.path.join(file_store.work_dir, new_id)
        os.makedirs(new_dir, exist_ok=True)
        argv = ["create", os.path.join(new_dir, name), "--type", type]
        res = runner._raw_run(argv, cwd=new_dir)
        if res.exit_code != 0:
            return _err(f"create failed: {res.stderr}")
        return new_id

    return mcp


def _run(runner: OfficeRunner, file_id: str, argv: list[str]):
    try:
        return runner.run(file_id, argv)
    except FileIDNotFound:
        raise  # let caller decide


def _run_text(runner: OfficeRunner, file_id: str, argv: list[str]) -> str:
    try:
        res = runner.run(file_id, argv)
    except FileIDNotFound:
        return _err(f"file_id '{file_id}' not found or expired")
    if res.exit_code != 0:
        return _err(f"officecli exited {res.exit_code}: {res.stderr.strip()}")
    return res.stdout.strip()
```

Then add the `_raw_run` helper to `runner.py` (used by `officecli_create`). Append this method to the `OfficeRunner` class:

```python
    def _raw_run(self, argv: list[str], cwd: str) -> RunResult:
        """Run an arbitrary officecli argv with no {path} substitution."""
        env = dict(os.environ)
        env["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
        proc = subprocess.run(
            [self.binary_path, *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
        )
        return RunResult(exit_code=proc.returncode, stdout=proc.stdout,
                         stderr=proc.stderr, image_path=None)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_tools.py -v 2>&1 | tail -25`
Expected: 4 passed. (If the screenshot image-assertion is flaky, check that the stub's `-o` parsing matches `runner`'s injected args.)

- [ ] **Step 5: Commit**

```bash
git add src/officecli_mcp/tools.py src/officecli_mcp/runner.py tests/test_tools.py
git commit -m "feat(tools): officecli_* MCP tools (handle-based, prefixed)"
```

---

## Task 6: server.py + __main__.py — wire it together

**Files:**
- Create: `src/officecli_mcp/server.py`
- Create: `src/officecli_mcp/__main__.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test `tests/test_server.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient


def _write_stub(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


def test_files_and_mcp_coexist(settings, tmp_path, monkeypatch):
    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho ok\n")
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))

    app = server_mod.build_app(settings)
    client = TestClient(app)

    # /files works
    up = client.post("/files", files={"file": ("a.docx", b"x", "application/octet-stream")})
    assert up.status_code == 200, up.text
    file_id = up.json()["file_id"]

    # /mcp endpoint responds to an MCP initialize over HTTP
    init_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"accept": "application/json, text/event-stream"},
    )
    # streamable-http returns 200 (SSE) or 202; either is fine for "the endpoint exists"
    assert init_resp.status_code in (200, 202), init_resp.status_code


def test_health(settings, tmp_path, monkeypatch):
    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho ok\n")
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))

    app = server_mod.build_app(settings)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_server.py -v 2>&1 | tail -15`
Expected: FAIL with `ModuleNotFoundError: No module named 'officecli_mcp.server'`

- [ ] **Step 3: Write `src/officecli_mcp/server.py`**

```python
"""Assemble the Starlette app: /files router + /mcp mount, plus lifespan."""
from __future__ import annotations

import contextlib
import logging

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from officecli_mcp import binary
from officecli_mcp.config import Settings
from officecli_mcp.files import FileStore, build_files_router
from officecli_mcp.runner import OfficeRunner
from officecli_mcp.tools import build_mcp

log = logging.getLogger(__name__)


def build_app(settings: Settings) -> Starlette:
    bin_path = binary.ensure_binary(settings.data_dir, settings.officecli_version, settings.officecli_sha256)
    log.info("officecli binary at %s", bin_path)

    file_store = FileStore(work_dir=settings.work_dir, ttl_seconds=settings.work_ttl_seconds)
    runner = OfficeRunner(binary_path=bin_path, file_store=file_store)
    mcp = build_mcp(runner=runner, file_store=file_store)

    files_router = build_files_router(file_store, settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        # Required when mounting mcp.streamable_http_app(): manage the session manager.
        async with mcp.session_manager.run():
            yield

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=files_router),
            Mount("/mcp", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.file_store = file_store
    app.state.mcp = mcp
    return app
```

- [ ] **Step 4: Write `src/officecli_mcp/__main__.py`**

```python
"""CLI entrypoint: dispatch http (uvicorn) or stdio (mcp.run)."""
from __future__ import annotations

import argparse
import logging

import uvicorn

from officecli_mcp.config import get_settings
from officecli_mcp.server import build_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="officecli-mcp")
    parser.add_argument("--transport", choices=["http", "stdio"], default=None,
                        help="http (streamable-HTTP, default) or stdio")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    transport = args.transport or settings.transport
    host = args.host or settings.host
    port = args.port or settings.port

    if transport == "stdio":
        # Build the app once (downloads binary, wires everything), then run
        # the MCP instance it holds over stdio. /files is unreachable in stdio
        # mode (no HTTP server), but binary bootstrap + tool wiring still apply.
        app = build_app(settings)
        app.state.mcp.run(transport="stdio")
        return

    app = build_app(settings)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_server.py -v 2>&1 | tail -20`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/officecli_mcp/server.py src/officecli_mcp/__main__.py tests/test_server.py
git commit -m "feat(server): wire /files + /mcp into one Starlette app, CLI dispatch"
```

---

## Task 7: OpenWebUI native upload shim

**Files:**
- Create: `examples/openwebui_officecli_upload.py`
- Test: `tests/test_upload_shim.py`

- [ ] **Step 1: Write the failing test `tests/test_upload_shim.py`**

Tests the shim against a fake OpenWebUI file endpoint + a real `/files` (via TestClient mounted in-process).

```python
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


def test_shim_fetches_and_posts(settings, tmp_path, monkeypatch):
    from officecli_mcp import server as server_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.tools import build_mcp
    import importlib.util, sys

    # Real officecli-mcp app (with stub binary).
    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    mcp_app = server_mod.build_app(settings)

    # Fake OpenWebUI: serves file bytes at /api/v1/files/{id}/content.
    file_bytes = b"PK\x03\x04real-docx"
    async def fake_content(request):
        return Response(file_bytes, media_type="application/octet-stream")
    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])

    # Load the shim module from examples/ and instantiate its Tools.
    spec = importlib.util.spec_from_file_location(
        "openwebui_officecli_upload", Path("examples/openwebui_officecli_upload.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp",  # we'll patch the client to hit mcp_app
        openwebui_url="http://owui",
        openwebui_api_key="sk-test",
    )

    # Patch the shim's HTTP calls to use the in-process TestClients.
    mcp_client = TestClient(mcp_app)
    owui_client = TestClient(owui)

    monkeypatch.setattr(tools, "_owui_get", lambda file_id: owui_client.get(
        f"/api/v1/files/{file_id}/content", headers={"Authorization": "Bearer sk-test"}).content)
    monkeypatch.setattr(tools, "_mcp_post", lambda fname, data: mcp_client.post(
        "/files", files={"file": (fname, data, "application/octet-stream")}).json())

    result = tools.officecli_upload(
        __files__=[{"id": "f1", "name": "report.docx", "url": "/api/v1/files/f1"}]
    )
    assert "file_id" in result
    assert result["filename"] == "report.docx"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_upload_shim.py -v 2>&1 | tail -15`
Expected: FAIL (file not found / module not importable).

- [ ] **Step 3: Write `examples/openwebui_officecli_upload.py`**

```python
"""OpenWebUI native Tool: upload chat-attached office docs to officecli-mcp.

Install: Workspace > Tools > paste this file. Set Valves:
  - officecli_mcp_url: e.g. http://officecli-mcp:8765
  - openwebui_url:     e.g. http://open-webui:8080  (the in-cluster OWUI base URL)
  - openwebui_api_key: an API key (Account Settings > API Keys) with file access.

Attach this tool to a model alongside the officecli-mcp MCP connection. The model
calls officecli_upload(__files__), gets back a file_id, and passes it to the
officecli_* MCP tools.
"""
from __future__ import annotations

import urllib.request
from typing import Any

import requests


class Tools:
    class Valves:
        def __init__(self):
            self.officecli_mcp_url = "http://officecli-mcp:8765"
            self.openwebui_url = "http://open-webui:8080"
            self.openwebui_api_key = ""

    def __init__(self):
        self.valves = self.Valves()

    # --- swappable HTTP helpers (monkeypatched in tests) ---
    def _owui_get(self, file_id: str) -> bytes:
        url = f"{self.valves.openwebui_url}/api/v1/files/{file_id}/content"
        resp = requests.get(url, headers={"Authorization": f"Bearer {self.valves.openwebui_api_key}"}, timeout=60)
        resp.raise_for_status()
        return resp.content

    def _mcp_post(self, filename: str, data: bytes) -> dict:
        url = f"{self.valves.officecli_mcp_url}/files"
        files = {"file": (filename, data, "application/octet-stream")}
        resp = requests.post(url, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def officecli_upload(self, __files__: list[dict[str, Any]] = []) -> str:
        """Upload attached office files to the officecli-mcp server.

        Args:
            __files__: OpenWebUI-injected list of attached file dicts (have 'id' and 'name').

        Returns:
            JSON string: {"file_id": "...", "filename": "...", "hint": "..."}
            Pass the file_id to the officecli_* MCP tools.
        """
        import json

        if not __files__:
            return json.dumps({"error": "no files attached"})

        out = []
        for f in __files__:
            file_id = f.get("id")
            name = f.get("name") or f.get("filename") or "upload.docx"
            if not file_id:
                # __files__ entries may nest under 'file'
                file_id = (f.get("file") or {}).get("id")
            if not file_id:
                continue
            try:
                data = self._owui_get(file_id)
                info = self._mcp_post(name, data)
                out.append({"file_id": info["file_id"], "filename": info.get("filename", name)})
            except Exception as e:  # noqa: BLE001
                out.append({"filename": name, "error": str(e)})

        return json.dumps({
            "files": out,
            "hint": "Pass each file_id to officecli_* MCP tools (e.g. officecli_view_html).",
        })
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_upload_shim.py -v 2>&1 | tail -20`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add examples/openwebui_officecli_upload.py tests/test_upload_shim.py
git commit -m "feat(examples): OpenWebUI native upload shim tool"
```

---

## Task 8: Dockerfile + docker-compose

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OFFICECLI_MCP_DATA_DIR=/data \
    OFFICECLI_MCP_WORK_DIR=/work

WORKDIR /app

# Install build deps for any C-extension wheels, then remove.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

VOLUME ["/data", "/work"]

EXPOSE 8765

# Binary is fetched at runtime (first start) per design.
ENTRYPOINT ["officecli-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
```

- [ ] **Step 2: Write `.dockerignore`**

```
.git
.github
__pycache__
*.pyc
.venv
venv
.pytest_cache
tests
docs
*.egg-info
/data
/work
officecli
officecli-*
```

- [ ] **Step 3: Write `docker-compose.yml`**

```yaml
services:
  officecli-mcp:
    build: .
    image: officecli-mcp:latest
    container_name: officecli-mcp
    ports:
      - "8765:8765"
    volumes:
      - officecli-data:/data
      - officecli-work:/work
    environment:
      OFFICECLI_MCP_TRANSPORT: http
      OFFICECLI_MCP_HOST: 0.0.0.0
      OFFICECLI_MCP_PORT: "8765"
      # OFFICECLI_MCP_API_KEY: set-to-lock-down-the-http-surface
      # OFFICECLI_SHA256: optional-pin
    restart: unless-stopped

  # Reference: OpenWebUI would connect to http://officecli-mcp:8765/mcp
  # (native MCP, streamable-HTTP) and POST uploads to http://officecli-mcp:8765/files
  # via the officecli_upload native tool.

volumes:
  officecli-data:
  officecli-work:
```

- [ ] **Step 4: Build the image (smoke test)**

Run: `docker build -t officecli-mcp:dev . 2>&1 | tail -15`
Expected: build succeeds (image tagged). If offline, this still works since the binary isn't fetched at build time.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "feat(docker): Dockerfile + compose for officecli-mcp"
```

---

## Task 9: End-to-end verification with the REAL officecli binary

This task validates against the real binary. It is gated behind `OFFICECLI_BIN` so CI without it skips; locally it's the definition of done.

**Files:**
- Create: `tests/test_e2e_real.py`

- [ ] **Step 1: Write `tests/test_e2e_real.py`**

```python
"""End-to-end tests against the REAL officecli binary.

Skipped unless OFFICECLI_BIN points to an executable officecli.
These encode the spec's 'Verification (definition of done)' checklist.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REAL_BIN = os.environ.get("OFFICECLI_BIN")
pytestmark = pytest.mark.skipif(
    not REAL_BIN or not Path(REAL_BIN).exists(),
    reason="set OFFICECLI_BIN to a real officecli binary to run e2e tests",
)


@pytest.fixture
def app(settings, monkeypatch):
    from officecli_mcp import server as server_mod

    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: REAL_BIN)
    return server_mod.build_app(settings)


def test_create_view_html_screenshot_edit_flow(app):
    client = TestClient(app)

    # 1. Create a blank pptx by uploading a minimal one is hard; instead use officecli_create.
    #    First upload a throwaway docx to get a host file_id, then create a pptx from it.
    up = client.post("/files", files={"file": ("seed.docx", b"PK", "application/octet-stream")})
    assert up.status_code == 200
    seed_id = up.json()["file_id"]

    # Use the MCP create tool to make a real deck.pptx
    from mcp.shared.memory import create_connected_server_and_client_session
    mcp = app.state.mcp
    import asyncio

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            # create deck
            r = await session.call_tool("officecli_create", {"file_id": seed_id, "name": "deck.pptx", "type": "pptx"})
            texts = [c.text for c in r.content if hasattr(c, "text")]
            new_id = texts[0].strip()
            assert not new_id.startswith("ERROR"), texts
            # view_html
            r2 = await session.call_tool("officecli_view_html", {"file_id": new_id})
            t2 = "".join(c.text for c in r2.content if hasattr(c, "text"))
            assert "<html" in t2.lower() or "<body" in t2.lower(), t2[:200]
            # view_screenshot
            r3 = await session.call_tool("officecli_view_screenshot", {"file_id": new_id, "page": 1})
            imgs = [c for c in r3.content if getattr(c, "type", None) == "image"]
            assert imgs
            png = base64.b64decode(imgs[0].data)
            assert png.startswith(b"\x89PNG")
            # delete then confirm expired
            client.delete(f"/files/{new_id}")
            r4 = await session.call_tool("officecli_view_html", {"file_id": new_id})
            assert r4.isError

    asyncio.run(run())
```

- [ ] **Step 2: Run the full suite (unit only, e2e should skip)**

Run: `python3 -m pytest -q 2>&1 | tail -20`
Expected: all unit tests pass; e2e test skipped (`1 skipped`).

- [ ] **Step 3: Run e2e with the real binary**

First fetch the real binary (one-time):
```bash
curl -L https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/officecli-linux-x64 -o /tmp/officecli && chmod +x /tmp/officecli
/tmp/officecli --version
```
Then run:
```bash
OFFICECLI_BIN=/tmp/officecli python3 -m pytest tests/test_e2e_real.py -v 2>&1 | tail -25
```
Expected: `test_create_view_html_screenshot_edit_flow` PASSES (real deck created, HTML returned, PNG returned, delete → expired error). This is the definition-of-done from the spec.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_real.py
git commit -m "test: e2e verification against real officecli binary (definition of done)"
```

---

## Task 10: README usage + push

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append usage sections to `README.md`**

Append the following after the existing "## Status" section (replace it):

```markdown
## Quick start (Docker)

```bash
docker compose up -d   # serves http://localhost:8765
```

OpenWebUI: add an MCP connection at `http://officecli-mcp:8765/mcp` (native MCP, streamable-HTTP), and install the `officecli_upload` native tool from `examples/openwebui_officecli_upload.py` with its Valves set.

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
| `officecli_create` | create a blank doc/xlsx/pptx → new file_id |
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
2. Install the native tool `examples/openwebui_officecli_upload.py` (Workspace > Tools); set Valves; make it Public; attach to the model.
3. Add MCP connection: `http://officecli-mcp:8765/mcp` (Settings > Connections).
4. Ensure the OpenWebUI pod can reach the officecli-mcp pod.
```

- [ ] **Step 2: Run the full test suite one final time**

Run: `python3 -m pytest -q 2>&1 | tail -15`
Expected: all unit tests pass, e2e skipped (unless `OFFICECLI_BIN` set).

- [ ] **Step 3: Commit and push**

```bash
git add README.md
git commit -m "docs: usage, tools table, config, OpenWebUI setup"
git push origin main
```

---

## Notes for the implementer

- **Import path discipline:** all SDK imports use `from mcp.server.fastmcp import FastMCP, Image` and `from mcp.types import ToolAnnotations`. Don't switch to the standalone `fastmcp` package mid-build.
- **`{path}` token:** the runner replaces the literal string `{path}` in argv with the resolved file path. Tools must always include it where the file goes.
- **No `shell=True`:** argv is always a list passed to `subprocess.run`. Never interpolate user input into a shell string.
- **Test isolation:** `tests/conftest.py` adds `src/` to `sys.path` so tests work without an installed package; the editable install (`pip install -e .`) is for the console script and runtime.
- **The stub binary pattern:** runner/tools tests use a tiny `/bin/sh` stub that prints canned output (and writes a fake PNG for `-o`). This keeps tests fast and binary-free. Only `test_e2e_real.py` uses the real officecli.
- **If `mcp.session_manager.run()` API differs:** the verified pattern is `async with mcp.session_manager.run(): yield` inside the Starlette lifespan. If your installed mcp version raises `AttributeError`, check `mcp` version (`pip show mcp`) — it must be `>=1.27,<2`.
```
