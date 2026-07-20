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
