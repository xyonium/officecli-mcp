"""Environment-based configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Host headers the MCP streamable-HTTP endpoint is reachable by, when DNS
# rebinding protection is on. Cross-container clients (OpenWebUI calling
# http://officecli-mcp:8765/mcp) must be allow-listed here or they get 421.
_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


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


def _parse_allowed_hosts() -> tuple[str, ...]:
    """OFFICECLI_MCP_ALLOWED_HOSTS overrides the default localhost set, not augments.

    Comma-separated. Exact host values or "host:*" wildcard-port patterns:
    set "officecli-mcp:8765" to allow that exact Host, or "officecli-mcp:*" to
    allow any port on that name. When unset, falls back to localhost-only.
    """
    raw = os.environ.get("OFFICECLI_MCP_ALLOWED_HOSTS")
    if raw is None:
        return _DEFAULT_ALLOWED_HOSTS
    hosts = tuple(h.strip() for h in raw.split(",") if h.strip())
    return hosts


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
    # MCP streamable-HTTP DNS-rebinding / Host-header guard (mcp sdk transport_security).
    dns_rebinding_protection: bool = _env_bool("OFFICECLI_MCP_DNS_REBINDING_PROTECTION", True)
    allowed_hosts: tuple[str, ...] = field(default_factory=_parse_allowed_hosts)

    @property
    def binary_path(self) -> str:
        return os.path.join(self.data_dir, "officecli")


def get_settings() -> Settings:
    return Settings()
