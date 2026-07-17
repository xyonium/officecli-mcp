from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient


def _write_stub(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


def test_files_and_health_work(settings, tmp_path, monkeypatch):
    """Custom /files and /health routes serve without needing the MCP session manager."""
    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho ok\n")
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))

    app = server_mod.build_app(settings)
    client = TestClient(app)

    up = client.post("/files", files={"file": ("a.docx", b"x", "application/octet-stream")})
    assert up.status_code == 200, up.text
    file_id = up.json()["file_id"]

    dl = client.get(f"/files/{file_id}")
    assert dl.status_code == 200
    assert dl.content == b"x"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_mcp_endpoint_serves_via_real_http(settings, tmp_path, monkeypatch):
    """The /mcp streamable-HTTP endpoint responds to a real MCP client.

    Uses uvicorn on a real port + streamable_http_client (the same client
    OpenWebUI uses), so the session-manager lifespan actually runs.
    """
    import threading
    import time

    import uvicorn
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho ok\n")
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))

    app = server_mod.build_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=8772, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(2.0)

    async def probe():
        async with streamable_http_client("http://127.0.0.1:8772/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    try:
        names = asyncio.run(probe())
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert "officecli_view_html" in names
    assert all(n.startswith("officecli_") for n in names)


# --- DNS-rebinding / Host-header guard (mcp sdk 1.28 transport_security) ---
#
# OpenWebUI reaches us cross-container as http://officecli-mcp:8765/mcp, so the
# inbound Host header is "officecli-mcp:8765". FastMCP auto-enables its
# DNS-rebinding guard when it *thinks* the server is localhost-only and
# allow-lists only 127.0.0.1/localhost/[::1]. We must let an operator declare
# the names the MCP endpoint is reachable by, and (per the user's choice)
# keep the guard ON by default while allow-listing those names.

_MCP_INIT = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
    b'"params":{"protocolVersion":"2024-11-05","capabilities":{},'
    b'"clientInfo":{"name":"t","version":"1"}}}'
)
_MCP_HEADERS = {
    "Host": "officecli-mcp:8765",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _build_app(settings, tmp_path, monkeypatch):
    from officecli_mcp import server as server_mod

    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho ok\n")
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    return server_mod.build_app(settings)


def _settings_from_env(settings):
    """Rebuild a Settings from the current env but keep the fixture's tmp dirs.

    The env-driven guard fields (allowed_hosts, dns_rebinding_protection) are
    re-read via the factory / explicit args so monkeypatched env is honored.
    """
    from officecli_mcp.config import Settings, _env_bool, _parse_allowed_hosts

    return Settings(
        transport=settings.transport,
        host=settings.host,
        port=settings.port,
        data_dir=settings.data_dir,
        work_dir=settings.work_dir,
        work_ttl_seconds=settings.work_ttl_seconds,
        max_upload_mb=settings.max_upload_mb,
        officecli_version=settings.officecli_version,
        officecli_sha256=settings.officecli_sha256,
        api_key=settings.api_key,
        allowed_extensions=settings.allowed_extensions,
        dns_rebinding_protection=_env_bool("OFFICECLI_MCP_DNS_REBINDING_PROTECTION", True),
        allowed_hosts=_parse_allowed_hosts(),
    )


def _post_initialize(client):
    return client.post("/mcp", content=_MCP_INIT, headers=_MCP_HEADERS)


def test_guard_allows_remote_host_when_allowlisted(settings, tmp_path, monkeypatch):
    """With docker service name in OFFICECLI_MCP_ALLOWED_HOSTS, /mcp accepts it."""
    monkeypatch.setenv("OFFICECLI_MCP_ALLOWED_HOSTS", "officecli-mcp:8765")
    s = _settings_from_env(settings)
    assert "officecli-mcp:8765" in s.allowed_hosts
    app = _build_app(s, tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = _post_initialize(client)
    assert resp.status_code != 421, f"got 421 Invalid Host: {resp.text}"
    assert "Invalid Host header" not in resp.text


def test_dns_rebinding_guard_rejects_non_allowlisted_host_by_default(
    settings, tmp_path, monkeypatch
):
    """Without an allow-list entry, the guard rejects the cross-container host.

    Locks in the behavior the user explicitly chose: protection stays ON by
    default, so an undeclared host gets 421 rather than silently passing.
    """
    monkeypatch.delenv("OFFICECLI_MCP_ALLOWED_HOSTS", raising=False)
    s = _settings_from_env(settings)
    app = _build_app(s, tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = _post_initialize(client)
    assert resp.status_code == 421, (resp.status_code, resp.text)
    assert resp.text == "Invalid Host header"


def test_dns_rebinding_protection_can_be_disabled(settings, tmp_path, monkeypatch):
    """OFFICECLI_MCP_DNS_REBINDING_PROTECTION=0 turns the guard fully off."""
    monkeypatch.setenv("OFFICECLI_MCP_DNS_REBINDING_PROTECTION", "0")
    monkeypatch.delenv("OFFICECLI_MCP_ALLOWED_HOSTS", raising=False)
    s = _settings_from_env(settings)
    app = _build_app(s, tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = _post_initialize(client)
    assert resp.status_code != 421, f"unexpected 421 with protection off: {resp.text}"
