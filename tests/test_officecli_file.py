from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_tools():
    spec = importlib.util.spec_from_file_location(
        "openwebui_officecli_file", Path("examples/openwebui_officecli_file.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


async def test_download_action_pushes_to_owui_storage(monkeypatch):
    """download: GET bytes from officecli-mcp, POST to OWUI storage, return browser URL."""
    from officecli_mcp import server as server_mod

    # Real officecli-mcp app (stub binary not needed for /files GET, but build_app
    # expects a binary; stub it).
    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S",
        (),
        {
            "transport": "http",
            "host": "127.0.0.1",
            "port": 8765,
            "data_dir": "/tmp/_fdata",
            "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600,
            "max_upload_mb": 50,
            "officecli_version": "latest",
            "officecli_sha256": "",
            "api_key": "",
            "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        },
    )()
    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)

    # Put a file into the store directly so /files/{id} serves it.
    file_bytes = b"PK\x03\x04downloaded-pptx-bytes"
    put = TestClient(mcp_app).post(
        "/files", files={"file": ("Kimi_K3.pptx", file_bytes, "application/octet-stream")}
    )
    assert put.status_code == 200, put.text
    file_id = put.json()["file_id"]

    # Fake OpenWebUI: accepts POST /api/v1/files/?process=false, echoes the
    # received Authorization header and returns {"id": "owui-xyz"}.
    received_auth = {}

    async def fake_upload(request):
        received_auth["auth"] = request.headers.get("authorization")
        received_auth["process"] = request.query_params.get("process")
        return Response(json.dumps({"id": "owui-xyz"}), media_type="application/json")

    owui = Starlette(routes=[Route("/api/v1/files/", fake_upload, methods=["POST"])])
    owui_client = TestClient(owui)
    mcp_client = TestClient(mcp_app)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp",
        openwebui_url="http://owui",
        openwebui_browser_url="https://ai.savorcare.com",
    )

    monkeypatch.setattr(
        tools,
        "_mcp_get",
        lambda fid: mcp_client.get(f"/files/{fid}"),
    )
    monkeypatch.setattr(
        tools,
        "_owui_post",
        lambda fname, data, mime, _req: owui_client.post(
            "/api/v1/files/?process=false",
            headers=tools._owui_headers(FakeRequest({"authorization": "Bearer current-user-token"})),
            files={"file": (fname, data, mime)},
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="download",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
            file_id=file_id,
        )
    )
    assert result["url"] == "https://ai.savorcare.com/api/v1/files/owui-xyz/content", result
    assert result["filename"] == "Kimi_K3.pptx", result
    assert result["size"] == len(file_bytes), result
    assert received_auth["auth"] == "Bearer current-user-token", received_auth
    assert received_auth["process"] == "false", received_auth


async def test_download_action_emits_files_event_for_sidebar_chip(monkeypatch):
    """download: emit a __event_emitter__ {type:'files'} event so OpenWebUI
    renders a downloadable FileItem chip on the assistant message (instead of
    the model having to print a URL the user copies out of the tool call).

    The FileItem component appends '/content' itself (FileItem.svelte:
    window.open(`${url}/content`)), so the emitted url MUST be the bare file
    base WITHOUT '/content' - emitting .../content would make it open
    .../content/content -> 404. The returned JSON url keeps '/content' (that
    one is for the model to print as a text link).
    """
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata", "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        })()
    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    file_bytes = b"PK\x03\x04downloaded-pptx-bytes"
    file_id = mcp_client.post(
        "/files", files={"file": ("Kimi_K3.pptx", file_bytes, "application/octet-stream")}
    ).json()["file_id"]

    async def fake_upload(request):
        return Response(json.dumps({"id": "owui-xyz"}), media_type="application/json")

    owui = Starlette(routes=[Route("/api/v1/files/", fake_upload, methods=["POST"])])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp",
        openwebui_url="http://owui",
        openwebui_browser_url="https://ai.savorcare.com",
    )
    monkeypatch.setattr(tools, "_mcp_get", lambda fid: mcp_client.get(f"/files/{fid}"))
    monkeypatch.setattr(
        tools, "_owui_post",
        lambda fname, data, mime, _req: owui_client.post(
            "/api/v1/files/?process=false",
            headers=tools._owui_headers(FakeRequest({"authorization": "Bearer t"})),
            files={"file": (fname, data, mime)},
        ).json(),
    )

    emitted: list[dict] = []

    async def collector(event):
        emitted.append(event)

    result = json.loads(
        await tools.officecli_file(
            action="download",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
            __event_emitter__=collector,
            file_id=file_id,
        )
    )

    # The returned JSON url keeps /content (for the model's text link).
    assert result["url"] == "https://ai.savorcare.com/api/v1/files/owui-xyz/content", result

    # Exactly one files event was emitted, with a single file chip.
    assert len(emitted) == 1, emitted
    evt = emitted[0]
    assert evt["type"] == "files", evt
    files = evt["data"]["files"]
    assert len(files) == 1, files
    chip = files[0]
    assert chip["type"] == "file", chip
    assert chip["name"] == "Kimi_K3.pptx", chip
    assert chip["size"] == len(file_bytes), chip
    # The chip url is the BARE file base - FileItem appends /content itself.
    assert chip["url"] == "https://ai.savorcare.com/api/v1/files/owui-xyz", chip
    assert "/content" not in chip["url"].rsplit("owui-xyz", 1)[-1], chip


