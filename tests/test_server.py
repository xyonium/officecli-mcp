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
