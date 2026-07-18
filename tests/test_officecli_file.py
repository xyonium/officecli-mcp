from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_tools():
    spec = importlib.util.spec_from_file_location(
        "openwebui_officecli_file", Path("examples/openwebui_officecli_file.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_download_action_pushes_to_owui_storage(monkeypatch):
    """download: GET bytes from officecli-mcp, POST to OWUI storage, return browser URL."""
    from officecli_mcp import server as server_mod

    # Real officecli-mcp app (stub binary not needed for /files GET, but build_app
    # expects a binary; stub it).
    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S",
        (),
        {
            "transport": "http",
            "host": "127.0.0.1",
            "port": 8765,
            "data_dir": "/tmp/_fdata",
            "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600,
            "max_upload_mb": 50,
            "officecli_version": "latest",
            "officecli_sha256": "",
            "api_key": "",
            "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        },
    )()
    import shutil

    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)

    # Put a file into the store directly so /files/{id} serves it.
    file_bytes = b"PK\x03\x04downloaded-pptx-bytes"
    put = TestClient(mcp_app).post(
        "/files", files={"file": ("Kimi_K3.pptx", file_bytes, "application/octet-stream")}
    )
    assert put.status_code == 200, put.text
    file_id = put.json()["file_id"]

    # Fake OpenWebUI: accepts POST /api/v1/files/?process=false, echoes the
    # received Authorization header and returns {"id": "owui-xyz"}.
    received_auth = {}

    async def fake_upload(request):
        received_auth["auth"] = request.headers.get("authorization")
        received_auth["process"] = request.query_params.get("process")
        return Response(json.dumps({"id": "owui-xyz"}), media_type="application/json")

    owui = Starlette(routes=[Route("/api/v1/files/", fake_upload, methods=["POST"])])
    owui_client = TestClient(owui)
    mcp_client = TestClient(mcp_app)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp",
        openwebui_url="http://owui",
        openwebui_browser_url="https://ai.savorcare.com",
    )

    monkeypatch.setattr(
        tools,
        "_mcp_get",
        lambda fid: mcp_client.get(f"/files/{fid}"),
    )
    monkeypatch.setattr(
        tools,
        "_owui_post",
        lambda fname, data, mime, _req: owui_client.post(
            "/api/v1/files/?process=false",
            headers=tools._owui_headers(FakeRequest({"authorization": "Bearer current-user-token"})),
            files={"file": (fname, data, mime)},
        ).json(),
    )

    result = json.loads(
        tools.officecli_file(
            action="download",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
            file_id=file_id,
        )
    )
    assert result["url"] == "https://ai.savorcare.com/api/v1/files/owui-xyz/content", result
    assert result["filename"] == "Kimi_K3.pptx", result
    assert result["size"] == len(file_bytes), result
    assert received_auth["auth"] == "Bearer current-user-token", received_auth
    assert received_auth["process"] == "false", received_auth
