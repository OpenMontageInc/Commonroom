"""Windows specifics: %LOCALAPPDATA%, .exe lookup, Store apps, hidden consoles, taskkill."""
from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO

from .base import DataFolderInUse, ExecutableHints, FolderPickerUnavailable, Platform

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


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


class WindowsPlatform(Platform):
    name = "windows"
    label = "Windows"

    def __init__(self):
        self._store_locations: dict[str, str | None] = {}
        self._store_lock = threading.Lock()

    def data_dir(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Commonroom"

    def popen_options(self) -> dict:
        return {"creationflags": CREATE_NO_WINDOW}

    def find_executable(self, hints: ExecutableHints) -> str | None:
        for command in hints.commands:
            # ".exe" first on purpose: a bare name matches any PATHEXT file in the first PATH folder that
            # has one, which is often an npm ".cmd" shim (e.g. ~/bin/claude.cmd ahead of the real
            # claude.exe). A shim runs through cmd.exe, which re-parses arguments and adds a process
            # between us and the tool. When only a shim exists, the bare name still finds it.
            for name in (command + ".exe", command):
                if found := shutil.which(name):
                    return found
        local = os.environ.get("LOCALAPPDATA") or ""
        for template in hints.paths.get(self.name, ()):
            if "{localappdata}" in template and not local:
                continue
            candidate = Path(template.format(home=Path.home(), localappdata=local))
            if candidate.is_file():
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
