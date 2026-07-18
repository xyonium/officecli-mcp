"""HTTP file store: upload/download/delete office docs by file_id handle."""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router

log = logging.getLogger(__name__)

_SAFE_EXT = {"docx", "xlsx", "pptx"}
STAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "csv", "tsv"}


def _safe_filename(name: str) -> str:
    """Strip path separators; keep only the basename."""
    base = os.path.basename(name).strip()
    if base in {"", ".", ".."}:
        raise ValueError("invalid filename")
    return base


class FileStore:
    """Owns /work/{file_id}/ and the TTL sweep."""

    def __init__(self, work_dir: str, ttl_seconds: int):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds

    def _dir(self, file_id: str) -> Path:
        return self.work_dir / file_id

    def put(self, filename: str, data: bytes) -> dict:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in _SAFE_EXT:
            raise ValueError(f"extension .{ext} not allowed")
        safe = _safe_filename(filename)
        file_id = uuid.uuid4().hex
        d = self._dir(file_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / safe).write_bytes(data)
        return {"file_id": file_id, "filename": safe, "size": len(data), "mime": _mime(ext)}

    def stage_asset(self, target_file_id: str, filename: str, data: bytes) -> dict:
        """Write an asset (image/CSV/TSV) into an EXISTING document's workdir.

        Unlike put(), this does not create a new file_id; it drops the asset
        alongside the target document so officecli can reference it by relative
        filename (src=kimi.png).
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in STAGE_EXT:
            raise ValueError(f"extension .{ext} not allowed for staging")
        d = self._dir(target_file_id)
        if not d.exists():
            raise KeyError(target_file_id)
        safe = _safe_filename(filename)
        (d / safe).write_bytes(data)
        return {"asset": safe, "target": target_file_id}

    def path_for(self, file_id: str, filename: str | None = None) -> Path:
        d = self._dir(file_id)
        if not d.exists():
            raise KeyError(file_id)
        if filename:
            return d / _safe_filename(filename)
        # Return only document-extension files; staged assets (png/csv/...) and
        # the screenshot product (shot.png) are never the document itself.
        docs = [
            p for p in d.iterdir()
            if p.is_file() and p.suffix.lower().lstrip(".") in _SAFE_EXT
        ]
        if not docs:
            raise KeyError(file_id)
        return docs[0]

    def read(self, file_id: str) -> tuple[bytes, str]:
        p = self.path_for(file_id)
        return p.read_bytes(), p.name

    def delete(self, file_id: str) -> bool:
        d = self._dir(file_id)
        if not d.exists():
            return False
        for child in d.iterdir():
            child.unlink(missing_ok=True)
        d.rmdir()
        return True

    def sweep(self) -> int:
        """Delete workdirs older than ttl. Returns count removed."""
        cutoff = time.time() - self.ttl_seconds
        removed = 0
        for d in self.work_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                mtime = d.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                self.delete(d.name)
                removed += 1
        return removed


def _mime(ext: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }.get(ext, "application/octet-stream")


def _check_api_key(request: Request, api_key: str) -> JSONResponse | None:
    if not api_key:
        return None
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token != api_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


async def upload(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            payload = await request.json()
            filename = payload["filename"]
            data = base64.b64decode(payload["data_base64"])
        else:
            form = await request.form()
            upload_file = form["file"]
            filename = upload_file.filename or "upload.docx"
            data = await upload_file.read()
    except (KeyError, ValueError) as e:
        return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return JSONResponse({"error": "file too large"}, status_code=413)

    try:
        info = store.put(filename, data)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=415)
    return JSONResponse(info)


async def download(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    file_id = request.path_params["file_id"]
    try:
        data, name = store.read(file_id)
    except KeyError:
        return JSONResponse({"error": "file_id not found or expired"}, status_code=404)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"content-disposition": f'attachment; filename="{name}"'},
    )


async def stage(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            payload = await request.json()
            target_file_id = payload["target_file_id"]
            filename = payload["filename"]
            data = base64.b64decode(payload["data_base64"])
        else:
            form = await request.form()
            target_file_id = form["target_file_id"]
            upload_file = form["file"]
            filename = upload_file.filename or "asset.bin"
            data = await upload_file.read()
    except (KeyError, ValueError) as e:
        return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return JSONResponse({"error": "file too large"}, status_code=413)

    try:
        info = store.stage_asset(target_file_id, filename, data)
    except KeyError:
        return JSONResponse(
            {"error": "target file_id not found or expired"}, status_code=404
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=415)
    return JSONResponse(info)


async def delete(request: Request) -> Response:
    settings = request.app.state.settings
    err = _check_api_key(request, settings.api_key)
    if err:
        return err
    store: FileStore = request.app.state.file_store
    file_id = request.path_params["file_id"]
    removed = store.delete(file_id)
    return JSONResponse({"deleted": removed})


def build_files_router(store: FileStore, settings) -> Router:
    routes = [
        Route("/files", upload, methods=["POST"]),
        Route("/files/stage", stage, methods=["POST"]),
        Route("/files/{file_id}", download, methods=["GET"]),
        Route("/files/{file_id}", delete, methods=["DELETE"]),
    ]
    return Router(routes)
