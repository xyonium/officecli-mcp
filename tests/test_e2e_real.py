"""End-to-end tests against the REAL officecli binary.

Skipped unless OFFICECLI_BIN points to an executable officecli.
These encode the spec's 'Verification (definition of done)' checklist.
"""
from __future__ import annotations

import asyncio
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


def test_create_view_html_screenshot_delete_flow(app):
    """Definition of done: create deck -> view_html -> view_screenshot -> delete -> expired."""
    client = TestClient(app)
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = app.state.mcp

    # Seed: upload a throwaway docx to get a host workdir/file_id.
    up = client.post("/files", files={"file": ("seed.docx", b"PK", "application/octet-stream")})
    assert up.status_code == 200
    seed_id = up.json()["file_id"]

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            # 1. create a real deck.pptx
            r = await session.call_tool(
                "officecli_create", {"file_id": seed_id, "name": "deck.pptx", "type": "pptx"}
            )
            texts = [c.text for c in r.content if hasattr(c, "text")]
            new_id = texts[0].strip()
            assert not new_id.startswith("ERROR"), texts

            # Add a slide so screenshot has a page to render (blank deck has 0 slides).
            await session.call_tool(
                "officecli_add", {"file_id": new_id, "selector": "/", "type": "slide"}
            )

            # 2. view_html -> HTML text
            r2 = await session.call_tool("officecli_view_html", {"file_id": new_id})
            t2 = "".join(c.text for c in r2.content if hasattr(c, "text"))
            assert "<html" in t2.lower() or "<body" in t2.lower(), t2[:200]

            # 3. view_screenshot -> base64 PNG image block
            r3 = await session.call_tool(
                "officecli_view_screenshot", {"file_id": new_id, "page": 1}
            )
            imgs = [c for c in r3.content if getattr(c, "type", None) == "image"]
            assert imgs, f"expected an image block, got {r3.content}"
            png = base64.b64decode(imgs[0].data)
            assert png.startswith(b"\x89PNG")

            # 4. delete then confirm expired (ToolError -> isError)
            client.delete(f"/files/{new_id}")
            r4 = await session.call_tool("officecli_view_html", {"file_id": new_id})
            assert r4.isError

    asyncio.run(run())
