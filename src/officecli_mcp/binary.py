"""Locate, download, and verify the officecli binary."""
from __future__ import annotations

import logging
import os
import platform
import stat
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

RELEASE_BASE = "https://github.com/iOfficeAI/OfficeCLI/releases"
_DOWNLOAD_TIMEOUT = 120.0


def asset_name() -> str:
    """Return the release asset name for the current platform."""
    system = platform.system()
    machine = platform.machine().lower()
    is_alpine = "alpine" in str(sys.platform) or "musl" in os.environ.get("OFFICECLI_MCP_LIBC", "")

    if system == "Linux":
        arch = "x64" if machine in {"x86_64", "amd64"} else "arm64"
        prefix = "officecli-linux-alpine-" if is_alpine else "officecli-linux-"
        return f"{prefix}{arch}"
    if system == "Darwin":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return f"officecli-mac-{arch}"
    if system == "Windows":
        arch = "x64" if machine in {"x86_64", "amd64"} else "arm64"
        return f"officecli-win-{arch}.exe"
    raise RuntimeError(f"Unsupported platform: {system} {machine}")


def _is_executable(path: str) -> bool:
    p = Path(path)
    return p.exists() and bool(p.stat().st_mode & stat.S_IXUSR)


def _download(asset: str, dest: str) -> None:
    url = f"{RELEASE_BASE}/latest/download/{asset}"
    log.info("Downloading officecli from %s", url)
    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(resp.content)
    os.chmod(dest, 0o755)


def _verify_sha256(path: str, expected: str) -> None:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError(f"officecli sha256 mismatch: expected {expected}, got {actual}")


def ensure_binary(data_dir: str, version: str = "latest", sha256: str = "") -> str:
    """Ensure the officecli binary exists at data_dir/officecli; download if missing.

    Returns the absolute path to the binary. If a cached binary exists it is
    reused (we do not re-download on every start when version=='latest', to
    keep startup fast offline). If sha256 is set, verify it.
    """
    dest = str(Path(data_dir) / "officecli")
    if not _is_executable(dest):
        _download(asset_name(), dest)
    if sha256:
        _verify_sha256(dest, sha256)
    if not _is_executable(dest):
        os.chmod(dest, 0o755)
    return dest
