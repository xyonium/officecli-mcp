from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session


def _write_stub_argv(path: Path, rec: Path) -> None:
    """Write a shell stub that records its argv to a file, then prints OK."""
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {rec}\necho OK\n")
    path.chmod(0o755)


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


async def test_add_multi_prop_emits_multiple_dash_prop(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.pptx", b"pptx-bytes")
    rec = tmp_path / "argv.txt"
    _write_stub_argv(Path(mcp._runner.binary_path), rec)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_add",
            {
                "file_id": info["file_id"],
                "selector": "/slide[1]",
                "type": "picture",
                "prop": ["src=kimi.png", "width=5in", "x=1cm", "y=1cm"],
            },
        )
        assert not res.isError, res.content
    argv = rec.read_text().splitlines()
    prop_indices = [i for i, a in enumerate(argv) if a == "--prop"]
    assert len(prop_indices) == 4
    assert argv[prop_indices[0] + 1] == "src=kimi.png"
    assert argv[prop_indices[1] + 1] == "width=5in"


async def test_add_no_prop_omits_flag(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.pptx", b"pptx-bytes")
    rec = tmp_path / "argv.txt"
    _write_stub_argv(Path(mcp._runner.binary_path), rec)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool(
            "officecli_add",
            {"file_id": info["file_id"], "selector": "/", "type": "slide"},
        )
    argv = rec.read_text().splitlines()
    assert "--prop" not in argv


async def test_set_multi_prop(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.docx", b"docx-bytes")
    rec = tmp_path / "argv.txt"
    _write_stub_argv(Path(mcp._runner.binary_path), rec)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool(
            "officecli_set",
            {
                "file_id": info["file_id"],
                "selector": "/body/p[1]",
                "prop": ["bold=true", "size=14"],
            },
        )
    argv = rec.read_text().splitlines()
    assert argv.count("--prop") == 2


async def test_import_tool_argv(mcp_server, tmp_path):
    mcp, store = mcp_server
    info = store.put("r.xlsx", b"xlsx-bytes")
    rec = tmp_path / "argv.txt"
    stub = Path(mcp._runner.binary_path)
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {rec}\necho IMPORTED\n")
    stub.chmod(0o755)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_import",
            {
                "file_id": info["file_id"],
                "sheet": "/Sheet1",
                "source": "kpi.csv",
                "header": True,
                "start_cell": "B2",
                "format": "csv",
            },
        )
        assert not res.isError, res.content
    argv = rec.read_text().splitlines()
    assert argv[0] == "import"
    assert "/Sheet1" in argv
    assert "kpi.csv" in argv
    assert "--header" in argv
    assert "--start-cell" in argv
    assert argv[argv.index("--start-cell") + 1] == "B2"
    assert "--format" in argv
    assert argv[argv.index("--format") + 1] == "csv"


async def test_import_tool_listed(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert "officecli_import" in {t.name for t in tools.tools}
