"""Test doubles: a platform that never touches the real machine, and turn helpers."""
import os
import threading
from pathlib import Path
from unittest.mock import Mock

from commonroom.platform.base import Platform
from commonroom.providers.base import TurnContext, TurnRequest


class FakePlatform(Platform):
    name = "fake"
    label = "Fake OS"

    def __init__(self, executables=None):
        self.executables = executables or {}

    def data_dir(self):
        raise AssertionError("Tests must not use the real data folder.")

    def find_executable(self, hints):
        return next((self.executables[c] for c in hints.commands if c in self.executables), None)

    def kill_process_tree(self, process):
        process.kill()

    def lock_exclusive(self, handle):
        raise NotImplementedError

    def pick_folder(self, prompt):
        return None

    def same_folder(self, a, b):
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def turn(prompt="Start", workspace=None, **fields):
    return TurnRequest(member_id=fields.pop("member_id", "member"), model=fields.pop("model", ""),
                       effort=fields.pop("effort", ""), prompt=prompt, workspace=workspace or Path.cwd(), **fields)


def context(ask=None, cancel=None):
    return TurnContext(cancel=cancel or threading.Event(), activity=Mock(), remember_session=Mock(),
                       ask=ask or Mock(return_value=None))
