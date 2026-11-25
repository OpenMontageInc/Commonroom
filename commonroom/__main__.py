"""Run the Commonroom service: ``python -m commonroom``."""
import sys

# Checked before importing the rest of Commonroom, which needs 3.10. macOS's own /usr/bin/python3 is often 3.9.
if sys.version_info < (3, 10):
    sys.exit(f"Commonroom needs Python 3.10 or newer, but {sys.executable} is {sys.version.split()[0]}. "
             "Install one from https://www.python.org/downloads/ (macOS: brew install python@3.12, "
             "then run python3.12 -m commonroom; Windows: winget install -e --id Python.Python.3.12).")

import argparse
import errno
import json
import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

from .app import build_app, own_data_folder
from .platform import DataFolderInUse, current_platform
from .providers.rpc import end_all_processes
from .server import make_server

# Windows reports a taken port as WSAEADDRINUSE, or WSAEACCES when the owner holds it exclusively.
_PORT_TAKEN_ERRNOS = {errno.EADDRINUSE, errno.EACCES}
_PORT_TAKEN_WINERRORS = {10048, 10013}


class PortInUse(RuntimeError):
    """The requested port already has a listener."""


def _is_commonroom(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            return json.loads(response.read()).get("service") == "commonroom"
    except Exception:
        return False


def port_in_use_message(port):
    if _is_commonroom(port):
        return f"Commonroom is already running at http://127.0.0.1:{port}/."
    return (f"Port {port} is already used by another program. "
            f"Close it or start Commonroom with --port <other>.")


def bind(port, platform):
    """Listen on 127.0.0.1:<port>; raise PortInUse (with a one-line message) when it is taken."""
    try:
        return make_server(None, port, platform=platform)
    except OSError as exc:
        if exc.errno in _PORT_TAKEN_ERRNOS or getattr(exc, "winerror", None) in _PORT_TAKEN_WINERRORS:
            raise PortInUse(port_in_use_message(port)) from exc
        raise


@contextmanager
def stop_on_hangup_or_terminate():
    """Treat SIGTERM and SIGHUP (a closed Terminal window) like Ctrl-C, so cleanup runs."""
    def interrupt(*_args):
        raise KeyboardInterrupt
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                previous[name] = signal.signal(getattr(signal, name), interrupt)
    try:
        yield
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="commonroom", description="A local group chat for you and your agents.")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--data", type=Path, help="data folder (default: the platform's application data folder)")
    parser.add_argument("--initial-project", type=Path, help="register this folder as the default project")
    options = parser.parse_args(argv)
    platform = current_platform()
    data = options.data or platform.data_dir()
    try:
        with stop_on_hangup_or_terminate(), own_data_folder(data, platform):
            # Bind before opening any room, so a taken port never starts agent turns that then die.
            with bind(options.port, platform) as server:
                app = build_app(data, options.initial_project, platform)
                server.app = app
                app.start()
                if sys.stdout:
                    print(f"Commonroom is running at http://127.0.0.1:{server.server_port}/", flush=True)
                server.serve_forever()
    except (DataFolderInUse, PortInUse, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        pass
    finally:
        # Agents run in their own process groups (macOS), so they would not see the service stop.
        end_all_processes()


if __name__ == "__main__":
    main()
