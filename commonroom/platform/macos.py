"""macOS specifics: Application Support, GUI-launch PATH, process groups, flock, osascript."""
from __future__ import annotations

import errno
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .base import DataFolderInUse, ExecutableHints, FolderPickerUnavailable, Platform, declared_file, env_file

# A GUI-launched app inherits a minimal PATH; these are where CLI installers usually put tools
# (official installers, Homebrew on Apple silicon and Intel, npm/volta/bun/pnpm global bins).
EXTRA_PATHS = ("/opt/homebrew/bin", "/usr/local/bin")
HOME_PATHS = (".local/bin", ".npm-global/bin", ".volta/bin", ".bun/bin", "Library/pnpm", ".local/share/mise/shims",
              ".asdf/shims")

_MARKER = "__COMMONROOM_PATH__"
_LOGIN_PATH_TTL = 60  # seconds; a rescan also clears it


def _login_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if not shell:
        try:
            import pwd
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except (ImportError, KeyError, OSError):
            shell = ""
    return shell if shell and os.path.isabs(shell) and os.access(shell, os.X_OK) else "/bin/zsh"

# "activate" brings osascript's dialog to the front; without it the picker can open behind the browser.
_PICK_FOLDER = ("on run argv", "activate", "POSIX path of (choose folder with prompt (item 1 of argv))", "end run")


class MacPlatform(Platform):
    name = "macos"
    label = "macOS"

    def data_dir(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Commonroom"

    def __init__(self):
        self._login_path: tuple[float, list[str]] | None = None
        self._login_lock = threading.Lock()

    def refresh(self) -> None:
        with self._login_lock:
            self._login_path = None

    def login_path(self) -> list[str]:
        """PATH as the user's login shell sets it up (nvm, fnm, asdf, mise, Homebrew, custom rc files).

        An app opened from Finder or the Dock gets launchd's minimal PATH instead, so tools installed
        through a version manager would otherwise be invisible. Cached briefly; [] if the shell fails.
        """
        with self._login_lock:
            if self._login_path and time.monotonic() - self._login_path[0] < _LOGIN_PATH_TTL:
                return list(self._login_path[1])
            paths: list[str] = []
            try:
                # -i and -l so both .zshrc/.bashrc and the profile files run; markers skip anything rc files print.
                done = subprocess.run([_login_shell(), "-ilc", f'printf "%s%s%s" {_MARKER} "$PATH" {_MARKER}'],
                                      stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                                      errors="replace", timeout=8, start_new_session=True)
                parts = (done.stdout or "").split(_MARKER)
                if len(parts) >= 3:
                    paths = [path for path in parts[-2].split(os.pathsep) if os.path.isabs(path)]
            except (OSError, subprocess.SubprocessError):
                pass
            self._login_path = (time.monotonic(), paths)
            return list(paths)

    def cli_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        # The inherited PATH comes first so a terminal's own choices (an active venv, pyenv) still win.
        paths = environment.get("PATH", os.defpath).split(os.pathsep)
        paths.extend(self.login_path())
        paths.extend(str(Path.home() / path) for path in HOME_PATHS)
        paths.extend(EXTRA_PATHS)
        environment["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
        return environment

    def popen_options(self) -> dict:
        # Own process group, so kill_process_tree can end the tool and everything it started.
        return {"start_new_session": True}

    def find_executable(self, hints: ExecutableHints) -> str | None:
        def usable(path: Path) -> bool:
            return path.is_file() and os.access(path, os.X_OK)

        if chosen := env_file(hints, usable):
            return str(chosen)
        search = self.cli_environment()["PATH"]
        for command in hints.commands:
            if found := shutil.which(command, path=search):
                return found
        for template in hints.paths.get(self.name, ()):
            if candidate := declared_file(template, usable, home=str(Path.home())):
                return str(candidate)
        return None

    def kill_process_tree(self, process: subprocess.Popen) -> None:
        group = None
        try:
            # popen_options starts each tool as the leader of its own group, so the group id is its pid.
            # getpgid fails once the leader has been reaped, while the rest of its group may still run.
            try:
                group = os.getpgid(process.pid)
            except ProcessLookupError:
                group = process.pid
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

    def bind_listener(self, sock: socket.socket, address) -> None:
        # On macOS SO_REUSEADDR also lets 127.0.0.1:<port> bind over a program listening on *:<port>, which
        # would then quietly lose its loopback traffic to us. Refuse when anything answers on the port.
        host, port = address[0], address[1]
        if port:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    pass
            except OSError:
                pass
            else:
                raise OSError(errno.EADDRINUSE, f"Port {port} is already in use.")
        super().bind_listener(sock, address)

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
