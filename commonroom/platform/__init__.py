"""Pick the operating-system implementation Commonroom runs on."""
from __future__ import annotations

import sys

from .base import DataFolderInUse, ExecutableHints, FolderPickerUnavailable, Platform

__all__ = ["DataFolderInUse", "ExecutableHints", "FolderPickerUnavailable", "Platform", "current_platform"]


def current_platform() -> Platform:
    if sys.platform == "win32":
        from .windows import WindowsPlatform
        return WindowsPlatform()
    if sys.platform == "darwin":
        from .macos import MacPlatform
        return MacPlatform()
    raise RuntimeError(f"Commonroom does not support this operating system ({sys.platform}) yet. "
                       "Windows and macOS are supported; adding another OS is one new file in commonroom/platform/.")
