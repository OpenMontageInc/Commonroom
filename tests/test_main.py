import contextlib
import io
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from commonroom.__main__ import main
from commonroom.server import make_server
from tests.fakes import FakePlatform


def platform_for_test():
    """The real platform where Commonroom supports the OS; elsewhere (Linux CI) a fake with POSIX socket rules."""
    if sys.platform in ("win32", "darwin"):
        from commonroom.platform import current_platform
        return current_platform()
    return FakePlatform()


class PortInUseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="commonroom-main-")
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name) / "data"

    def run_main(self, port):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stopped,                 patch("commonroom.__main__.current_platform", platform_for_test):
            main(["--port", str(port), "--data", str(self.data)])
        return stopped.exception.code, errors.getvalue()

    def test_port_taken_by_another_program_exits_with_one_clear_line(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            port = other.getsockname()[1]
            code, message = self.run_main(port)
        self.assertEqual(code, 1)
        self.assertEqual(message, f"Port {port} is already used by another program. "
                                  f"Close it or start Commonroom with --port <other>.\n")
        # Nothing was opened: the port check happens before any room is created.
        self.assertFalse((self.data / "projects.sqlite3").exists())

    def test_port_taken_by_commonroom_says_so(self):
        server = make_server(SimpleNamespace(version="0.0.0"), 0)
        listener = threading.Thread(target=server.serve_forever)
        listener.start()
        try:
            code, message = self.run_main(server.server_port)
        finally:
            server.shutdown()
            listener.join()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertEqual(message, f"Commonroom is already running at http://127.0.0.1:{server.server_port}/.\n")


if __name__ == "__main__":
    unittest.main()
