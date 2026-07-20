"""End-to-end tests against the REAL officecli binary.

Skipped unless OFFICECLI_BIN points to an executable officecli.
These encode the spec's 'Verification (definition of done)' checklist.
"""
from __future__ import annotations

import asyncio
import base64
import json
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
            create_out = "\n".join(texts)
            new_id = create_out.splitlines()[0].strip()
            assert not new_id.startswith("ERROR"), texts
            # The real binary prints slide dimensions on create; the tool must
            # surface them so the model can size objects to the page.
            assert "slideWidth" in create_out, create_out
            assert "slideHeight" in create_out, create_out

            # Add a slide so screenshot has a page to render (blank deck has 0 slides).
            await session.call_tool(
                "officecli_add", {"file_id": new_id, "selector": "/", "type": "slide"}
            )

            # 2. view_html -> text (default compact mode strips tags but keeps
            # visible text like slide titles; just confirm it returned content).
            r2 = await session.call_tool("officecli_view_html", {"file_id": new_id})
            t2 = "".join(c.text for c in r2.content if hasattr(c, "text"))
            assert t2.strip(), t2[:200]

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
            create_out = "\n".join(c.text for c in r.content if hasattr(c, "text"))
            new_id = create_out.splitlines()[0].strip()
            assert not new_id.startswith("ERROR"), new_id
            # create surfaces slide dimensions now.
            assert "slideWidth" in create_out, create_out

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

def test_batch_with_docstring_example_schema(app):
    """The officecli_batch docstring shows an exact JSON schema. This test runs
    a batch built to that schema against the real binary to guarantee the
    documented example actually works - so the model can copy it verbatim."""
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    client = TestClient(app)
    mcp = app.state.mcp
    up = client.post("/files", files={"file": ("seed.pptx", b"PK", "application/octet-stream")})
    assert up.status_code == 200
    seed_id = up.json()["file_id"]

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            await session.initialize()
            cr = await session.call_tool(
                "officecli_create", {"file_id": seed_id, "name": "b.pptx", "type": "pptx"}
            )
            new_id = "\n".join(c.text for c in cr.content if hasattr(c, "text")).splitlines()[0].strip()
            # Schema straight from the docstring: parent for add, path for set,
            # props as a key->value MAP.
            cmds = json.dumps(
                [
                    {"command": "add", "parent": "/", "type": "slide"},
                    {
                        "command": "add",
                        "parent": "/slide[1]",
                        "type": "shape",
                        "props": {"x": "1cm", "y": "1cm", "width": "5cm", "height": "3cm"},
                    },
                    {"command": "set", "path": "/slide[1]/shape[1]", "props": {"line": "none"}},
                ]
            )
            r = await session.call_tool(
                "officecli_batch", {"file_id": new_id, "commands_json": cmds}
            )
            out = "\n".join(c.text for c in r.content if hasattr(c, "text"))
            assert not r.isError, out
            # All items must succeed (atomic mode rolls back on any failure).
            assert "3 succeeded" in out, out
            assert "0 failed" in out, out

    asyncio.run(run())


# --- /tools + /tools/call e2e against the real binary ---


@pytest.fixture
def real_app_client(app):
    return TestClient(app)


def test_tools_endpoint_lists_real_tools(real_app_client):
    resp = real_app_client.get("/tools")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["tools"]}
    assert "officecli_create" in names


def test_tools_call_create_and_view_roundtrip(real_app_client):
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_create", "arguments": {"name": "http.pptx", "type": "pptx", "file_id": "x"}},
    )
    body = resp.json()
    assert body["isError"] is False
    file_id = body["content"][0]["text"].splitlines()[0]
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_view_text", "arguments": {"file_id": file_id}},
    )
    assert resp.json()["isError"] is False


def test_tools_call_screenshot_respects_max_edge(real_app_client):
    import base64
    import io

    from PIL import Image

    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_create", "arguments": {"name": "shot.pptx", "type": "pptx", "file_id": "x"}},
    )
    file_id = resp.json()["content"][0]["text"].splitlines()[0]
    # officecli v1.0.139 refuses to screenshot an empty deck ("--page 1 out of
    # range (total slides: 0)"), so add a slide first.
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_add", "arguments": {"file_id": file_id, "selector": "/", "type": "slide"}},
    )
    assert resp.json()["isError"] is False
    resp = real_app_client.post(
        "/tools/call",
        json={"name": "officecli_view_screenshot", "arguments": {"file_id": file_id}},
    )
    body = resp.json()
    assert body["isError"] is False
    img_block = next(b for b in body["content"] if b["type"] == "image")
    with Image.open(io.BytesIO(base64.b64decode(img_block["data"]))) as im:
        assert max(im.size) <= 1024