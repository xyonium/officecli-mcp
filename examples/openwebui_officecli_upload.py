"""OpenWebUI native Tool: upload chat-attached office docs to officecli-mcp.

Install: Workspace > Tools > paste this file. Set Valves:
  - officecli_mcp_url: e.g. http://officecli-mcp:8765
  - openwebui_url:     e.g. http://open-webui:8080  (the in-cluster OWUI base URL)
  - openwebui_api_key: an API key (Account Settings > API Keys) with file access.

Attach this tool to a model alongside the officecli-mcp MCP connection. The model
calls officecli_upload(__files__), gets back a file_id, and passes it to the
officecli_* MCP tools.
"""
from __future__ import annotations

import json
from typing import Any

import requests


class Tools:
    class Valves:
        def __init__(self, **kwargs):
            self.officecli_mcp_url = "http://officecli-mcp:8765"
            self.openwebui_url = "http://open-webui:8080"
            self.openwebui_api_key = ""
            for k, v in kwargs.items():
                setattr(self, k, v)

    def __init__(self):
        self.valves = self.Valves()

    # --- swappable HTTP helpers (monkeypatched in tests) ---
    def _owui_get(self, file_id: str) -> bytes:
        url = f"{self.valves.openwebui_url}/api/v1/files/{file_id}/content"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.valves.openwebui_api_key}"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content

    def _mcp_post(self, filename: str, data: bytes) -> dict:
        url = f"{self.valves.officecli_mcp_url}/files"
        files = {"file": (filename, data, "application/octet-stream")}
        resp = requests.post(url, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def officecli_upload(self, __files__: list[dict[str, Any]] = []) -> str:
        """Upload attached office files to the officecli-mcp server.

        Args:
            __files__: OpenWebUI-injected list of attached file dicts (have 'id' and 'name').

        Returns:
            JSON string: {"files": [...], "hint": "..."}
            Pass each file_id to the officecli_* MCP tools.
        """
        if not __files__:
            return json.dumps({"error": "no files attached"})

        out = []
        for f in __files__:
            file_id = f.get("id")
            name = f.get("name") or f.get("filename") or "upload.docx"
            if not file_id:
                # __files__ entries may nest under 'file'
                file_id = (f.get("file") or {}).get("id")
            if not file_id:
                continue
            try:
                data = self._owui_get(file_id)
                info = self._mcp_post(name, data)
                out.append({"file_id": info["file_id"], "filename": info.get("filename", name)})
            except Exception as e:  # noqa: BLE001
                out.append({"filename": name, "error": str(e)})

        return json.dumps(
            {
                "files": out,
                "hint": "Pass each file_id to officecli_* MCP tools (e.g. officecli_view_html).",
            }
        )
