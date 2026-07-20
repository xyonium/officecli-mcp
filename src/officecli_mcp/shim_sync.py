"""Push the rendered officecli_file shim into OpenWebUI's Tools API on boot.

Best-effort: any failure logs a warning and the server keeps running (the
shim in OpenWebUI is simply stale; action="run"/"tools" still hit the live
server). Requires an OpenWebUI ADMIN API key - create/update of tool content
is an admin-or-workspace.tools-permission operation (routers/tools.py).
"""
from __future__ import annotations

import logging

import httpx

from officecli_mcp.manifest import get_manifest
from officecli_mcp.shim import SHIM_HEADER, render_shim

log = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _client(url: str, key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=_TIMEOUT,
    )


def _stored_revision(content: str) -> str:
    first = (content or "").splitlines()[0] if content else ""
    return first.removeprefix(SHIM_HEADER).strip() if first.startswith(SHIM_HEADER) else ""


async def sync_shim(settings, mcp) -> None:
    """Create or update the officecli_file tool in OpenWebUI to match the
    current manifest. No-op when disabled, unconfigured, or up to date."""
    if not getattr(settings, "owui_sync", True):
        log.info("shim self-sync disabled (OFFICECLI_MCP_OWUI_SYNC=0)")
        return
    url = getattr(settings, "owui_url", "")
    key = getattr(settings, "owui_api_key", "")
    if not url or not key:
        log.info("shim self-sync skipped: OFFICECLI_MCP_OWUI_URL / _API_KEY not set")
        return
    tool_id = getattr(settings, "owui_tool_id", "officecli_file")
    try:
        manifest = await get_manifest(mcp)
        content = render_shim(manifest)
        async with _client(url, key) as client:
            resp = await client.get(f"/api/v1/tools/id/{tool_id}")
            if resp.status_code == 404:
                resp = await client.post(
                    "/api/v1/tools/create",
                    json={
                        "id": tool_id,
                        "name": "officecli_file",
                        "content": content,
                        "meta": {
                            "description": (
                                "All officecli document operations through "
                                "officecli-mcp (upload/download/stage/run/tools)."
                            )
                        },
                        # ToolForm.access_grants looks Optional but the API
                        # 422s on null ("Input should be a valid list") -
                        # verified live. [] = private (owner/admin only).
                        "access_grants": [],
                    },
                )
                resp.raise_for_status()  # never log success on a failed create
                log.info("shim self-sync: created OpenWebUI tool '%s'", tool_id)
                return
            resp.raise_for_status()
            existing = resp.json()
            if _stored_revision(existing.get("content", "")) == manifest["revision"]:
                log.info("shim self-sync: OpenWebUI tool '%s' up to date", tool_id)
                return
            resp = await client.post(
                f"/api/v1/tools/id/{tool_id}/update",
                json={
                    "id": tool_id,
                    "name": existing.get("name", tool_id),
                    "content": content,
                    "meta": {
                        **(existing.get("meta") or {}),
                        "description": (
                            "All officecli document operations through "
                            "officecli-mcp (upload/download/stage/run/tools)."
                        ),
                    },
                    # Preserve the tool's visibility (public stays public).
                    # GET may echo null for a private tool; the API 422s on
                    # null, so fall back to an empty list.
                    "access_grants": existing.get("access_grants") or [],
                },
            )
            resp.raise_for_status()
            log.info("shim self-sync: updated OpenWebUI tool '%s'", tool_id)
    except Exception:  # noqa: BLE001
        log.warning("shim self-sync failed (continuing without it)", exc_info=True)
