"""Build the manifest from a stub-backed FastMCP instance (no real binary)."""
from __future__ import annotations


async def live_manifest(tmp_work: str | None = None) -> dict:
    import tempfile

    from officecli_mcp.files import FileStore
    from officecli_mcp.manifest import get_manifest
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.tools import build_mcp

    work = tmp_work or tempfile.mkdtemp(prefix="shim-test-")
    store = FileStore(work_dir=work, ttl_seconds=3600)
    runner = OfficeRunner(binary_path="/bin/true", file_store=store)
    mcp = build_mcp(runner=runner, file_store=store)
    return await get_manifest(mcp)