async def test_download_without_event_emitter_still_works(monkeypatch):
    """When __event_emitter__ is None (e.g. older OpenWebUI or non-chat path),
    download must still return the JSON url and not crash."""
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata", "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        })()
    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    file_bytes = b"PK\x03\x04pptx"
    file_id = mcp_client.post(
        "/files", files={"file": ("x.pptx", file_bytes, "application/octet-stream")}
    ).json()["file_id"]

    async def fake_upload(request):
        return Response(json.dumps({"id": "owui-abc"}), media_type="application/json")

    owui = Starlette(routes=[Route("/api/v1/files/", fake_upload, methods=["POST"])])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui",
        openwebui_browser_url="https://ai.savorcare.com",
    )
    monkeypatch.setattr(tools, "_mcp_get", lambda fid: mcp_client.get(f"/files/{fid}"))
    monkeypatch.setattr(
        tools, "_owui_post",
        lambda fname, data, mime, _req: owui_client.post(
            "/api/v1/files/?process=false",
            headers=tools._owui_headers(FakeRequest({"authorization": "Bearer t"})),
            files={"file": (fname, data, mime)},
        ).json(),
    )

    # No __event_emitter__ passed -> default None. Must not raise.
    result = json.loads(
        await tools.officecli_file(
            action="download",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
            file_id=file_id,
        )
    )
    assert result["url"] == "https://ai.savorcare.com/api/v1/files/owui-abc/content", result


async def test_upload_action_returns_file_ids(monkeypatch):
    """upload: fetch each attached file from OWUI, POST to officecli-mcp, return file_ids."""
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S",
        (),
        {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata", "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        },
    )()
    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    file_bytes = b"PK\x03\x04real-docx"
    received_auth = {}

    async def fake_content(request):
        received_auth["auth"] = request.headers.get("authorization")
        return Response(file_bytes, media_type="application/octet-stream")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui"
    )
    monkeypatch.setattr(
        tools,
        "_owui_get",
        lambda fid, __request__: owui_client.get(
            f"/api/v1/files/{fid}/content", headers=tools._owui_headers(__request__)
        ).content,
    )
    monkeypatch.setattr(
        tools,
        "_mcp_post",
        lambda fname, data: mcp_client.post(
            "/files", files={"file": (fname, data, "application/octet-stream")}
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="upload",
            __files__=[{"id": "f1", "name": "report.docx"}],
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
        )
    )
    assert result["files"], result
    assert result["files"][0]["file_id"]
    assert result["files"][0]["filename"] == "report.docx"
    assert "officecli_" in result["hint"]
    assert received_auth["auth"] == "Bearer current-user-token", received_auth


async def test_unknown_action_returns_error():
    mod = _load_tools()
    tools = mod.Tools()
    result = json.loads(await tools.officecli_file(action="frobnicate"))
    assert result == {"error": "unknown action 'frobnicate'"}


async def test_download_without_file_id_returns_error():
    mod = _load_tools()
    tools = mod.Tools()
    result = json.loads(await tools.officecli_file(action="download"))
    assert result == {"error": "file_id required"}


