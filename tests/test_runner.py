from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def _write_stub(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


def test_run_text_returns_stdout(settings, tmp_path, monkeypatch):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho 'VIEW TEXT OUTPUT'\n")

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "text"])
    assert res.exit_code == 0
    assert "VIEW TEXT OUTPUT" in res.stdout
    assert res.image_path is None


def test_run_html_intercept_returns_text(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho '<html><body>RENDERED</body></html>'\n")

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "html"])
    assert res.exit_code == 0
    assert "RENDERED" in res.stdout
    assert res.image_path is None  # html is stdout, not a file


def test_run_screenshot_intercept_writes_png(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.pptx", b"pptx-bytes")
    # Stub: when given -o, write a fake PNG signature to that path.
    # Use base64 to avoid printf escape-portability issues across shells.
    # iVBORw0KGgo= is base64 for the 8-byte PNG signature \x89PNG\r\n\x1a\n
    stub = tmp_path / "officecli"
    stub.write_bytes(
        b"#!/bin/sh\n"
        b"out=''\n"
        b"while [ $# -gt 0 ]; do\n"
        b"  if [ \"$1\" = '-o' ]; then out=\"$2\"; fi\n"
        b"  shift\n"
        b"done\n"
        b"if [ -n \"$out\" ]; then printf '%s' \"$(echo iVBORw0KGgo= | base64 -d)\" > \"$out\"; fi\n"
    )
    stub.chmod(0o755)

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "screenshot", "--page", "1"])
    assert res.exit_code == 0
    assert res.image_path is not None
    # The stub writes a fake PNG signature via base64 (printf escape semantics
    # differ across shells, so decode a fixed base64 string instead).
    png = runner.read_image(res.image_path)
    assert png.startswith(b"\x89PNG")


def test_run_substitutes_path_token(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho \"$@\" > /tmp/ocli_argv_test; echo ok\n")

    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["view", "{path}", "text"])
    assert res.exit_code == 0
    argv = Path("/tmp/ocli_argv_test").read_text()
    expected_path = str(store.path_for(info["file_id"]))
    assert expected_path in argv


def test_run_unknown_file_id_raises(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner, FileIDNotFound
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho x\n")
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    with pytest.raises(FileIDNotFound):
        runner.run("nope", ["view", "{path}", "text"])


def test_run_nonzero_exit_captured(settings, tmp_path):
    from officecli_mcp.runner import OfficeRunner
    from officecli_mcp.files import FileStore

    store = FileStore(work_dir=settings.work_dir, ttl_seconds=3600)
    info = store.put("r.docx", b"docx-bytes")
    stub = tmp_path / "officecli"
    _write_stub(stub, "#!/bin/sh\necho 'boom' 1>&2; exit 3\n")
    runner = OfficeRunner(binary_path=str(stub), file_store=store)
    res = runner.run(info["file_id"], ["validate", "{path}"])
    assert res.exit_code == 3
    assert "boom" in res.stderr
