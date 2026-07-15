from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


def _make_app(settings):
    from officecli_mcp.files import build_files_router, FileStore
    from starlette.applications import Starlette
    from starlette.routing import Mount

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
