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

    # DNS-rebinding / Host-header guard for the streamable-HTTP endpoint.
    # OpenWebUI reaches us by service name (Host: officecli-mcp:8765); the SDK's
    # default guard only allow-lists localhost, so cross-container traffic gets
    # 421 "Invalid Host header" unless the name is declared here. Pass host= so
    # the SDK does not re-enable a localhost-only guard on top of our explicit one.
    # We always pass an explicit TransportSecuritySettings so the operator's
    # enable/disable choice is honored even on a 127.0.0.1 bind (passing None
    # would let FastMCP auto-enable protection and ignore our toggle).
    from mcp.server.transport_security import TransportSecuritySettings

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.dns_rebinding_protection,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=[f"http://{h}" for h in settings.allowed_hosts],
    )
    mcp = build_mcp(
        runner=runner,
        file_store=file_store,
        host=settings.host,
        transport_security=transport_security,
    )

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
