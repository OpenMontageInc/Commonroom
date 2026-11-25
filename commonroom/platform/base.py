"""The operating-system seam.

Everything that differs between Windows and macOS (and later Linux) lives behind
this interface. The rest of Commonroom asks the current ``Platform`` instead of
checking ``sys.platform`` or ``os.name`` itself. Adding an OS means adding one
subclass and one line in ``commonroom.platform.current_platform``.
"""
from __future__ import annotations

import socket
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Mapping


@dataclass(frozen=True)
class ExecutableHints:
    """Where a command-line tool may live, declared as data by each provider.

    ``commands`` are looked up on PATH. ``paths`` maps a platform name
    ("windows", "macos", ...) to extra candidate files; each entry may use the
    placeholders ``{home}`` and ``{localappdata}``. ``store_packages`` maps a
    platform name to (package name, path inside the package) pairs for tools
    installed through an OS app store (for example the Windows Codex app).
    """

    commands: tuple[str, ...]
    paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    store_packages: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)


class DataFolderInUse(RuntimeError):
    """Another Commonroom service already owns the data folder."""


class FolderPickerUnavailable(RuntimeError):
    """The native folder picker could not be shown; the user should type a path."""


class Platform(ABC):
    #: Short stable name used as a key in ExecutableHints ("windows", "macos").
    name: str
    #: Human label shown in the setup screen ("Windows", "macOS").
    label: str

    @abstractmethod
    def data_dir(self) -> Path:
        """Folder for Commonroom's own databases and settings."""

    @abstractmethod
    def find_executable(self, hints: ExecutableHints) -> str | None:
        """Return the full path of the tool, or None when it is not installed."""

    def cli_environment(self) -> dict[str, str] | None:
        """Environment for child processes, or None to inherit unchanged."""
        return None

    def popen_options(self) -> dict:
        """Extra keyword arguments for subprocess.Popen/run (e.g. hide console windows)."""
        return {}

    @abstractmethod
    def kill_process_tree(self, process: subprocess.Popen) -> None:
        """Forcefully end a child process and everything it started."""

    @abstractmethod
    def lock_exclusive(self, handle: BinaryIO) -> None:
        """Take a non-blocking exclusive lock on an open file.

        Raise ``DataFolderInUse`` when another process holds it.
        """

    @abstractmethod
    def pick_folder(self, prompt: str) -> str | None:
        """Show the native folder picker. Return None when the user cancels.

        Raise ``FolderPickerUnavailable`` when no picker can be shown.
        """

    def bind_listener(self, sock: socket.socket, address) -> None:
        """Bind the service's listening socket so it fails if another program already listens there."""
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # POSIX: only skips TIME_WAIT
        sock.bind(address)

    def same_folder(self, a: Path, b: Path) -> bool:
        """Whether two existing paths name the same folder on this OS."""
        try:
            return a.samefile(b)
        except OSError:
            return False

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label}
