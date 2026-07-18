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


def test_stage_image_into_pptx(app, tmp_path):
    """Definition of done: stage a real PNG -> add picture -> screenshot shows it.

    Mirrors the spec section 5.3 verification: stage asset, add picture with src=asset,
    view_screenshot to confirm the image appears on the slide.
    """
    client = TestClient(app)
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = app.state.mcp

    # 1. Seed a host workdir, then create a real deck.pptx + a slide.
    up = client.post("/files", files={"file": ("seed.docx", b"PK", "application/octet-stream")})
    assert up.status_code == 200
    seed_id = up.json()["file_id"]

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            r = await session.call_tool(
                "officecli_create", {"file_id": seed_id, "name": "deck.pptx", "type": "pptx"}
            )
            new_id = [c.text for c in r.content if hasattr(c, "text")][0].strip()
            assert not new_id.startswith("ERROR"), new_id

            await session.call_tool(
                "officecli_add", {"file_id": new_id, "selector": "/", "type": "slide"}
            )
            return new_id

    new_id = asyncio.run(run())

    # 2. Stage a real PNG into the deck's workdir.
    # Use a minimal valid PNG (1x1) generated here so the test is self-contained.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    stage = client.post(
        "/files/stage",
        data={"target_file_id": new_id, "filename": "kimi.png"},
        files={"file": ("kimi.png", png, "image/png")},
    )
    assert stage.status_code == 200, stage.text
    assert stage.json()["asset"] == "kimi.png"

    # 3. add picture with src=kimi.png and sizing/position.
    async def add_and_view():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            r = await session.call_tool(
                "officecli_add",
                {
                    "file_id": new_id,
                    "selector": "/slide[1]",
                    "type": "picture",
                    "prop": ["src=kimi.png", "width=5in", "height=3in", "x=1in", "y=1in"],
                },
            )
            assert not r.isError, [c.text for c in r.content if hasattr(c, "text")]
            # 4. view_screenshot -> image content returned (picture is on the slide).
            r2 = await session.call_tool(
                "officecli_view_screenshot", {"file_id": new_id, "page": 1}
            )
            imgs = [c for c in r2.content if getattr(c, "type", None) == "image"]
            assert imgs, f"expected a screenshot image, got {r2.content}"

    asyncio.run(add_and_view())

    # 5. Download the pptx and confirm the image part is embedded.
    dl = client.get(f"/files/{new_id}")
    assert dl.status_code == 200
    # A pptx with an embedded picture contains an image part (png) in the zip.
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(dl.content))
    names = z.namelist()
    assert any("media" in n and n.endswith(".png") for n in names), names