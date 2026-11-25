"""The provider seam.

A provider is one agent harness installed on the user's machine (Codex,
Claude Code, Antigravity, and later Copilot or others). The room never knows
which provider it is talking to: it builds a ``TurnRequest`` plus ``TurnContext``
and gets a ``TurnResult`` back.

To add a provider: subclass ``Provider``, implement ``run_turn`` and
``list_models``, declare ``id``/``label``/``hints``/``install_url`` (and optionally
``logo``), and add it to ``commonroom.providers.registry.BUILTIN_PROVIDERS``.
Nothing else changes.
"""
from __future__ import annotations

import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Optional

from ..platform.base import ExecutableHints, Platform

#: Folder holding the providers' logo files (see ``Provider.logo``).
LOGOS = Path(__file__).resolve().parent / "logos"


class ProviderNotInstalled(RuntimeError):
    """The provider's command-line tool was not found on this machine."""


class SessionLost(RuntimeError):
    """The provider no longer has the saved session; the room should start a new one."""


@dataclass
class Model:
    id: str
    label: str
    efforts: list[str] = field(default_factory=list)
    default_effort: Optional[str] = None
    description: Optional[str] = None
    resolved_model: Optional[str] = None
    effort_labels: Optional[dict[str, str]] = None

    def to_dict(self) -> dict:
        data = {"id": self.id, "label": self.label or self.id, "efforts": list(dict.fromkeys(self.efforts))}
        if self.description:
            data["description"] = self.description
        if self.default_effort in data["efforts"]:
            data["defaultEffort"] = self.default_effort
        if self.resolved_model:
            data["resolvedModel"] = self.resolved_model
        if self.effort_labels:
            data["effortLabels"] = dict(self.effort_labels)
        return data


@dataclass
class Detection:
    """What the setup screen shows about one provider on this machine."""
    id: str
    label: str
    installed: bool
    install_url: str
    path: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "installed": self.installed, "installUrl": self.install_url,
                "path": self.path, "version": self.version, "error": self.error}


#: ask(title, detail, choices, withdrawn) -> chosen id (or typed text).
#: choices: [{"id", "label", "multi"?: bool, "input"?: bool}]. ``withdrawn`` is an
#: optional threading.Event the provider sets when the harness cancels the question.
AskFn = Callable[[str, str, list, Optional[threading.Event]], str]


@dataclass(frozen=True)
class TurnRequest:
    """What to run: one member's turn, fully described as data."""
    member_id: str
    model: str
    effort: str
    prompt: str
    workspace: Path
    session: str = ""                # provider session to resume, "" for a new one
    session_workspace: str = ""      # folder the saved session was started in


@dataclass
class TurnContext:
    """How the provider talks back to the room while a turn runs."""
    cancel: threading.Event
    activity: Callable[[str], None]
    remember_session: Callable[[str], None]
    ask: AskFn


@dataclass(frozen=True)
class TurnResult:
    text: str
    session: str


class Provider(ABC):
    id: ClassVar[str]
    label: ClassVar[str]
    hints: ClassVar[ExecutableHints]
    install_url: ClassVar[str]
    #: Arguments that print a version string, or None when the tool has none.
    version_args: ClassVar[Optional[tuple[str, ...]]] = ("--version",)
    #: File name of the provider's logo, an SVG in ``providers/logos/``. None (or a missing
    #: file) makes the UI draw a neutral initial instead. Use ``fill="currentColor"`` for a
    #: one-colour mark so it follows the light/dark theme.
    logo: ClassVar[Optional[str]] = None

    def __init__(self, platform: Platform):
        self.platform = platform

    def executable(self) -> str:
        found = self.platform.find_executable(self.hints)
        if not found:
            raise ProviderNotInstalled(
                f"{self.label} is not installed or not on PATH. Install it from {self.install_url}")
        return found

    def logo_file(self) -> Optional[Path]:
        """The provider's logo file, or None when it declares none or the file is missing."""
        name = self.logo
        if not isinstance(name, str) or not name.endswith(".svg") or Path(name).name != name or "\\" in name:
            return None
        path = LOGOS / name
        return path if path.is_file() else None

    def detect(self) -> Detection:
        path = self.platform.find_executable(self.hints)
        if not path:
            return Detection(self.id, self.label, False, self.install_url)
        version = None
        if self.version_args is not None:
            try:
                done = subprocess.run([path, *self.version_args], capture_output=True, encoding="utf-8",
                                      errors="replace", timeout=10, stdin=subprocess.DEVNULL,
                                      env=self.platform.cli_environment(), **self.platform.popen_options())
                lines = (done.stdout or done.stderr or "").strip().splitlines()
                version = lines[0].strip() if lines else None
            except (OSError, subprocess.SubprocessError) as exc:
                return Detection(self.id, self.label, True, self.install_url, path, None, str(exc))
        return Detection(self.id, self.label, True, self.install_url, path, version)

    @abstractmethod
    def run_turn(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        """Run one turn to completion. Raise SessionLost if ``request.session`` is gone."""

    @abstractmethod
    def list_models(self, workspace: Path, cancel: threading.Event) -> list[Model]:
        """Ask the installed tool which models (and effort levels) it offers."""
