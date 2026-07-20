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
from officecli_mcp.files import FileStore, delete, download, stage, upload
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
        view_html_mode=getattr(settings, "view_html_mode", 2),
        view_html_max_chars=getattr(settings, "view_html_max_chars", 8000),
        screenshot_max_edge=getattr(settings, "screenshot_max_edge", 1024),
    )

    # Custom HTTP routes share the same process/workdir as the MCP tools.
    @mcp.custom_route("/files", methods=["POST"])
    async def _upload(request):
        request.app.state.settings = settings
        request.app.state.file_store = file_store
        return await upload(request)

    @mcp.custom_route("/files/stage", methods=["POST"])
    async def _stage(request):
        request.app.state.settings = settings
        request.app.state.file_store = file_store
        return await stage(request)

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

    # Generic manifest + dispatch so the OpenWebUI native tool can drive every
    # officecli_* tool over plain HTTP. These go through the SAME FastMCP
    # request handlers as /mcp, so behavior (validation, errors, content
    # blocks) is identical to the MCP path.
    import mcp.types as mcp_types

    from officecli_mcp.files import _check_api_key
    from officecli_mcp.manifest import get_manifest

    @mcp.custom_route("/tools", methods=["GET"])
    async def _tools_manifest(request):
        err = _check_api_key(request, settings.api_key)
        if err:
            return err
        from starlette.responses import JSONResponse

        return JSONResponse(await get_manifest(mcp))

    @mcp.custom_route("/tools/call", methods=["POST"])
    async def _tools_call(request):
        err = _check_api_key(request, settings.api_key)
        if err:
            return err
        import base64 as _b64

        from starlette.responses import JSONResponse

        try:
            payload = await request.json()
            name = payload["name"]
            arguments = payload.get("arguments") or {}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

        list_handler = mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        listed = await list_handler(mcp_types.ListToolsRequest())
        if isinstance(listed, mcp_types.ServerResult):
            listed = listed.root
        if name not in {t.name for t in listed.tools}:
            return JSONResponse({"error": f"unknown tool '{name}'"}, status_code=404)

        call_handler = mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        try:
            result = await call_handler(req)
        except Exception as e:  # noqa: BLE001
            # Validation/execution errors surface as content, not HTTP errors,
            # so the model can read and self-correct (same as the MCP path).
            return JSONResponse(
                {"content": [{"type": "text", "text": str(e)}], "isError": True}
            )
        if isinstance(result, mcp_types.ServerResult):
            result = result.root
        content = []
        for block in getattr(result, "content", []) or []:
            if getattr(block, "type", None) == "image":
                content.append(
                    {
                        "type": "image",
                        "data": block.data if isinstance(block.data, str) else _b64.b64encode(block.data).decode(),
                        "mimeType": getattr(block, "mimeType", "image/png"),
                    }
                )
            elif hasattr(block, "text"):
                content.append({"type": "text", "text": block.text})
        return JSONResponse({"content": content, "isError": bool(getattr(result, "isError", False))})

    app = mcp.streamable_http_app()
    app.state.settings = settings
    app.state.file_store = file_store
    app.state.mcp = mcp
    return app
