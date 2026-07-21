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
