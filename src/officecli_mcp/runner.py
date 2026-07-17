"""Run officecli as a subprocess; intercept html (stdout) and screenshot (PNG)."""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from officecli_mcp.files import FileStore

log = logging.getLogger(__name__)


class FileIDNotFound(Exception):
    """The file_id is unknown or expired."""


def _subprocess_env() -> dict[str, str]:
    """Env for officecli subprocesses.

    officecli is a self-contained .NET app that hard-crashes without ICU
    ("Couldn't find a valid ICU package"). We bundle no libicu, so force .NET
    globalization-invariant mode. Set here (not just in the Dockerfile) so
    local-dev runs without the container ENV also work, and so a tool that
    later spawns officecli outside our normal path still inherits it.
    """
    env = dict(os.environ)
    env.setdefault("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1")
    env["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
    return env


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    image_path: str | None  # set when a screenshot PNG was produced


class OfficeRunner:
    def __init__(self, binary_path: str, file_store: FileStore):
        self.binary_path = binary_path
        self.file_store = file_store

    def resolve(self, file_id: str) -> Path:
        try:
            return self.file_store.path_for(file_id)
        except KeyError as e:
            raise FileIDNotFound(file_id) from e

    def run(self, file_id: str, argv_template: list[str]) -> RunResult:
        """argv_template uses the literal token '{path}' where the file path goes.

        Special handling:
        - 'view ... html' / 'view ... svg' / text modes: no -o, capture stdout.
        - 'view ... screenshot': if no -o present, inject -o <workdir>/shot.png,
          then read the PNG into image_path.
        """
        path = str(self.resolve(file_id))
        argv = [a.replace("{path}", path) for a in argv_template]
        cwd = str(Path(path).parent)

        is_screenshot = "screenshot" in argv
        image_path: str | None = None
        if is_screenshot and "-o" not in argv:
            image_path = str(Path(cwd) / "shot.png")
            argv += ["-o", image_path]

        proc = subprocess.run(
            [self.binary_path, *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        return RunResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            image_path=image_path,
        )

    def read_image(self, image_path: str) -> bytes:
        return Path(image_path).read_bytes()

    def _raw_run(self, argv: list[str], cwd: str) -> RunResult:
        """Run an arbitrary officecli argv with no {path} substitution."""
        proc = subprocess.run(
            [self.binary_path, *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        return RunResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            image_path=None,
        )
