from __future__ import annotations

import os
import stat
from pathlib import Path


def test_asset_name_linux_x64(monkeypatch):
    from officecli_mcp import binary

    monkeypatch.setattr(binary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(binary.sys, "platform", lambda: "linux")
    assert binary.asset_name() == "officecli-linux-x64"


def test_asset_name_linux_arm64(monkeypatch):
    from officecli_mcp import binary

    monkeypatch.setattr(binary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(binary.sys, "platform", lambda: "linux")
    assert binary.asset_name() == "officecli-linux-arm64"


def test_ensure_binary_uses_cached(monkeypatch, tmp_path):
    from officecli_mcp import binary

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fake_bin = data_dir / "officecli"
    fake_bin.write_text("#!/bin/sh\necho hi\n")
    fake_bin.chmod(0o755)

    download_called = []

    def fake_download(_asset, _dest):
        download_called.append(True)

    monkeypatch.setattr(binary, "_download", fake_download)
    monkeypatch.setattr(binary, "_is_executable", lambda p: True)

    path = binary.ensure_binary(str(data_dir), version="latest")
    assert path == str(fake_bin)
    assert download_called == []  # cached, no download


def test_ensure_binary_downloads_when_missing(monkeypatch, tmp_path):
    from officecli_mcp import binary

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    def fake_download(_asset, dest):
        Path(dest).write_text("#!/bin/sh\necho hi\n")
        os.chmod(dest, 0o755)

    monkeypatch.setattr(binary, "_download", fake_download)
    # _is_executable reflects real existence so the download path actually triggers.
    monkeypatch.setattr(binary, "_is_executable", lambda p: Path(p).exists())

    path = binary.ensure_binary(str(data_dir), version="latest")
    assert Path(path).exists()
    mode = Path(path).stat().st_mode
    assert mode & stat.S_IXUSR