def test_owui_headers_forwards_authorization():
    mod = _load_tools()
    tools = mod.Tools()
    h = tools._owui_headers(FakeRequest({"authorization": "Bearer u123", "cookie": "sid=abc"}))
    assert h["Authorization"] == "Bearer u123"
    assert h["Cookie"] == "sid=abc"
    assert tools._owui_headers(None) == {}


async def test_stage_action_from_source_file_id(monkeypatch):
    """stage(source_file_id): fetch OWUI image bytes, POST to /files/stage, return asset."""
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata", "work_dir": "/tmp/_fwork",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test",
        })()
    shutil.rmtree("/tmp/_fwork", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    # Seed a target pptx in officecli-mcp.
    target = mcp_client.post(
        "/files", files={"file": ("deck.pptx", b"PK\x03\x04pptx", "application/octet-stream")}
    ).json()["file_id"]

    image_bytes = b"\x89PNG\r\n\x1a\ngenerated-image"
    received_auth = {}

    async def fake_content(request):
        received_auth["auth"] = request.headers.get("authorization")
        return Response(image_bytes, media_type="image/png")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui"
    )
    monkeypatch.setattr(
        tools, "_owui_get",
        lambda fid, __request__: owui_client.get(
            f"/api/v1/files/{fid}/content", headers=tools._owui_headers(__request__)
        ).content,
    )
    monkeypatch.setattr(
        tools, "_mcp_stage",
        lambda target_fid, fname, data: mcp_client.post(
            "/files/stage",
            data={"target_file_id": target_fid, "filename": fname},
            files={"file": (fname, data, "image/png")},
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="stage",
            file_id=target,
            source_file_id="owui-img-1",
            filename="kimi.png",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
        )
    )
    assert result["asset"] == "kimi.png", result
    assert result["target"] == target, result
    assert "officecli_add" in result["hint"]
    assert received_auth["auth"] == "Bearer current-user-token", received_auth
    # Asset actually landed in the target workdir.
    from pathlib import Path as P
    assert P("/tmp/_fwork", target, "kimi.png").exists()


async def test_stage_without_filename_infers_png_from_bytes(monkeypatch):
    """stage(source_file_id) with NO filename: infer .png from image magic bytes
    instead of falling back to asset.bin (which the STAGE_EXT whitelist 415s).

    Reproduces the real model failure: the model has only an OpenWebUI image id,
    no filename, so it omits filename. The shim must derive a stgable extension
    from the bytes themselves.
    """
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test3")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata3", "work_dir": "/tmp/_fwork3",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test3",
        })()
    shutil.rmtree("/tmp/_fwork3", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata3", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    target = mcp_client.post(
        "/files", files={"file": ("deck.pptx", b"PK\x03\x04pptx", "application/octet-stream")}
    ).json()["file_id"]

    # Real PNG magic bytes (\x89PNG\r\n\x1a\n) + payload, no filename provided.
    image_bytes = b"\x89PNG\r\n\x1a\ngenerated-image"

    async def fake_content(request):
        return Response(image_bytes, media_type="image/png")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui"
    )
    monkeypatch.setattr(
        tools, "_owui_get",
        lambda fid, __request__: owui_client.get(
            f"/api/v1/files/{fid}/content", headers=tools._owui_headers(__request__)
        ).content,
    )
    # Route through the REAL /files/stage endpoint via mcp_client (no _mcp_stage
    # mock) so the STAGE_EXT extension check actually runs and would 415 on a
    # wrong ext like asset.bin.
    monkeypatch.setattr(
        tools, "_mcp_stage",
        lambda target_fid, fname, data: mcp_client.post(
            "/files/stage",
            data={"target_file_id": target_fid},
            files={"file": (fname, data, "application/octet-stream")},
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="stage",
            file_id=target,
            source_file_id="owui-img-1",
            # NOTE: no filename= ... the model's real call.
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
        )
    )
    assert "error" not in result, result
    assert result["asset"].endswith(".png"), result
    assert result["target"] == target, result
    from pathlib import Path as P
    assert P("/tmp/_fwork3", target, result["asset"]).exists()


