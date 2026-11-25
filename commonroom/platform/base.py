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
    placeholders ``{home}``, ``{localappdata}``, ``{appdata}``, ``{programfiles}`` and ``{programdata}``
    (Windows folders are empty elsewhere; an entry using an empty one is skipped). ``store_packages`` maps a
    platform name to (package name, path inside the package) pairs for tools
    installed through an OS app store (for example the Windows Codex app).
    """

    commands: tuple[str, ...]
    paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    store_packages: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    #: Per platform, folders (same placeholders as ``paths``) whose match for ``commands`` is never the
    #: tool, e.g. the Windows app alias folder where an old Claude desktop app registered "Claude.exe".
    exclude: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Environment variables a user may set to the tool's full path; checked before anything else.
    env: tuple[str, ...] = ()


def expand_hint(template: str, **folders: str) -> Path | None:
    """Fill a hint's placeholders; None when it uses a folder this OS does not have (empty or missing)."""
    values = {"home": str(Path.home()), "localappdata": "", "appdata": "", "programfiles": "",
              "programdata": "", **folders}
    if any(f"{{{name}}}" in template and not value for name, value in values.items()):
        return None
    return Path(template.format(**values))


def declared_file(template: str, usable, **folders: str) -> Path | None:
    """The file a ``paths`` hint names, or None. A ``*`` in the hint picks the newest usable match,
    for tools that keep each version in its own folder (e.g. ``bin/<hash>/codex.exe``)."""
    path = expand_hint(template, **folders)
    if path is None:
        return None
    if "*" not in str(path):
        return path if usable(path) else None
    parts = path.parts
    first = next(index for index, part in enumerate(parts) if "*" in part)
    anchor = Path(*parts[:first])
    try:
        matches = [match for match in anchor.glob(str(Path(*parts[first:]))) if usable(match)]
        return max(matches, key=lambda match: match.stat().st_mtime, default=None)
    except OSError:
        return None


def env_file(hints: ExecutableHints, usable) -> Path | None:
    """The tool path a user set in one of the hint's environment variables, if it is usable."""
    import os
    for name in hints.env:
        value = os.environ.get(name, "").strip().strip('"')
        if value and usable(Path(value)):
            return Path(value)
    return None


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

    def refresh(self) -> None:
        """Forget cached lookups (PATH from the shell or registry) before a new machine scan."""

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
