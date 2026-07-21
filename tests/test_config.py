"""Tests for config.py."""
from __future__ import annotations

import importlib


def test_new_settings_defaults(monkeypatch):
    for var in (
        "OFFICECLI_MCP_SCREENSHOT_MAX_EDGE",
        "OFFICECLI_MCP_OWUI_SYNC",
        "OFFICECLI_MCP_OWUI_URL",
        "OFFICECLI_MCP_OWUI_API_KEY",
        "OFFICECLI_MCP_OWUI_TOOL_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    import officecli_mcp.config as cfg

    try:
        importlib.reload(cfg)
        s = cfg.Settings()
        assert s.screenshot_max_edge == 1024
        assert s.owui_sync is True
        assert s.owui_url == ""
        assert s.owui_api_key == ""
        assert s.owui_tool_id == "officecli"
    finally:
        importlib.reload(cfg)


def test_new_settings_from_env(monkeypatch):
    monkeypatch.setenv("OFFICECLI_MCP_SCREENSHOT_MAX_EDGE", "512")
    monkeypatch.setenv("OFFICECLI_MCP_OWUI_SYNC", "0")
    monkeypatch.setenv("OFFICECLI_MCP_OWUI_URL", "http://open-webui:8080")
    monkeypatch.setenv("OFFICECLI_MCP_OWUI_API_KEY", "sk-test")

    import officecli_mcp.config as cfg

    try:
        importlib.reload(cfg)
        s = cfg.Settings()
        assert s.screenshot_max_edge == 512
        assert s.owui_sync is False
        assert s.owui_url == "http://open-webui:8080"
        assert s.owui_api_key == "sk-test"
    finally:
        importlib.reload(cfg)
