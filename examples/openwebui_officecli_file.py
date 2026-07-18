"""OpenWebUI native Tool: move office docs in and out of officecli-mcp by handle.

One tool, one method, two actions:
  - action="upload":  push chat-attached office docs INTO officecli-mcp -> file_id.
  - action="download": pull a finished doc OUT of officecli-mcp and into OpenWebUI's
    own file storage, returning a browser-reachable download URL.

Auth model: uses the CURRENT user's credentials - reads the Authorization header
(or session cookie) from OpenWebUI's injected __request__ and forwards it both when
fetching uploaded-file bytes and when POSTing the finished file back into OpenWebUI
storage. No stored API key is needed, so this works as a shared Public tool in
multi-user setups (each user only touches their own files).

Why download goes through OpenWebUI storage (not a direct officecli-mcp URL):
officecli-mcp runs on the internal docker network and is not reachable from the
user's browser. OpenWebUI IS browser-reachable, so we POST the bytes into OpenWebUI
(via POST /api/v1/files/?process=false, which skips RAG ingestion) and hand back an
OpenWebUI URL the browser can load.

Install: Workspace > Tools > paste this file. Set Valves:
  - officecli_mcp_url:       internal officecli-mcp base, e.g. http://officecli-mcp:8765
  - openwebui_url:           internal OpenWebUI base used for API calls, e.g. http://open-webui:8080
  - openwebui_browser_url:   browser-reachable OpenWebUI base for returned download URLs,
                             e.g. https://ai.savorcare.com (default "" -> falls back to openwebui_url)

Attach this tool to a model alongside the officecli-mcp MCP connection. For uploads
the model calls officecli_file(action="upload", __files__=...) to get a file_id, then
passes file_id to the officecli_* MCP tools. For downloads the model calls
officecli_file(action="download", file_id=...) and shows the returned URL as a link.
"""
from __future__ import annotations

import json
from typing import Any

import requests
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        # Pydantic BaseModel (NOT a plain class) so OpenWebUI can call
        # Valves.schema() to render the Valves editor and Valves(**form_data)
        # to apply saved values. A plain class with __init__ has no .schema()
        # and crashes GET /api/v1/tools/id/<id>/valves/spec with 500.
        officecli_mcp_url: str = "http://officecli-mcp:8765"
        openwebui_url: str = "http://open-webui:8080"
        openwebui_browser_url: str = ""  # browser-reachable OWUI base; "" -> openwebui_url

    def __init__(self):
        self.valves = self.Valves()

    # --- swappable HTTP helpers (monkeypatched in tests) ---
    def _owui_headers(self, __request__: Any) -> dict[str, str]:
        """Forward the current user's credentials so we touch only their files."""
        headers: dict[str, str] = {}
        try:
            auth = __request__.headers.get("authorization")
            if auth:
                headers["Authorization"] = auth
            cookie = __request__.headers.get("cookie")
            if cookie:
                headers["Cookie"] = cookie
        except Exception:
            pass
        return headers

    def _owui_get(self, file_id: str, __request__: Any) -> bytes:
        """Fetch an attached file's bytes from OpenWebUI (upload action)."""
        url = f"{self.valves.openwebui_url}/api/v1/files/{file_id}/content"
        resp = requests.get(url, headers=self._owui_headers(__request__), timeout=60)
        resp.raise_for_status()
        return resp.content

    def _mcp_post(self, filename: str, data: bytes) -> dict:
        """Push bytes into officecli-mcp /files (upload action) -> file_id."""
        url = f"{self.valves.officecli_mcp_url}/files"
        files = {"file": (filename, data, "application/octet-stream")}
        resp = requests.post(url, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def _mcp_get(self, file_id: str) -> requests.Response:
        """Pull a finished file's bytes from officecli-mcp /files/{id} (download)."""
        url = f"{self.valves.officecli_mcp_url}/files/{file_id}"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        return resp

    def _owui_post(self, filename: str, data: bytes, mime: str, __request__: Any) -> dict:
        """POST bytes into OpenWebUI storage (download action) -> owui file id."""
        url = f"{self.valves.openwebui_url}/api/v1/files/?process=false"
        files = {"file": (filename, data, mime)}
        resp = requests.post(
            url, headers=self._owui_headers(__request__), files=files, timeout=120
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _filename_from_disposition(resp: requests.Response, fallback: str) -> str:
        """Pull the filename out of a content-disposition header, else fallback."""
        cd = resp.headers.get("content-disposition", "")
        # attachment; filename="Kimi_K3.pptx"
        for part in cd.split(";"):
            part = part.strip()
            if part.lower().startswith("filename="):
                name = part.split("=", 1)[1].strip().strip('"')
                if name:
                    return name
        return fallback or "download.docx"

    def officecli_file(
        self,
        action: str,
        __files__: list[dict[str, Any]] = [],  # noqa: B006
        __request__: Any = None,
        file_id: str = "",
        filename: str = "",
    ) -> str:
        """Move office documents in or out of officecli-mcp by handle.

        Args:
            action: "upload" (push attached files in, get file_id) or
                "download" (pull a finished file out as a browser-reachable link).
            __files__: upload only - OpenWebUI-injected attached-file dicts (have 'id','name').
            __request__: OpenWebUI-injected FastAPI Request; its Authorization/cookie
                are forwarded so we act as the current user (no stored key).
            file_id: download only - the officecli-mcp file_id to fetch.
            filename: download only, optional - override the saved filename.

        Returns:
            JSON string.
            upload:  {"files":[{"file_id":...,"filename":...}], "hint":"..."}
            download: {"url":"https://.../api/v1/files/{owui_id}/content","filename":...,"size":...}
        """
        if action == "upload":
            return self._upload(__files__, __request__)
        if action == "download":
            return self._download(file_id, filename, __request__)
        return json.dumps({"error": f"unknown action '{action}'"})

    def _upload(self, __files__: list[dict[str, Any]], __request__: Any) -> str:
        if not __files__:
            return json.dumps({"error": "no files attached"})
        out = []
        for f in __files__:
            file_id = f.get("id")
            name = f.get("name") or f.get("filename") or "upload.docx"
            if not file_id:
                file_id = (f.get("file") or {}).get("id")
            if not file_id:
                continue
            try:
                data = self._owui_get(file_id, __request__)
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

    def _download(self, file_id: str, filename: str, __request__: Any) -> str:
        if not file_id:
            return json.dumps({"error": "file_id required"})
        try:
            resp = self._mcp_get(file_id)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return json.dumps(
                    {
                        "error": (
                            "file_id not found or expired (TTL ~1h); re-create the "
                            "document or increase OFFICECLI_MCP_WORK_TTL_SECONDS"
                        )
                    }
                )
            return json.dumps({"error": f"officecli-mcp fetch failed: {e}"})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"officecli-mcp fetch failed: {e}"})

        data = resp.content
        name = filename or self._filename_from_disposition(resp, "download.docx")
        mime = "application/octet-stream"

        try:
            info = self._owui_post(name, data, mime, __request__)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"openwebui upload failed: {e}"})

        owui_id = info.get("id")
        if not owui_id:
            return json.dumps({"error": f"openwebui upload returned no id: {info}"})
        base = self.valves.openwebui_browser_url or self.valves.openwebui_url
        url = f"{base}/api/v1/files/{owui_id}/content"
        return json.dumps({"url": url, "filename": name, "size": len(data)})
