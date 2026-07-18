from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session


def _write_stub(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


@pytest.fixture
def mcp_server(settings, tmp_path):
    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho 'TEXT-OUT'\n")
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    return tools_mod.build_mcp(runner=runner, file_store=store), store


async def test_list_tools_has_prefixed_names(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert "officecli_view_html" in names
        assert "officecli_view_screenshot" in names
        assert "officecli_create" in names
        # Ensure no unprefixed collisions
        assert all(n.startswith("officecli_") for n in names)


async def test_server_instructions_teach_upload_workflow(mcp_server):
    """The model learns 'upload first, then use file_id' from server instructions."""
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.initialize()
    # instructions surface in the InitializeResult; fall back to the FastMCP attr.
    text = (getattr(result, "instructions", None) or "").lower()
    assert "file_id" in text
    assert "officecli_file" in text


async def test_view_html_returns_text(mcp_server):
    mcp, store = mcp_server
    info = store.put("r.docx", b"docx-bytes")
    # Override stub to emit HTML.
    Path(mcp._runner.binary_path).write_text("#!/bin/sh\necho '<html>HI</html>'\n")
    Path(mcp._runner.binary_path).chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool("officecli_view_html", {"file_id": info["file_id"]})
        texts = [c.text for c in res.content if hasattr(c, "text")]
        assert any("HI" in t for t in texts)


async def test_view_screenshot_returns_image(mcp_server, tmp_path, settings):
    mcp, store = mcp_server
    info = store.put("r.pptx", b"pptx-bytes")
    # Stub writes a fake PNG signature to -o path.
    stub = Path(mcp._runner.binary_path)
    stub.write_bytes(
        b"#!/bin/sh\no='';while [ $# -gt 0 ];do [ \"$1\" = '-o' ]&&o=\"$2\";shift;done;"
        b"[ -n \"$o\" ]&&printf '%s' \"$(echo iVBORw0KGgo= | base64 -d)\" >\"$o\"\n"
    )
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_view_screenshot", {"file_id": info["file_id"], "page": 1}
        )
        imgs = [c for c in res.content if getattr(c, "type", None) == "image"]
        assert imgs, f"expected an image block, got {res.content}"


async def test_unknown_file_id_is_error(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool("officecli_view_html", {"file_id": "ghost"})
        assert res.isError
        texts = [c.text for c in res.content if hasattr(c, "text")]
        assert any("not found" in t.lower() for t in texts)
