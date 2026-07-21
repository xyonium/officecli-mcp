"""Tests for shim rendering and template/example parity."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fake_manifest() -> dict:
    return {
        "revision": "ab" * 20,
        "instructions": "WORKFLOW: upload first.",
        "tools": [
            {
                "name": "officecli_view_text",
                "description": "Plain text of the document. Read-only.",
                "inputSchema": {},
                "readOnly": True,
                "signature": "officecli_view_text(file_id: str, page?: int)",
            },
            {
                "name": "officecli_set",
                "description": 'Set properties. Quote "test" intact.',
                "inputSchema": {},
                "readOnly": False,
                "signature": "officecli_set(file_id: str, selector: str, prop?: list[str])",
            },
        ],
    }


def test_render_embeds_revision_manifest_and_instructions():
    from officecli_mcp.shim import render_shim

    src = render_shim(_fake_manifest())
    assert src.splitlines()[0] == f"# officecli-shim-rev: {'ab' * 20}"
    assert "WORKFLOW: upload first." in src
    # Full descriptions embedded verbatim (quotes escaped inside the docstring).
    assert "officecli_view_text(file_id: str, page?: int)" in src
    assert "Plain text of the document. Read-only." in src
    assert "officecli_set(file_id: str, selector: str, prop?: list[str])" in src
    # The generic actions exist.
    assert 'if action == "run":' in src
    assert 'if action == "tools":' in src
    assert "_mcp_call" in src


def test_render_output_is_valid_python():
    from officecli_mcp.shim import render_shim

    src = render_shim(_fake_manifest())
    compile(src, "shim.py", "exec")  # raises SyntaxError if invalid


def test_example_matches_rendered_template():
    """examples/openwebui_officecli_file.py must equal the template rendered
    with the CURRENT server manifest. Regenerate with:
        python -m officecli_mcp.shim
    """
    import anyio

    from officecli_mcp.shim import render_shim
    from tests.test_shim_helpers import live_manifest  # local helper below

    expected = render_shim(anyio.run(live_manifest))
    actual = (ROOT / "examples" / "openwebui_officecli_file.py").read_text()
    assert actual == expected, (
        "example shim is stale; run: python -m officecli_mcp.shim"
    )


def test_example_starts_with_revision_header():
    from officecli_mcp.shim import SHIM_HEADER

    first = (ROOT / "examples" / "openwebui_officecli_file.py").read_text().splitlines()[0]
    assert first.startswith(SHIM_HEADER)
    assert re.fullmatch(r"# officecli-shim-rev: [0-9a-f]{40}", first)
