"""Render the officecli_file shim from the live manifest; keep the example in sync."""
from __future__ import annotations

import logging
from pathlib import Path

from officecli_mcp.shim_template import TEMPLATE

log = logging.getLogger(__name__)

SHIM_HEADER = "# officecli-shim-rev: "
EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "examples" / "openwebui_officecli_file.py"


def _manifest_section(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        lines.append(f"* {t['signature']}")
        desc = (t.get("description") or "").strip()
        if desc:
            lines.extend(f"    {line}" for line in desc.splitlines())
    return "\n".join(lines)


def render_shim(manifest: dict) -> str:
    """Fill the template with the manifest. str.replace, never .format."""
    return (
        TEMPLATE.replace("{REV}", manifest["revision"])
        .replace("{INSTRUCTIONS}", manifest.get("instructions", ""))
        .replace("{MANIFEST}", _manifest_section(manifest.get("tools", [])))
    )


def sync_example(manifest: dict) -> None:
    """Regenerate examples/openwebui_officecli_file.py from the template."""
    EXAMPLE_PATH.write_text(render_shim(manifest))
    log.info("regenerated %s", EXAMPLE_PATH)


async def _amain() -> None:
    import tempfile

    from officecli_mcp.files import FileStore
    from officecli_mcp.manifest import get_manifest
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.tools import build_mcp

    store = FileStore(work_dir=tempfile.mkdtemp(prefix="shim-gen-"), ttl_seconds=3600)
    runner = OfficeRunner(binary_path="/bin/true", file_store=store)
    mcp = build_mcp(runner=runner, file_store=store)
    sync_example(await get_manifest(mcp))


if __name__ == "__main__":
    import anyio

    anyio.run(_amain)
