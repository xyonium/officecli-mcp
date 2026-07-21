"""Behavior tests for the shim's run/tools actions (against the generated example)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "officecli_file_tool", ROOT / "examples" / "openwebui_officecli_file.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_run_forwards_tool_and_arguments(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    seen = {}

    def fake_call(path, payload=None):
        seen["path"] = path
        seen["payload"] = payload
        return {"content": [{"type": "text", "text": "OK"}], "isError": False}

    monkeypatch.setattr(tools, "_mcp_call", fake_call)
    out = json.loads(
        await tools.officecli_file(
            action="run",
            tool="officecli_set",
            arguments='{"file_id":"f1","selector":"/slide[1]","prop":["x=2cm"]}',
        )
    )
    assert seen["path"] == "/tools/call"
    assert seen["payload"] == {
        "name": "officecli_set",
        "arguments": {"file_id": "f1", "selector": "/slide[1]", "prop": ["x=2cm"]},
    }
    assert out == {"content": [{"type": "text", "text": "OK"}], "isError": False}


async def test_run_rejects_bad_arguments_json():
    mod = _load()
    out = json.loads(await mod.Tools().officecli_file(action="run", tool="t", arguments="{nope"))
    assert "JSON" in out["error"]


async def test_run_unknown_tool_surfaces_404(monkeypatch):
    import requests

    mod = _load()
    tools = mod.Tools()

    def fake_call(path, payload=None):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("nf", response=resp)

    monkeypatch.setattr(tools, "_mcp_call", fake_call)
    out = json.loads(await tools.officecli_file(action="run", tool="officecli_nope"))
    assert "unknown tool" in out["error"]


async def test_run_converts_image_blocks_to_data_urls(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    monkeypatch.setattr(
        tools,
        "_mcp_call",
        lambda path, payload=None: {
            "content": [{"type": "image", "data": "QUJD", "mimeType": "image/png"}],
            "isError": False,
        },
    )
    out = json.loads(await tools.officecli_file(action="run", tool="officecli_view_screenshot", arguments="{}"))
    assert out["content"][0]["data"] == "data:image/png;base64,QUJD"
    assert out["content"][0]["type"] == "image"


async def test_run_propagates_iserror_text(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    monkeypatch.setattr(
        tools,
        "_mcp_call",
        lambda path, payload=None: {
            "content": [{"type": "text", "text": "officecli exited 2: UNSUPPORTED prop"}],
            "isError": True,
        },
    )
    out = json.loads(await tools.officecli_file(action="run", tool="officecli_set", arguments="{}"))
    assert out["isError"] is True
    assert "UNSUPPORTED" in out["content"][0]["text"]


async def test_tools_action_returns_manifest(monkeypatch):
    mod = _load()
    tools = mod.Tools()
    monkeypatch.setattr(
        tools, "_mcp_call", lambda path, payload=None: {"revision": "x", "tools": []}
    )
    out = json.loads(await tools.officecli_file(action="tools"))
    assert out["revision"] == "x"
