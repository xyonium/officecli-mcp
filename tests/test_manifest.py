"""Tests for the server-side tool manifest builder."""
from __future__ import annotations

import pytest


@pytest.fixture
def mcp_server(settings, tmp_path):
    from officecli_mcp import tools as tools_mod
    from officecli_mcp.files import FileStore
    from officecli_mcp.runner import OfficeRunner

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    stub.write_text("#!/bin/sh\necho OK\n")
    stub.chmod(0o755)
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    return tools_mod.build_mcp(runner=runner, file_store=store)


async def test_manifest_contains_all_tools_with_full_descriptions(mcp_server):
    from officecli_mcp.manifest import get_manifest

    manifest = await get_manifest(mcp_server)
    names = {t["name"] for t in manifest["tools"]}
    assert "officecli_view_html" in names
    assert "officecli_batch" in names
    assert all(n.startswith("officecli_") for n in names)
    by_name = {t["name"]: t for t in manifest["tools"]}
    # Full description, not a summary: the batch tool's multi-paragraph
    # schema guidance must survive verbatim.
    assert "props" in by_name["officecli_batch"]["description"]
    assert "verbatim" in by_name["officecli_batch"]["description"]
    # Signature is a compact one-liner built from the input schema.
    assert by_name["officecli_set"]["signature"] == (
        "officecli_set(file_id: str, selector: str, prop?: list[str])"
    )
    # Read-only flags come from the ToolAnnotations.
    assert by_name["officecli_view_text"]["readOnly"] is True
    assert by_name["officecli_set"]["readOnly"] is False
    # Instructions are the FastMCP server instructions (workflow guidance).
    assert "file_id" in manifest["instructions"]


async def test_revision_changes_with_description(mcp_server):
    from officecli_mcp.manifest import get_manifest, manifest_revision

    manifest = await get_manifest(mcp_server)
    rev = manifest["revision"]
    assert len(rev) == 40  # sha1 hex
    mutated = [dict(t) for t in manifest["tools"]]
    mutated[0]["description"] += " changed"
    assert manifest_revision(mutated) != rev


def test_compact_sig_defaults_and_maps():
    from officecli_mcp.manifest import _compact_sig

    assert _compact_sig(
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "commands_json": {"type": "string"},
                "page": {"type": "integer", "default": None},
                "json": {"type": "boolean", "default": False},
            },
            "required": ["file_id", "commands_json"],
        }
    ) == "(file_id: str, commands_json: str, page?: int, json?: bool)"
    assert _compact_sig({"type": "object", "properties": {"x": {"type": "null"}}}) == "(x: Any)"
