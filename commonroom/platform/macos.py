"""macOS specifics: Application Support, GUI-launch PATH, process groups, flock, osascript."""
from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import BinaryIO

from .base import DataFolderInUse, ExecutableHints, FolderPickerUnavailable, Platform

# A GUI-launched app inherits a minimal PATH; these are where CLI installers usually put tools.
EXTRA_PATHS = ("/opt/homebrew/bin", "/usr/local/bin")

_PICK_FOLDER = ("on run argv", "POSIX path of (choose folder with prompt (item 1 of argv))", "end run")


class MacPlatform(Platform):
    name = "macos"
    label = "macOS"

    def data_dir(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Commonroom"

    def cli_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        paths = environment.get("PATH", os.defpath).split(os.pathsep)
        paths.append(str(Path.home() / ".local" / "bin"))
        paths.extend(EXTRA_PATHS)
        environment["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
        return environment

    def popen_options(self) -> dict:
        # Own process group, so kill_process_tree can end the tool and everything it started.
        return {"start_new_session": True}

    def find_executable(self, hints: ExecutableHints) -> str | None:
        search = self.cli_environment()["PATH"]
        for command in hints.commands:
            if found := shutil.which(command, path=search):
                return found
        for template in hints.paths.get(self.name, ()):
            candidate = Path(template.format(home=Path.home(), localappdata=""))
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def kill_process_tree(self, process: subprocess.Popen) -> None:
        group = None
        try:
            group = os.getpgid(process.pid)
            if group == os.getpgrp():
                group = None  # Never signal our own group.
        except (AttributeError, OSError):
            pass
        for sig, fallback in ((signal.SIGTERM, process.terminate), (getattr(signal, "SIGKILL", signal.SIGTERM), process.kill)):
            try:
                if group is not None:
                    os.killpg(group, sig)
                else:
                    fallback()
            except (AttributeError, OSError):
                pass
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                continue

    def lock_exclusive(self, handle: BinaryIO) -> None:
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise DataFolderInUse(f"Another Commonroom process is using {Path(handle.name).parent}.") from exc

    def pick_folder(self, prompt: str) -> str | None:
        unavailable = "macOS could not open the folder picker. Enter the folder path instead."
        argv = ["/usr/bin/osascript"]
        for line in _PICK_FOLDER:
            argv += ["-e", line]
        try:
            result = subprocess.run([*argv, prompt], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FolderPickerUnavailable(unavailable) from exc
        if result.returncode != 0:
            if (result.stderr or "").rstrip().endswith("(-128)"):
                return None
            raise FolderPickerUnavailable(unavailable)
        return result.stdout.rstrip("\r\n") or None