async def test_stage_action_from_files(monkeypatch):
    """stage(__files__): take first attached file, POST to /files/stage."""
    from officecli_mcp import server as server_mod

    stub = Path("/tmp/_officecli_stub_for_file_test2")
    stub.write_text("#!/bin/sh\necho ok\n")
    stub.chmod(0o755)
    monkeypatch.setattr(server_mod.binary, "ensure_binary", lambda *a, **k: str(stub))
    settings = type(
        "S", (), {
            "transport": "http", "host": "127.0.0.1", "port": 8765,
            "data_dir": "/tmp/_fdata2", "work_dir": "/tmp/_fwork2",
            "work_ttl_seconds": 3600, "max_upload_mb": 50,
            "officecli_version": "latest", "officecli_sha256": "",
            "api_key": "", "allowed_extensions": ("docx", "xlsx", "pptx"),
            "dns_rebinding_protection": False,
            "allowed_hosts": ("127.0.0.1:*", "localhost:*"),
            "binary_path": "/tmp/_officecli_stub_for_file_test2",
        })()
    shutil.rmtree("/tmp/_fwork2", ignore_errors=True)
    shutil.rmtree("/tmp/_fdata2", ignore_errors=True)
    mcp_app = server_mod.build_app(settings)
    mcp_client = TestClient(mcp_app)

    target = mcp_client.post(
        "/files", files={"file": ("deck.pptx", b"PK\x03\x04pptx", "application/octet-stream")}
    ).json()["file_id"]

    csv_bytes = b"a,b\n1,2\n"
    received = {}

    async def fake_content(request):
        received["fetched"] = True
        return Response(csv_bytes, media_type="text/csv")

    owui = Starlette(routes=[Route("/api/v1/files/{file_id}/content", fake_content)])
    owui_client = TestClient(owui)

    mod = _load_tools()
    tools = mod.Tools()
    tools.valves = mod.Tools.Valves(
        officecli_mcp_url="http://mcp", openwebui_url="http://owui"
    )
    monkeypatch.setattr(
        tools, "_owui_get",
        lambda fid, __request__: owui_client.get(
            f"/api/v1/files/{fid}/content", headers=tools._owui_headers(__request__)
        ).content,
    )
    monkeypatch.setattr(
        tools, "_mcp_stage",
        lambda target_fid, fname, data: mcp_client.post(
            "/files/stage",
            data={"target_file_id": target_fid, "filename": fname},
            files={"file": (fname, data, "text/csv")},
        ).json(),
    )

    result = json.loads(
        await tools.officecli_file(
            action="stage",
            file_id=target,
            __files__=[{"id": "csv-1", "name": "kpi.csv"}],
            filename="kpi.csv",
            __request__=FakeRequest({"authorization": "Bearer current-user-token"}),
        )
    )
    assert result["asset"] == "kpi.csv", result
    assert result["target"] == target, result
    assert "officecli_import" in result["hint"]
    assert received.get("fetched")


async def test_stage_without_target_file_id_returns_error():
    mod = _load_tools()
    tools = mod.Tools()
    result = json.loads(await tools.officecli_file(action="stage"))
    assert result == {"error": "file_id (target document) required"}


def test_valves_is_pydantic_model_with_schema():
    """OpenWebUI calls Valves.schema() to render the Valves editor and
    Valves(**form_data) to apply saved values. A plain class with __init__
    has no .schema() and crashes GET /api/v1/tools/id/<id>/valves/spec (500).
    Valves MUST be a pydantic BaseModel exposing all three Valve fields.
    """
    mod = _load_tools()
    Valves = mod.Tools.Valves

    # .schema() is the exact (pydantic-v1-compat) method OpenWebUI's tools
    # router calls at routers/tools.py:761. It is deprecated in pydantic v2
    # but OpenWebUI still uses it, so we pin the real contract here.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        schema = Valves.schema()
    props = schema["properties"]
    assert {"officecli_mcp_url", "openwebui_url", "openwebui_browser_url"} <= set(props)

    # Valves(**form_data) applies saved values (OpenWebUI update path), and
    # unset fields keep their defaults.
    v = Valves(openwebui_browser_url="https://ai.savorcare.com")
    assert v.officecli_mcp_url == "http://officecli-mcp:8765"
    assert v.openwebui_browser_url == "https://ai.savorcare.com"

