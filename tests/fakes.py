"""Fake providers, settings and platform for tests. Nothing here starts a real agent."""
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from commonroom.core.identity import SILENCE
from commonroom.platform.base import DataFolderInUse, FolderPickerUnavailable, Platform
from commonroom.providers.base import Provider, TurnResult

LEAD = {"name": "Chief of Staff", "provider": "codex", "model": "fake-lead", "effort": "medium"}


class FakeSettings:
    def __init__(self, lead=LEAD, display_name="Sam"):
        self.data = {"displayName": display_name, "lead": dict(lead) if lead else None, "setupComplete": True}

    def get(self):
        return json.loads(json.dumps(self.data))


@dataclass
class Turn:
    provider: str
    request: object
    context: object
    messages: list

    @property
    def target(self):
        return self.messages[-1]

    @property
    def prompt(self):
        return self.request.prompt

    @property
    def workspace(self):
        return self.request.workspace

    @property
    def member(self):
        prompt = self.request.prompt
        return dict(id=self.request.member_id, provider=self.provider, model=self.request.model,
                    effort=self.request.effort, session=self.request.session,
                    session_workspace=self.request.session_workspace, _cancel=self.context.cancel,
                    name=re.match(r"Your current display name is (.*?)\. Your stable member ID", prompt)[1],
                    cursor=int(re.search(r"Your last processed room message was (\d+)\.", prompt)[1]))


class FakeHarness:
    """Records every turn; ``respond(turn) -> (text, session)`` decides the outcome."""

    def __init__(self):
        self.calls = []
        self.lock = Lock()
        self.respond = self.silent

    @staticmethod
    def silent(turn):
        return SILENCE, turn.request.session or f"native-{turn.request.member_id}"

    def run(self, provider, request, context):
        messages = [json.loads(line) for line in request.prompt.splitlines() if line.startswith("{")]
        turn = Turn(provider, request, context, messages)
        with self.lock:
            self.calls.append(turn)
        context.activity("Fake harness is processing the delivery")
        text, session = self.respond(turn)
        return TurnResult(text, session)


class FakeProvider(Provider):
    hints = None
    install_url = "https://example.invalid"

    def __init__(self, provider_id, harness):
        super().__init__(None)
        self.id, self.label, self.harness = provider_id, provider_id.title(), harness

    def run_turn(self, request, context):
        return self.harness.run(self.id, request, context)

    def list_models(self, workspace, cancel):
        return []


class FakeProviders:
    def __init__(self, harness, ids=("codex", "claude", "antigravity")):
        self.providers = {pid: FakeProvider(pid, harness) for pid in ids}

    def get(self, provider_id):
        if provider_id not in self.providers:
            raise ValueError(f"Unknown provider: {provider_id}")
        return self.providers[provider_id]

    def ids(self):
        return list(self.providers)

    def __iter__(self):
        return iter(self.providers.values())


class FakePlatform(Platform):
    name, label = "test", "Test OS"

    def __init__(self, picked=None, picker_error=None):
        self.picked, self.picker_error, self.prompts = picked, picker_error, []

    def data_dir(self):
        raise AssertionError("Tests must never use the real data folder.")

    def find_executable(self, hints):
        return None

    def kill_process_tree(self, process):
        pass

    #: path -> open handle; a lock is held while its handle stays open.
    held = {}

    def lock_exclusive(self, handle):
        key = str(Path(handle.name).resolve())
        owner = FakePlatform.held.get(key)
        if owner is not None and owner is not handle and not owner.closed:
            raise DataFolderInUse(f"Another Commonroom process is using {Path(key).parent}.")
        FakePlatform.held[key] = handle

    def pick_folder(self, prompt):
        self.prompts.append(prompt)
        if self.picker_error:
            raise FolderPickerUnavailable(self.picker_error)
        return self.picked


def wait_until_idle(room, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with room.lock:
            if not room.workers:
                return
        time.sleep(0.005)
    raise AssertionError(f"Room workers did not finish: {room.state()['jobs']}")
