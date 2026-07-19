from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


def _make_app(settings):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from officecli_mcp.files import FileStore, build_files_router

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=settings.work_ttl_seconds)
    app = Starlette(routes=[Mount("/", app=build_files_router(store, settings))])
    app.state.settings = settings
    app.state.file_store = store
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
    os.utime(d, (old, old))
    store.sweep()
    assert not d.exists()


def test_path_for_refreshes_mtime_so_active_docs_survive_sweep(settings):
    """A frequently-read document should not be swept even if it was created
    long ago - path_for touches the workdir mtime on access."""
    from officecli_mcp.files import FileStore
    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    doc = store.put("deck.pptx", b"PK\x03\x04pptx")
    d = Path(settings.work_dir, doc["file_id"])
    # Backdate creation to well beyond TTL, as if the doc sat idle for days.
    ancient = time.time() - (3600 + 600)
    os.utime(d, (ancient, ancient))
    # Now access it (model reads/views the doc) - mtime should refresh to ~now.
    store.path_for(doc["file_id"])
    mtime_after_access = d.stat().st_mtime
    assert mtime_after_access > ancient + 1000  # refreshed, not ancient anymore
    # Sweep should NOT remove it now that it was recently accessed.
    store.sweep()
    assert d.exists()


def test_upload_triggers_lazy_sweep(settings, tmp_path):
    """Uploading a new file sweeps idle workdirs (lazy cleanup wiring)."""
    app, store = _make_app(settings)
    client = TestClient(app)
    # Create an old, idle workdir directly.
    old_id = "old-idle-doc"
    d = Path(settings.work_dir, old_id)
    d.mkdir(parents=True)
    (d / "stale.docx").write_bytes(b"x")
    ancient = time.time() - (settings.work_ttl_seconds + 60)
    os.utime(d, (ancient, ancient))
    assert d.exists()
    # Uploading a new file should sweep the stale one away.
    client.post("/files", files={"file": ("new.docx", b"y", "application/octet-stream")})
    assert not d.exists(), "lazy sweep on upload should have removed the idle workdir"


def test_default_work_ttl_is_48h():
    """Default TTL is 48 hours (172800s), not the old 1h, so long sessions
    don't lose documents mid-conversation."""
    # Construct with no env override (Settings reads env; ensure unset).
    import os as _os

    from officecli_mcp.config import Settings
    saved = _os.environ.pop("OFFICECLI_MCP_WORK_TTL_SECONDS", None)
    try:
        assert Settings().work_ttl_seconds == 48 * 60 * 60
    finally:
        if saved is not None:
            _os.environ["OFFICECLI_MCP_WORK_TTL_SECONDS"] = saved


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
