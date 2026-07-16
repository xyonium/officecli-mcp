from __future__ import annotations

import importlib.util
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


def test_shim_fetches_and_posts(settings, tmp_path, monkeypatch):
    from officecli_mcp import server as server_mod

    # Real officecli-mcp app (with stub binary).
    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    mcp_app = server_mod.build_app(settings)

    # Fake OpenWebUI: serves file bytes at /api/v1/files/{id}/content,
    # echoing the received Authorization header to prove credentials are forwarded.
    file_bytes = b"PK\x03\x04real-docx"

    async def fake_content(request):
        auth = request.headers.get("authorization", "(none)")
        return Response(file_bytes + b"|" + auth.encode(), media_type="application/octet-stream")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])

    # Load the shim module from examples/.
    spec = importlib.util.spec_from_file_location(
        "openwebui_officecli_upload", Path("examples/openwebui_officecli_upload.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp",
        openwebui_url="http://owui",
    )

    mcp_client = TestClient(mcp_app)
    owui_client = TestClient(owui)

    # Fake __request__ carrying the current user's Bearer token.
    class FakeRequest:
        def __init__(self, headers: dict[str, str]):
            self.headers = headers

    fake_req = FakeRequest({"authorization": "Bearer current-user-token"})

    # Patch the HTTP helpers to route through the in-process TestClients while
    # still exercising the real credential-forwarding logic.
    monkeypatch.setattr(
        tools,
        "_owui_get",
        lambda file_id, __request__: owui_client.get(
            f"/api/v1/files/{file_id}/content",
            headers=tools._owui_headers(__request__),
        ).content,
    )
    monkeypatch.setattr(
        tools,
        "_mcp_post",
        lambda fname, data: mcp_client.post(
            "/files", files={"file": (fname, data, "application/octet-stream")}
        ).json(),
    )

    import json

    result = json.loads(
        tools.officecli_upload(
            __files__=[{"id": "f1", "name": "report.docx", "url": "/api/v1/files/f1"}],
            __request__=fake_req,
        )
    )
    assert result["files"], result
    assert result["files"][0]["file_id"]
    assert result["files"][0]["filename"] == "report.docx"
    assert "officecli_" in result["hint"]


def test_owui_headers_forwards_authorization():
    """The shim must forward the current user's Authorization header, not use a stored key."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "owui_shim", Path("examples/openwebui_officecli_upload.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeRequest:
        def __init__(self, h):
            self.headers = h

    tools = mod.Tools()
    h = tools._owui_headers(FakeRequest({"authorization": "Bearer u123", "cookie": "sid=abc"}))
    assert h["Authorization"] == "Bearer u123"
    assert h["Cookie"] == "sid=abc"

    # No request -> no credentials (does not fabricate a key).
    assert tools._owui_headers(None) == {}
