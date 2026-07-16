"""Assemble the app: officecli_* MCP tools + /files + /health on one server.

We attach /files and /health as custom Starlette routes on the FastMCP server
(via mcp.custom_route), then serve mcp.streamable_http_app() as the root app.
That app's own lifespan runs the session manager - mounting it inside another
Starlette app would skip that lifespan, so it must be the root.
"""
from __future__ import annotations

import logging

from officecli_mcp import binary
from officecli_mcp.config import Settings
from officecli_mcp.files import FileStore, delete, download, upload
from officecli_mcp.runner import OfficeRunner
from officecli_mcp.tools import build_mcp

log = logging.getLogger(__name__)


def build_app(settings: Settings):
    bin_path = binary.ensure_binary(
        settings.data_dir, settings.officecli_version, settings.officecli_sha256
    )
    log.info("officecli binary at %s", bin_path)

    file_store = FileStore(work_dir=settings.work_dir, ttl_seconds=settings.work_ttl_seconds)
    runner = OfficeRunner(binary_path=bin_path, file_store=file_store)
    mcp = build_mcp(runner=runner, file_store=file_store)

    # Custom HTTP routes share the same process/workdir as the MCP tools.
    @mcp.custom_route("/files", methods=["POST"])
    async def _upload(request):
        request.app.state.settings = settings
        request.app.state.file_store = file_store
        return await upload(request)

    @mcp.custom_route("/files/{file_id}", methods=["GET"])
    async def _download(request):
        request.app.state.settings = settings
        request.app.state.file_store = file_store
        return await download(request)

    @mcp.custom_route("/files/{file_id}", methods=["DELETE"])
    async def _delete(request):
        request.app.state.settings = settings
        request.app.state.file_store = file_store
        return await delete(request)

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(request):
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok"})

    app = mcp.streamable_http_app()
    app.state.settings = settings
    app.state.file_store = file_store
    app.state.mcp = mcp
    return app
