"""officecli_* MCP tool definitions (handle-based)."""
from __future__ import annotations

import logging
import os
import re
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


def _downscale_png(png: bytes, max_edge: int) -> bytes:
    """Clamp a PNG's longest edge to max_edge px (aspect preserved).

    Screenshots go straight into the model context as base64, so unbounded
    resolution = unbounded tokens. max_edge=0 disables resizing. A corrupt
    image is returned unchanged (logged) rather than failing the tool call.
    """
    if max_edge <= 0:
        return png
    import io

    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(png)) as im:
            w, h = im.size
            if max(w, h) <= max_edge:
                return png
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        log.warning("screenshot downscale failed; returning original image", exc_info=True)
        return png


def build_mcp(
    runner: OfficeRunner,
    file_store: FileStore,
    *,
    host: str = "0.0.0.0",
    transport_security: TransportSecuritySettings | None = None,
    view_html_mode: int = 2,
    view_html_max_chars: int = 8000,
    screenshot_max_edge: int = 1024,
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
            "INSPECT BEFORE RESIZE: to size an element to fill the page, first "
            "officecli_get(selector, json=true) to read its current x/y/width/height, "
            "compare to the page dimensions from officecli_create, then officecli_set "
            "the new size in one call. "
            "BATCH PROPS: officecli_set and officecli_add take prop as a LIST of "
            "'key=value' - pass every property for one element in a SINGLE call "
            "(e.g. prop=[\"x=2cm\",\"y=4cm\",\"width=21cm\",\"height=5cm\"]), never "
            "one call per property. officecli_batch is DIFFERENT: each item uses "
            "\"command\", \"parent\" (for add) or \"path\" (for set/remove), and "
            "\"props\" as a key->value MAP {\"x\":\"1cm\"} - see its docstring for "
            "the exact schema and copy the example verbatim. "
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
            "will fail. src= MUST be a staged asset filename. "
            "SIZING: officecli_create returns the page dimensions in its output "
            "(pptx 16:9 = slideWidth 960pt / slideHeight 540pt = 33.87cm x 19.05cm). "
            "Size objects from THESE dimensions, not guesses: a full-bleed "
            "background picture or textbox is x=0 y=0 width=33.87cm height=19.05cm. "
            "PICTURES STRETCH: officecli add picture sets the box to exactly the "
            "width/height you give - it does NOT crop or preserve aspect ratio. A "
            "1:1 image forced into 33.87cm x 19.05cm will be squashed. So generate "
            "(or pick) images in the TARGET aspect ratio (16:9 for a full-bleed "
            "slide background) before staging, or set width/height to match the "
            "image's own ratio."
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
        """Render document to HTML. PPTX/DOCX. Read-only.

        Output is governed by OFFICECLI_MCP_VIEW_HTML_MODE: 0=disabled, 1=full
        HTML, 2=compact (default; base64 images -> [IMG], styles/scripts
        stripped - small enough for the context), 3=truncated. officecli's raw
        HTML is a full interactive page that can blow the context on complex
        docs, so compact is the default. For a faithful visual check use
        officecli_view_screenshot instead.
        """
        if view_html_mode == 0:
            return _err(
                "view_html is disabled (OFFICECLI_MCP_VIEW_HTML_MODE=0). Use "
                "officecli_view_screenshot for a visual check or "
                "officecli_view_annotated/outline for structure."
            )
        html = _run_text(runner, file_id, ["view", "{path}", "html"])
        if view_html_mode == 1:
            return html
        if view_html_mode == 3:
            return _truncate_html(html, view_html_max_chars)
        return _compact_html(html)  # mode 2 (default)

    @mcp.tool(annotations=_READ_ONLY)
    def officecli_view_screenshot(file_id: str, page: int | None = None) -> Image:
        """Render one page to a PNG image. Read-only. Use to visually verify edits."""
        argv = ["view", "{path}", "screenshot"]
        if page is not None:
            argv += ["--page", str(page)]
        res = _run(runner, file_id, argv)
        if res.image_path is None:
            raise ToolError("screenshot produced no image file")
        png = _downscale_png(runner.read_image(res.image_path), screenshot_max_edge)
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
        """Get an element by selector (e.g. /slide[1]). Read-only.

        Returns the element's current position and size (x/y/width/height) and
        its format/properties. Use --json for structured output including
        'effective' computed properties (font, etc.) and child elements. Use
        this to INSPECT an element's current size before resizing it with
        officecli_set (e.g. check a textbox's width/height, compare to the
        page size from officecli_create, then set the new size in one call).
        depth=N expands N levels of children to discover selectors.
        """
        argv = ["get", "{path}", selector]
        if depth is not None:
            argv += ["--depth", str(depth)]
        if json:
            argv.append("--json")
        return _run_text(runner, file_id, argv)

    @mcp.tool(annotations=_WRITE)
    def officecli_set(file_id: str, selector: str, prop: list[str] | None = None) -> str:
        """Set properties on matched elements. prop is a list of 'key=value'.

        Pass MULTIPLE properties in ONE call - do not call set once per
        property. Example: to position and size a textbox, call
        officecli_set(prop=["x=2cm","y=4cm","width=21cm","height=5cm"], ...)
        not four separate calls. Each item becomes its own --prop.
        """
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
        """Add an element under selector (the PARENT). selector=/ adds a slide.

        type: slide | shape | text | textbox | picture | paragraph | run | ...
        prop: a LIST of 'key=value' strings, e.g. ["x=1cm","y=1cm","width=5cm"].
        Pass all props in one call. (NOTE: officecli_batch uses "props" as a
        key->value MAP instead - different shape, do not mix them up.)

        Picture: src= MUST be a staged asset filename (call officecli_file
        action="stage" first) - never a URL or /api/v1/files/ path (SSRF guard
        blocks it). A picture stretches to exactly the width/height you give -
        no crop/fit - so generate images in the target aspect ratio.
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
        """Run many commands in one open/save cycle. commands_json is a JSON
        ARRAY of objects. Default is ATOMIC: if any item fails, nothing is
        applied (use only when you want all-or-nothing).

        Each item's "command" is the bare verb (add/set/remove/move/swap/get).
        Fields are SIBLINGS of "command" - DIFFERENT from officecli_add/set:
          - "parent": add target (e.g. "/slide[1]"). NOT "selector".
          - "path": set/remove/get target (e.g. "/slide[1]/shape[1]").
          - "type": element type for add (slide/shape/text/picture/...).
          - "props": a key->value MAP ({"x":"1cm","bold":"true"}), NOT a list
            of "k=v" strings. (add/set tools use a list; batch uses a map.)
          - "to"/"after"/"before": move. "path2": swap's second path.

        Example (copy verbatim, edit values):
        [{"command":"add","parent":"/","type":"slide"},
         {"command":"add","parent":"/slide[1]","type":"shape","props":{"x":"1cm","y":"1cm","width":"5cm","height":"3cm","fill":"#5B7CFA"}},
         {"command":"set","path":"/slide[1]/shape[1]","props":{"line":"none"}}]
        """
        return _run_text(runner, file_id, ["batch", "{path}", "--commands", commands_json])

    @mcp.tool(annotations=_WRITE)
    def officecli_create(file_id: str, name: str, type: str) -> str:
        """Create a blank document. name e.g. 'deck.pptx'; type in docx|xlsx|pptx.

        Returns the new file_id on its FIRST line, followed by the officecli
        create output. For pptx that output includes slideWidth/slideHeight
        (e.g. 960pt x 540pt = 33.87cm x 19.05cm, 16:9) - USE these to size
        objects: a full-bleed background is x=0 y=0 width=33.87cm height=19.05cm.
        The file_id to pass to other tools is the FIRST line of the return.
        """
        new_id = uuid.uuid4().hex
        new_dir = os.path.join(file_store.work_dir, new_id)
        os.makedirs(new_dir, exist_ok=True)
        argv = ["create", os.path.join(new_dir, name), "--type", type]
        res = runner._raw_run(argv, cwd=new_dir)
        if res.exit_code != 0:
            return _err(f"create failed: {res.stderr.strip()}")
        # Surface the create stdout so the model learns the page dimensions
        # (slideWidth/slideHeight for pptx). Without it the model only sees a
        # bare file_id and guesses object sizes, leaving them in the top-left.
        out = res.stdout.strip()
        if out:
            return f"{new_id}\n{out}"
        return new_id

    return mcp


def _run(runner: OfficeRunner, file_id: str, argv: list[str]):
    try:
        return runner.run(file_id, argv)
    except FileIDNotFound as e:
        raise ToolError(f"file_id '{file_id}' not found or expired") from e


_BASE64_IMG_RE = re.compile(r'<img[^>]*src="data:image/[^"]+"[^>]*>', re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def _compact_html(html: str) -> str:
    """Reduce officecli's full interactive HTML to a small text skeleton.

    officecli view html emits a full page (CSS, JS, base64 images, sidebar)
    that can blow the model context. For compact mode we drop style/script
    blocks, replace each base64 image with an [IMG] placeholder, strip
    remaining tags, and collapse whitespace - leaving the visible text and
    structure the model needs to locate edits, at a fraction of the size.
    """
    s = _SCRIPT_RE.sub("", html)
    s = _STYLE_RE.sub("", s)
    s = _BASE64_IMG_RE.sub("[IMG]", s)
    s = _TAG_RE.sub("", s)  # drop remaining tags, keep their text content
    # Decode the common entities officecli emits so text is readable.
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    s = _WS_RE.sub(" ", s)
    s = _BLANK_LINE_RE.sub("\n", s)
    return s.strip()


def _truncate_html(html: str, max_chars: int) -> str:
    """Return the raw HTML truncated to max_chars with a truncation note."""
    if len(html) <= max_chars:
        return html
    return f"{html[:max_chars]}\n...[truncated: {len(html)} chars total; set OFFICECLI_MCP_VIEW_HTML_MODE=2 for compact or =1 for full]"


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
        # officecli splits failure info across streams: the cause usually goes
        # to stderr ('UNSUPPORTED props: ...', 'Error: Slide 99 not found'),
        # but partial-failure context goes to stdout ('No properties applied
        # to /slide[1]'). Include both so the model sees the whole picture.
        parts = [f"officecli exited {res.exit_code}"]
        if res.stderr.strip():
            parts.append(res.stderr.strip())
        if res.stdout.strip():
            parts.append(f"stdout: {res.stdout.strip()}")
        raise ToolError("\n".join(parts))
    # Surface non-fatal warnings officecli writes to stderr on success.
    out = res.stdout.strip()
    if res.stderr.strip():
        out = f"{out}\n[stderr] {res.stderr.strip()}" if out else res.stderr.strip()
    return out
