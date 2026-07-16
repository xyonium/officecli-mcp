"""Environment-based configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    transport: str = os.environ.get("OFFICECLI_MCP_TRANSPORT", "http")
    host: str = os.environ.get("OFFICECLI_MCP_HOST", "0.0.0.0")
    port: int = _env_int("OFFICECLI_MCP_PORT", 8765)
    data_dir: str = os.environ.get("OFFICECLI_MCP_DATA_DIR", "/data")
    work_dir: str = os.environ.get("OFFICECLI_MCP_WORK_DIR", "/work")
    work_ttl_seconds: int = _env_int("OFFICECLI_MCP_WORK_TTL_SECONDS", 3600)
    max_upload_mb: int = _env_int("OFFICECLI_MCP_MAX_UPLOAD_MB", 50)
    officecli_version: str = os.environ.get("OFFICECLI_VERSION", "latest")
    officecli_sha256: str = os.environ.get("OFFICECLI_SHA256", "")
    api_key: str = os.environ.get("OFFICECLI_MCP_API_KEY", "")
    allowed_extensions: tuple[str, ...] = ("docx", "xlsx", "pptx")

    @property
    def binary_path(self) -> str:
        return os.path.join(self.data_dir, "officecli")


def get_settings() -> Settings:
    return Settings()
