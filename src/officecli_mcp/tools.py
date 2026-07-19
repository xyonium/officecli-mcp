"""officecli_* MCP tool definitions (handle-based)."""
from __future__ import annotations

import logging
import os
import uuid

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from officecli_mcp.files import FileStore
from officecli_mcp.runner import FileIDNotFound, OfficeRunner

log = logging.getLogger(__name__)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def build_mcp(
    runner: OfficeRunner,
    file_store: FileStore,
    *,
    host: str = "0.0.0.0",
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    mcp = FastMCP(
        "officecli-mcp",
        instructions=(
            "Operate on Office documents (.docx/.xlsx/.pptx) by handle, never by path. "
            "WORKFLOW: the model never holds file bytes. First get a file_id: if the user "
            "attached a file in OpenWebUI, call the separate `officecli_file` native tool "
            "with action=\"upload\" (it fetches the bytes and returns a file_id); or call "
            "`officecli_create` to make a blank doc. Then pass that file_id to the "
            "officecli_* tools below. When the user wants the finished file, call "
            "`officecli_file` with action=\"download\" and the file_id, then show the "
            "returned URL as a download link. "
            "RENDER->LOOK->FIX: use officecli_view_html (HTML text) or "
            "officecli_view_screenshot (PNG image) to see the document, edit with "
            "officecli_set/add/remove/edit, then view again to verify. "
            "Selectors are officecli DOM/CSS paths like /slide[1] or /body/p[2]; run "
            "officecli_view_annotated or officecli_view_outline to discover them. "
            "ASSETS: to insert an image or import CSV, first call `officecli_file` "
            "with action=\"stage\" (source_file_id= an OpenWebUI image id, or rely on "
            "__files__) to drop the asset into the document's workdir and get an "
            "asset filename; then call `officecli_add` with type=picture and "
            "prop=[\"src=<asset>\",\"width=...\",\"x=...\",\"y=...\"] (or "
            "`officecli_import` with source=<asset>). "
            "NEVER pass a URL (http://...) or an OpenWebUI /api/v1/files/ path as "
            "src= - officecli's SSRF guard blocks internal addresses and the call "
            "will fail. src= MUST be a staged asset filename."
        ),
        # Pass the real bind host so FastMCP's auto DNS-rebinding guard only
        # fires for true localhost binds. We supply an explicit
        # transport_security below to own the allowed_hosts list regardless.
        host=host,
        transport_security=transport_security,
    )
    # Expose runner on the instance for tests; not part of the public API.
    mcp._runner = runner  # type: ignore[attr-defined]
    mcp._file_store = file_store  # type: ignore[attr-defined]

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_text(file_id: str, page: int | None = None) -> str:
        """Plain text of the document (docx/xlsx/pptx). Read-only."""
        argv = ["view", "{path}", "text"]
        if page is not None:
            argv += ["--page", str(page)]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_html(file_id: str) -> str:
        """Render document to HTML (returned as text). PPTX/DOCX. Read-only."""
        return _run_text(runner, file_id, ["view", "{path}", "html"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_screenshot(file_id: str, page: int | None = None) -> Image:
        """Render one page to a PNG image. Read-only. Use to visually verify edits."""
        argv = ["view", "{path}", "screenshot"]
        if page is not None:
            argv += ["--page", str(page)]
        res = _run(runner, file_id, argv)
        if res.image_path is None:
            raise ToolError("screenshot produced no image file")
        png = runner.read_image(res.image_path)
        return Image(data=png, format="png")

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_annotated(file_id: str, json: bool = False) -> str:
        """Annotated structure with element selectors. Read-only. Use to find paths."""
        argv = ["view", "{path}", "annotated"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_outline(file_id: str) -> str:
        """Document outline (headings / slide titles). Read-only."""
        return _run_text(runner, file_id, ["view", "{path}", "outline"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_stats(file_id: str) -> str:
        """Document stats (counts, sizes). Read-only."""
        return _run_text(runner, file_id, ["view", "{path}", "stats"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_issues(file_id: str, json: bool = False) -> str:
        """Content/layout issues. Read-only. Use before declaring a doc done."""
        argv = ["view", "{path}", "issues"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_get(file_id: str, selector: str, depth: int | None = None, json: bool = False) -> str:
        """Get an element by selector (e.g. /slide[1]). Read-only."""
        argv = ["get", "{path}", selector]
        if depth is not None:
            argv += ["--depth", str(depth)]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_set(file_id: str, selector: str, prop: list[str] | None = None) -> str:
        """Set a property on matched elements. prop is a list of 'key=value'."""
        argv = ["set", "{path}", selector]
        if prop:
            for p in prop:
                argv += ["--prop", p]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_edit(file_id: str, find: str, replace: str) -> str:
        """Find and replace text in the document."""
        return _run_text(
            runner,
            file_id,
            ["set", "{path}", "/find-replace", "--find", find, "--replace", replace],
        )

    @mcp.tool(annotations=_WRITE)
    def officecli_add(file_id: str, selector: str, type: str, prop: list[str] | None = None) -> str:
        """Add an element. selector=/ for top-level (e.g. add a slide with type=slide).

        prop is a list of 'key=value' (e.g. ["src=kimi.png","width=5in"] for a picture).
        For a picture, src= MUST be a staged asset filename - never a URL or an
        OpenWebUI /api/v1/files/ path (officecli's SSRF guard blocks internal
        addresses). Call officecli_file(action="stage") first to drop the asset
        into the document's workdir and get the filename.
        """
        _reject_url_src(prop)
        argv = ["add", "{path}", selector, "--type", type]
        if prop:
            for p in prop:
                argv += ["--prop", p]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_import(
        file_id: str,
        sheet: str,
        source: str,
        header: bool = False,
        start_cell: str = "A1",
        format: str | None = None,
    ) -> str:
        """Import CSV/TSV into an Excel sheet. source is a staged asset filename
        (drop it first via officecli_file action='stage'). sheet e.g. /Sheet1."""
        argv = ["import", "{path}", sheet, source]
        if header:
            argv.append("--header")
        argv += ["--start-cell", start_cell]
        if format:
            argv += ["--format", format]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_remove(file_id: str, selector: str) -> str:
        """Remove matched elements."""
        return _run_text(runner, file_id, ["remove", "{path}", selector])

    @mcp.tool(annotations=_WRITE)
    def officecli_move(file_id: str, selector: str, position: int) -> str:
        """Move an element to a new position."""
        return _run_text(runner, file_id, ["move", "{path}", selector, "--to", str(position)])

    @mcp.tool(annotations=_WRITE)
    def officecli_swap(file_id: str, selector_a: str, selector_b: str) -> str:
        """Swap two elements."""
        return _run_text(runner, file_id, ["swap", "{path}", selector_a, selector_b])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_validate(file_id: str, json: bool = False) -> str:
        """Validate against the OpenXML schema. Read-only."""
        argv = ["validate", "{path}"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_batch(file_id: str, commands_json: str) -> str:
        """Run many commands (JSON array) in one open/save cycle."""
        return _run_text(runner, file_id, ["batch", "{path}", "--commands", commands_json])

    @mcp.tool(annotations=_WRITE)
    def officecli_create(file_id: str, name: str, type: str) -> str:
        """Create a blank document. name e.g. 'deck.pptx'; type in docx|xlsx|pptx.

        Returns a NEW file_id for the created document (the input file_id is
        only used to host the new file's workdir).
        """
        new_id = uuid.uuid4().hex
        new_dir = os.path.join(file_store.work_dir, new_id)
        os.makedirs(new_dir, exist_ok=True)
        argv = ["create", os.path.join(new_dir, name), "--type", type]
        res = runner._raw_run(argv, cwd=new_dir)
        if res.exit_code != 0:
            return _err(f"create failed: {res.stderr.strip()}")
        return new_id

    return mcp


def _run(runner: OfficeRunner, file_id: str, argv: list[str]):
    try:
        return runner.run(file_id, argv)
    except FileIDNotFound as e:
        raise ToolError(f"file_id '{file_id}' not found or expired") from e


def _reject_url_src(prop: list[str] | None) -> None:
    """Block src= values that are URLs or OpenWebUI API paths.

    officecli's add picture accepts a URL for src=, but its SSRF guard refuses
    internal/docker addresses - so a model passing an OpenWebUI file URL hits a
    confusing 'Refusing to fetch image from non-public address' error. The
    sanctioned path is to stage the asset first (officecli_file action="stage")
    and pass the returned filename. Fail fast with that guidance instead.
    """
    if not prop:
        return
    for p in prop:
        if "=" not in p:
            continue
        key, val = p.split("=", 1)
        if key.strip() != "src":
            continue
        v = val.strip()
        if v.startswith(("http://", "https://")) or v.startswith("/api/v1/files/"):
            raise ToolError(
                "src= must be a staged asset filename, not a URL or OpenWebUI "
                "path (officecli's SSRF guard blocks internal addresses). Call "
                "officecli_file(action=\"stage\", file_id=<this doc>, "
                "source_file_id=<the OpenWebUI image id>) first to drop the "
                "image into the document's workdir, then pass the returned "
                "asset filename as src=."
            )



def _run_text(runner: OfficeRunner, file_id: str, argv: list[str]) -> str:
    try:
        res = runner.run(file_id, argv)
    except FileIDNotFound as e:
        raise ToolError(f"file_id '{file_id}' not found or expired") from e
    if res.exit_code != 0:
        raise ToolError(f"officecli exited {res.exit_code}: {res.stderr.strip()}")
    return res.stdout.strip()
