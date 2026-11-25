"""Windows specifics: %LOCALAPPDATA%, .exe lookup, Store apps, hidden consoles, taskkill."""
from __future__ import annotations

import errno
import ntpath
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .base import DataFolderInUse, ExecutableHints, FolderPickerUnavailable, Platform, declared_file, env_file, expand_hint

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_PICK_FOLDER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$owner = [System.Windows.Forms.Form]::new()
$dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
try {
    $owner.Text = 'Commonroom folder picker'
    $owner.TopMost = $true
    $owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $owner.Size = [System.Drawing.Size]::new(330, 170)
    $owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $owner.MaximizeBox = $false
    $owner.MinimizeBox = $false
    $owner.Show()
    $owner.Activate()
    $dialog.Description = $env:COMMONROOM_PICKER_PROMPT
    $dialog.SelectedPath = [Environment]::GetFolderPath('MyDocuments')
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::WriteLine($dialog.SelectedPath)
    }
} finally {
    $dialog.Dispose()
    $owner.Dispose()
}
"""

_STORE_LOCATION = ("Get-AppxPackage -Name $env:COMMONROOM_APPX_PACKAGE -ErrorAction SilentlyContinue | "
                   "Sort-Object Version -Descending | Select-Object -First 1 -ExpandProperty InstallLocation")


# Where package managers put command-line tools. Checked after PATH, because PATH in a long-running
# process (or one started from Explorer before an install) can be stale or missing these folders.
_TOOL_FOLDERS = (
    "{home}/.local/bin",                       # official Claude Code installer
    "{appdata}/npm",                           # npm -g with the default prefix
    "{localappdata}/Microsoft/WinGet/Links",   # winget portable packages (per user)
    "{programfiles}/WinGet/Links",             # winget portable packages (machine wide)
    "{home}/scoop/shims",
    "{programdata}/chocolatey/bin",
    "{localappdata}/Volta/bin",
    "{programfiles}/Volta",
    "{programfiles}/nodejs",                   # nvm-windows' default symlink, and the Node.js installer
    "{localappdata}/pnpm",
    "{home}/.bun/bin",
)
_PATH_TTL = 60  # seconds; a rescan also clears it
_PATHSEP = ";"  # spelled out so tests can exercise this module on any OS


def _registry_path() -> list[str]:
    """The user's and the machine's PATH as currently saved, which installers update after we started."""
    try:
        import winreg
    except ImportError:
        return []
    paths: list[str] = []
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        paths.extend(os.path.expandvars(part).strip() for part in str(value).split(_PATHSEP))
    return [path for path in paths if path]


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _expand(template: str, folders: dict[str, str]) -> Path | None:
    return expand_hint(template, **folders)


class WindowsPlatform(Platform):
    name = "windows"
    label = "Windows"

    def __init__(self):
        self._store_locations: dict[str, str | None] = {}
        self._store_lock = threading.Lock()
        self._saved_path: tuple[float, list[str]] | None = None

    def data_dir(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Commonroom"

    def popen_options(self) -> dict:
        return {"creationflags": CREATE_NO_WINDOW}

    def refresh(self) -> None:
        with self._store_lock:
            self._store_locations.clear()
            self._saved_path = None

    def _folders(self) -> dict[str, str]:
        return {"home": str(Path.home()), "localappdata": os.environ.get("LOCALAPPDATA") or "",
                "appdata": os.environ.get("APPDATA") or "", "programfiles": os.environ.get("ProgramFiles") or "",
                "programdata": os.environ.get("ProgramData") or ""}

    def _search_path(self) -> list[str]:
        with self._store_lock:
            if not self._saved_path or time.monotonic() - self._saved_path[0] >= _PATH_TTL:
                self._saved_path = (time.monotonic(), _registry_path())
            saved = list(self._saved_path[1])
        folders = self._folders()
        extra = [str(path) for template in _TOOL_FOLDERS if (path := _expand(template, folders))]
        # Install roots people move with environment variables: nvm-windows, Scoop, Chocolatey, Volta.
        custom = [os.environ.get("NVM_SYMLINK") or "",
                  str(Path(os.environ["SCOOP"]) / "shims") if os.environ.get("SCOOP") else "",
                  str(Path(os.environ["ChocolateyInstall"]) / "bin") if os.environ.get("ChocolateyInstall") else "",
                  str(Path(os.environ["VOLTA_HOME"]) / "bin") if os.environ.get("VOLTA_HOME") else ""]
        inherited = os.environ.get("PATH", "").split(_PATHSEP)
        merged = [*inherited, *saved, *custom, *extra]
        return list(dict.fromkeys(path for path in merged if path))

    def cli_environment(self) -> dict[str, str]:
        # Children get the same search path, so an npm .cmd shim can find node.exe even when Node was
        # installed after Commonroom started.
        environment = os.environ.copy()
        environment["PATH"] = _PATHSEP.join(self._search_path())
        return environment

    def find_executable(self, hints: ExecutableHints) -> str | None:
        if chosen := env_file(hints, Path.is_file):
            return str(chosen)
        folders = self._folders()
        excluded = {ntpath.normcase(str(path)) for template in hints.exclude.get(self.name, ())
                    if (path := _expand(template, folders))}
        search = [folder for folder in self._search_path() if ntpath.normcase(folder.rstrip("\\/")) not in excluded]
        path = _PATHSEP.join(search)
        for command in hints.commands:
            # ".exe" first on purpose: a bare name matches any PATHEXT file in the first PATH folder that
            # has one, which is often an npm ".cmd" shim (e.g. ~/bin/claude.cmd ahead of the real
            # claude.exe). A shim runs through cmd.exe, which re-parses arguments and adds a process
            # between us and the tool. When only a shim exists, the bare name still finds it.
            for name in (command + ".exe", command):
                if found := shutil.which(name, path=path):
                    return found
        for template in hints.paths.get(self.name, ()):
            if candidate := declared_file(template, Path.is_file, **folders):
                return str(candidate)
        for package, inner in hints.store_packages.get(self.name, ()):
            location = self._store_location(package)
            if location and (candidate := Path(location) / inner).is_file():
                return str(candidate)
        return None

    def _store_location(self, package: str) -> str | None:
        """InstallLocation of the newest installed Store package, looked up once per process."""
        with self._store_lock:
            if package not in self._store_locations:
                try:
                    done = subprocess.run([_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                                           _STORE_LOCATION], capture_output=True, encoding="utf-8", errors="replace",
                                          timeout=15, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                                          env={**os.environ, "COMMONROOM_APPX_PACKAGE": package})
                    location = (done.stdout or "").strip().splitlines()
                    self._store_locations[package] = location[0].strip() if done.returncode == 0 and location else None
                except (OSError, subprocess.SubprocessError):
                    self._store_locations[package] = None
            return self._store_locations[package]

    def kill_process_tree(self, process: subprocess.Popen) -> None:
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                           timeout=10, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def lock_exclusive(self, handle: BinaryIO) -> None:
        import msvcrt
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise DataFolderInUse(f"Another Commonroom process is using {Path(handle.name).parent}.") from exc

    def pick_folder(self, prompt: str) -> str | None:
        unavailable = "Windows could not open the folder picker. Enter the folder path instead."
        try:
            result = subprocess.run([_powershell(), "-NoLogo", "-NoProfile", "-STA", "-Command", _PICK_FOLDER],
                                    stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                                    creationflags=CREATE_NO_WINDOW,
                                    env={**os.environ, "COMMONROOM_PICKER_PROMPT": prompt})
        except (OSError, UnicodeError) as exc:
            raise FolderPickerUnavailable(unavailable) from exc
        if result.returncode != 0:
            raise FolderPickerUnavailable(unavailable)
        return result.stdout.rstrip("\r\n") or None

    def bind_listener(self, sock: socket.socket, address) -> None:
        # On Windows SO_REUSEADDR lets a second socket bind a port that is already listening, so two
        # services could silently share one port. Exclusive use makes a taken port fail to bind.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(address)

    def same_folder(self, a: Path, b: Path) -> bool:
        try:
            return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
        except OSError:
            return False
