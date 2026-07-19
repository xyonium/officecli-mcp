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


async def test_set_docstring_tells_model_to_batch_props(mcp_server):
    """Regression guard: the model kept calling set once per property (one prop
    per call -> 4 calls to position a textbox) because the docstring didn't say
    multiple props go in one call. The docstring must explicitly tell it to pass
    every property in a single call and show a multi-item example."""
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
    set_tool = next(t for t in tools.tools if t.name == "officecli_set")
    desc = (set_tool.description or "").lower()
    assert "single call" in desc or "one call" in desc, set_tool.description
    # Must show a multi-item example so the model copies the shape.
    assert "x=2cm" in (set_tool.description or "").lower(), set_tool.description


async def test_add_rejects_url_src_with_stage_guidance(mcp_server, tmp_path):
    """officecli_add must refuse src= URLs/paths and point the model at stage,
    instead of letting officecli fail with a confusing SSRF error.

    The model repeatedly tries to pass an OpenWebUI file URL directly as src=,
    which officecli's SSRF guard blocks (internal docker IP). Fail fast with a
    hint to call officecli_file(action="stage") first.
    """
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
                "prop": [
                    "src=http://open-webui:8080/api/v1/files/abc/content",
                    "width=5in",
                ],
            },
        )
        assert res.isError, res.content
        text = " ".join(c.text for c in res.content if hasattr(c, "text")).lower()
        assert "stage" in text
        # Must NOT have shelled out to officecli (no argv recorded).
        assert not rec.exists() or rec.read_text().strip() == ""


async def test_add_rejects_relative_api_path_src(mcp_server, tmp_path):
    """Same guard for src=/api/v1/files/... (no scheme) - also not a staged asset."""
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
                "prop": ["src=/api/v1/files/abc/content"],
            },
        )
        assert res.isError
        text = " ".join(c.text for c in res.content if hasattr(c, "text")).lower()
        assert "stage" in text


async def test_add_allows_local_staged_src(mcp_server, tmp_path):
    """A bare filename (staged asset) is NOT rejected - only URLs/paths are."""
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
                "prop": ["src=kimi.png", "width=5in"],
            },
        )
        assert not res.isError, res.content
    argv = rec.read_text().splitlines()
    assert "src=kimi.png" in argv


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


async def test_instructions_teach_stage_workflow(mcp_server):
    """The model learns 'stage->add picture / import' from server instructions."""
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.initialize()
    text = (getattr(result, "instructions", None) or "").lower()
    assert "stage" in text
    assert "picture" in text
    assert "officecli_import" in text


async def test_import_tool_listed(mcp_server):
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert "officecli_import" in {t.name for t in tools.tools}


async def test_create_surfaces_slide_dimensions(settings, tmp_path):
    """officecli_create must return the new file_id AND the create stdout,
    which carries slideWidth/slideHeight. Without the dimensions the model
    can't size textboxes/pictures to fill the slide - it only knows a
    bare file_id and guesses sizes, leaving objects in the top-left quadrant.
    """
    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    # Stub mimics officecli `create` stdout: prints the slide dimensions.
    stub = tmp_path / "officecli"
    _write_stub(
        stub,
        "#!/bin/sh\n"
        "if [ \"$1\" = \"create\" ]; then\n"
        "  echo 'Created: deck.pptx'\n"
        "  echo '  totalSlides: 0'\n"
        "  echo '  slideWidth: 960pt'\n"
        "  echo '  slideHeight: 540pt'\n"
        "else\n"
        "  echo OK\n"
        "fi\n",
    )
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    mcp = tools_mod.build_mcp(runner=runner, file_store=store)

    # A host file_id must exist (its workdir hosts the new file).
    host = store.put("host.pptx", b"PK\x03\x04host")
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_create",
            {"file_id": host["file_id"], "name": "deck.pptx", "type": "pptx"},
        )
    assert not res.isError, res.content
    text = " ".join(c.text for c in res.content if hasattr(c, "text"))
    # The model must see the page dimensions so it can size objects to fit.
    assert "960pt" in text, text
    assert "540pt" in text, text


async def test_instructions_teach_slide_sizing(mcp_server):
    """Instructions must tell the model the 16:9 pptx page size and that
    add picture stretches (no crop/fit), so it generates images in the right
    aspect ratio and uses full-bleed coordinates instead of guessing."""
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.initialize()
    text = (getattr(result, "instructions", None) or "").lower()
    # The full-bleed width/height for 16:9 pptx must appear.
    assert "33.87cm" in text or "960pt" in text, text
    # And the model must be warned pictures stretch (no auto-crop).
    assert "stretch" in text or "no crop" in text or "aspect ratio" in text, text


async def test_run_text_error_includes_stdout_not_just_stderr(settings, tmp_path):
    """officecli writes partial-failure context to stdout (e.g. 'No properties
    applied to /slide[1]' alongside a stderr 'UNSUPPORTED props' list). The
    ToolError on non-zero exit must include stdout when present, or the model
    loses the 'what actually happened' half of the error."""

    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.pptx", b"PK\x03\x04pptx")
    # Stub: prints context to stdout, an error to stderr, exits non-zero -
    # mirrors officecli's real 'set unsupported prop' behavior.
    stub = tmp_path / "officecli"
    _write_stub(
        stub,
        "#!/bin/sh\necho 'No properties applied to /slide[1]'\n"
        "echo 'UNSUPPORTED props: bogusprop' 1>&2\nexit 2\n",
    )
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    mcp = tools_mod.build_mcp(runner=runner, file_store=store)

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        res = await session.call_tool(
            "officecli_set",
            {"file_id": info["file_id"], "selector": "/slide[1]", "prop": ["bogusprop=1"]},
        )
    # Non-zero exit -> isError; the stdout context must reach the model.
    assert res.isError, res.content
    err_text = " ".join(c.text for c in res.content if hasattr(c, "text"))
    assert "UNSUPPORTED props" in err_text, err_text  # from stderr (already worked)
    assert "No properties applied" in err_text, err_text  # from stdout (was dropped)


async def test_get_docstring_advertises_size_and_format(mcp_server):
    """officecli_get --json returns the element's current x/y/width/height and
    effective properties - exactly what the model needs to fix 'objects only
    fill the top-left quadrant' (get current size, compare to page, set new
    size). The docstring must say so or the model won't reach for it."""
    mcp, _ = mcp_server
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = await session.list_tools()
    get_tool = next(t for t in tools.tools if t.name == "officecli_get")
    desc = (get_tool.description or "").lower()
    # Must advertise that it returns position/size/format.
    assert "size" in desc or "width" in desc or "position" in desc, get_tool.description
    assert "json" in desc, get_tool.description  # --json gives structured format
