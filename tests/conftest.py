"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make src importable without an installed package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def settings(tmp_path: Path) -> "object":
    from officecli_mcp.config import Settings

    return Settings(
        transport="http",
        host="127.0.0.1",
        port=8765,
        data_dir=str(tmp_path / "data"),
        work_dir=str(tmp_path / "work"),
        work_ttl_seconds=3600,
        max_upload_mb=50,
        officecli_version="latest",
        officecli_sha256="",
        api_key="",
        allowed_extensions=("docx", "xlsx", "pptx"),
    )
