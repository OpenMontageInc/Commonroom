"""Bidirectional JSON-lines transport shared by the locally installed agent harnesses."""
from __future__ import annotations

import collections
import json
import queue
import subprocess
import threading
import time
import weakref

from ..platform.base import Platform
from .base import SessionLost

#: Error text harnesses use when a saved session no longer exists. The one list every provider uses.
LOST_SESSION_MARKERS = ("no conversation found", "conversation not found", "session not found", "unknown session",
                        "no such session", "thread not found", "no rollout found")


def raise_if_session_lost(exc: BaseException, session: str, cancel) -> None:
    """Turn a harness error about a missing saved session into ``SessionLost``."""
    if isinstance(exc, SessionLost):
        raise exc
    if session and not (cancel is not None and cancel.is_set()) and \
            any(marker in str(exc).lower() for marker in LOST_SESSION_MARKERS):
        raise SessionLost(str(exc)) from exc


class RpcError(RuntimeError):
    def __init__(self, method, error):
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(f"{method}: {error.get('message', error)}")


_live: "weakref.WeakSet[RpcProcess]" = weakref.WeakSet()
_live_lock = threading.Lock()


def end_all_processes() -> None:
    """Kill every harness still running, so stopping the service never leaves agents working unattended."""
    with _live_lock:
        running = list(_live)
    for rpc in running:
        proc = getattr(rpc, "proc", None)
        if proc is not None and proc.poll() is None:
            rpc.platform.kill_process_tree(proc)


class RpcProcess:
    def __init__(self, platform: Platform, argv, workspace, cancel=None):
        self.platform = platform
        self.argv = argv
        self.workspace = workspace
        self.cancel = cancel
        self.on_cancel = None
        self.writer = threading.Lock()
        self.queue = queue.Queue()
        self.stderr = collections.deque(maxlen=12)
        self.serial = 0

    def __enter__(self):
        self.proc = subprocess.Popen(self.argv, cwd=self.workspace, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     encoding="utf-8", errors="replace", bufsize=1,
                                     env=self.platform.cli_environment(), **self.platform.popen_options())

        def reader():
            try:
                for line in self.proc.stdout:
                    if line.strip():
                        self.queue.put(line)
            finally:
                self.queue.put(None)

        def errors():
            for line in self.proc.stderr:
                self.stderr.append(line.rstrip())

        self.readers = [threading.Thread(target=fn, daemon=True) for fn in (reader, errors)]
        for thread in self.readers:
            thread.start()
        with _live_lock:
            _live.add(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.platform.kill_process_tree(self.proc)
            self.proc.wait()
        with _live_lock:
            _live.discard(self)
        # A process the harness started can outlive it while still holding these pipes (for example a
        # dev server). Closing a pipe whose reader is still blocked on it would wait until that process
        # exits, so such a pipe is left to its daemon reader, which ends when the pipe does.
        for thread, pipe in zip(self.readers, (self.proc.stdout, self.proc.stderr)):
            thread.join(timeout=1)
            if not thread.is_alive():
                pipe.close()

    def wait_for(self, predicate, timeout=2):
        """Drain protocol events during shutdown until the harness acknowledges."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.queue.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if raw is None:
                return None
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if predicate(event):
                return event
        return None

    def read(self):
        while True:
            if self.cancel and self.cancel.is_set():
                callback, self.on_cancel = self.on_cancel, None
                if callback:
                    try:
                        interrupt_id = callback()
                        if interrupt_id is not None:
                            self.wait_for(lambda ev: ev.get("id") == interrupt_id and "method" not in ev)
                    except OSError:
                        pass
                raise RuntimeError("Turn stopped.")
            try:
                line = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError("Harness closed the connection. " + "\n".join(self.stderr))
            try:
                return json.loads(line)
            except ValueError:
                self.stderr.append(line.rstrip())

    def send(self, obj):
        with self.writer:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def handle(self, event, on_event, on_request):
        if "method" not in event:
            return
        method, params = event["method"], event.get("params", {})
        if "id" not in event:
            on_event(method, params)
            return
        try:
            result = on_request(method, params)
            response = {"result": result}
        except Exception as exc:
            response = {"error": {"code": -32603, "message": str(exc)}}
        self.send({"jsonrpc": "2.0", "id": event["id"], **response})

    def call(self, method, params, on_event, on_request):
        self.serial += 1
        req = self.serial
        self.send({"jsonrpc": "2.0", "id": req, "method": method, "params": params})
        while True:
            event = self.read()
            if event.get("id") == req and "method" not in event:
                if "error" in event:
                    raise RpcError(method, event["error"])
                return event.get("result")
            self.handle(event, on_event, on_request)

    def pump_until(self, predicate, on_event, on_request):
        while not predicate():
            self.handle(self.read(), on_event, on_request)


def ignore_event(method, params):
    pass


def unsupported_request(method, params):
    raise RuntimeError(f"Model discovery cannot handle {method}.")
