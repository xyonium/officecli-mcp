"""officecli_* MCP tool definitions (handle-based)."""
from __future__ import annotations

import logging
import os
import uuid

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from officecli_mcp.files import FileStore
from officecli_mcp.runner import FileIDNotFound, OfficeRunner

log = logging.getLogger(__name__)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def build_mcp(runner: OfficeRunner, file_store: FileStore) -> FastMCP:
    mcp = FastMCP("officecli-mcp")
    # Expose runner on the instance for tests; not part of the public API.
    mcp._runner = runner  # type: ignore[attr-defined]
    mcp._file_store = file_store  # type: ignore[attr-defined]

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_text(file_id: str, page: int | None = None) -> str:
        """View the plain text of an office document (docx/xlsx/pptx)."""
        argv = ["view", "{path}", "text"]
        if page is not None:
            argv += ["--page", str(page)]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_html(file_id: str) -> str:
        """Render an office document to HTML and return it (PPTX/DOCX)."""
        return _run_text(runner, file_id, ["view", "{path}", "html"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_screenshot(file_id: str, page: int | None = None) -> Image:
        """Render a page of an office document to a PNG screenshot."""
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
        """View annotated structure of the document."""
        argv = ["view", "{path}", "annotated"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_outline(file_id: str) -> str:
        """View the document outline (headings/slide titles)."""
        return _run_text(runner, file_id, ["view", "{path}", "outline"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_stats(file_id: str) -> str:
        """View document stats (counts, sizes)."""
        return _run_text(runner, file_id, ["view", "{path}", "stats"])

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_issues(file_id: str, json: bool = False) -> str:
        """View content/layout issues in the document."""
        argv = ["view", "{path}", "issues"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_get(file_id: str, selector: str, depth: int | None = None, json: bool = False) -> str:
        """Get an element by DOM/CSS selector."""
        argv = ["get", "{path}", selector]
        if depth is not None:
            argv += ["--depth", str(depth)]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_set(file_id: str, selector: str, prop: str) -> str:
        """Set a property on elements matched by selector. prop is 'key=value'."""
        return _run_text(runner, file_id, ["set", "{path}", selector, "--prop", prop])

    @mcp.tool(annotations=_WRITE)
    def officecli_edit(file_id: str, find: str, replace: str) -> str:
        """Find and replace text in the document."""
        return _run_text(
            runner,
            file_id,
            ["set", "{path}", "/find-replace", "--find", find, "--replace", replace],
        )

    @mcp.tool(annotations=_WRITE)
    def officecli_add(file_id: str, selector: str, type: str, prop: str | None = None) -> str:
        """Add an element under the selector."""
        argv = ["add", "{path}", selector, "--type", type]
        if prop:
            argv += ["--prop", prop]
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_remove(file_id: str, selector: str) -> str:
        """Remove elements matched by selector."""
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
        """Validate the document against the OpenXML schema."""
        argv = ["validate", "{path}"]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_batch(file_id: str, commands_json: str) -> str:
        """Run a batch of commands (JSON) in one open/save cycle."""
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


def _run_text(runner: OfficeRunner, file_id: str, argv: list[str]) -> str:
    try:
        res = runner.run(file_id, argv)
    except FileIDNotFound as e:
        raise ToolError(f"file_id '{file_id}' not found or expired") from e
    if res.exit_code != 0:
        raise ToolError(f"officecli exited {res.exit_code}: {res.stderr.strip()}")
    return res.stdout.strip()
