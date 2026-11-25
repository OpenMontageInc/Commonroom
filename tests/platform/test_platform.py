import errno
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from commonroom import platform as platforms
from commonroom.platform import DataFolderInUse, ExecutableHints, FolderPickerUnavailable, current_platform
from commonroom.platform.macos import MacPlatform
from commonroom.platform.windows import CREATE_NO_WINDOW, WindowsPlatform
from commonroom.providers.antigravity import AntigravityProvider
from commonroom.providers.claude import ClaudeProvider
from commonroom.providers.codex import CodexProvider


class CurrentPlatformTests(unittest.TestCase):
    def test_selects_by_operating_system(self):
        with patch.object(platforms.sys, "platform", "win32"):
            self.assertIsInstance(current_platform(), WindowsPlatform)
        with patch.object(platforms.sys, "platform", "darwin"):
            self.assertIsInstance(current_platform(), MacPlatform)
        with patch.object(platforms.sys, "platform", "linux"), self.assertRaisesRegex(RuntimeError, "not support"):
            current_platform()


class Folder(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)


class MacTests(Folder):
    def test_application_support_and_gui_launch_paths(self):
        mac = MacPlatform()
        with patch("commonroom.platform.macos.Path.home", return_value=self.home), patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.assertEqual(mac.data_dir(), self.home / "Library" / "Application Support" / "Commonroom")
            path = mac.cli_environment()["PATH"]
            self.assertEqual(path.split(os.pathsep), ["/usr/bin", str(self.home / ".local" / "bin"),
                                                     "/opt/homebrew/bin", "/usr/local/bin"])
            with patch("commonroom.platform.macos.shutil.which",
                       side_effect=lambda command, **_options: f"found/{command}") as which:
                for provider, command in ((CodexProvider, "codex"), (ClaudeProvider, "claude"), (AntigravityProvider, "agy")):
                    self.assertEqual(mac.find_executable(provider.hints), f"found/{command}")
                    which.assert_called_with(command, path=path)

    def test_declared_macos_path_is_a_fallback(self):
        claude = self.home / ".local" / "bin" / "claude"
        claude.parent.mkdir(parents=True)
        claude.touch()
        with patch("commonroom.platform.macos.Path.home", return_value=self.home), \
                patch("commonroom.platform.macos.shutil.which", return_value=None), \
                patch("commonroom.platform.macos.os.access", return_value=True):
            self.assertEqual(MacPlatform().find_executable(ClaudeProvider.hints), str(claude))
            self.assertIsNone(MacPlatform().find_executable(CodexProvider.hints))

    def test_lock_uses_nonblocking_flock(self):
        fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=Mock(side_effect=BlockingIOError(errno.EAGAIN, "busy")))
        with (self.home / "room.lock").open("a+b") as handle, patch.dict("sys.modules", {"fcntl": fcntl}):
            with self.assertRaises(DataFolderInUse):
                MacPlatform().lock_exclusive(handle)
        lock, flags = fcntl.flock.call_args.args
        self.assertEqual(flags, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_picker_can_select_cancel_or_fail(self):
        mac = MacPlatform()
        with patch("commonroom.platform.macos.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="/Users/example/Project folder/\n", stderr="")
            self.assertEqual(mac.pick_folder("Choose a folder"), "/Users/example/Project folder/")
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/osascript")
            self.assertEqual(argv[-1], "Choose a folder")
            run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="execution error: User canceled. (-128)\n")
            self.assertIsNone(mac.pick_folder("Choose a folder"))
            run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="execution error: other\n")
            with self.assertRaises(FolderPickerUnavailable):
                mac.pick_folder("Choose a folder")

    def test_kill_signals_the_child_group_but_never_our_own(self):
        process = Mock(pid=42)
        with patch("commonroom.platform.macos.os.getpgid", create=True, return_value=42), \
                patch("commonroom.platform.macos.os.getpgrp", create=True, return_value=1), \
                patch("commonroom.platform.macos.os.killpg", create=True) as killpg:
            MacPlatform().kill_process_tree(process)
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 42)
        process.terminate.assert_not_called()

        process = Mock(pid=42)
        process.wait.side_effect = [subprocess.TimeoutExpired("x", 2), 0]
        with patch("commonroom.platform.macos.os.getpgid", create=True, return_value=1), \
                patch("commonroom.platform.macos.os.getpgrp", create=True, return_value=1), \
                patch("commonroom.platform.macos.os.killpg", create=True) as killpg:
            MacPlatform().kill_process_tree(process)
        killpg.assert_not_called()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_children_get_their_own_process_group(self):
        self.assertEqual(MacPlatform().popen_options(), {"start_new_session": True})


class WindowsTests(Folder):
    def test_data_dir_prefers_local_app_data(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.home / "Local")}):
            self.assertEqual(WindowsPlatform().data_dir(), self.home / "Local" / "Commonroom")
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}), \
                patch("commonroom.platform.windows.Path.home", return_value=self.home):
            self.assertEqual(WindowsPlatform().data_dir(), self.home / "Commonroom")

    def test_path_lookup_tries_exe_first(self):
        with patch("commonroom.platform.windows.shutil.which",
                   side_effect=lambda name: f"C:/bin/{name}" if name == "agy.exe" else None) as which:
            self.assertEqual(WindowsPlatform().find_executable(AntigravityProvider.hints), "C:/bin/agy.exe")
        self.assertEqual(which.call_args_list[0].args, ("agy.exe",))

    def test_declared_install_folders_are_fallbacks(self):
        local_data = self.home / "AppData" / "Local"
        claude = self.home / ".local" / "bin" / "claude.exe"
        agy = local_data / "Programs" / "agy" / "bin" / "agy.exe"
        for binary in (claude, agy):
            binary.parent.mkdir(parents=True)
            binary.touch()
        windows = WindowsPlatform()
        windows._store_locations["OpenAI.Codex"] = None
        with patch("commonroom.platform.windows.Path.home", return_value=self.home), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(local_data)}), \
                patch("commonroom.platform.windows.shutil.which", return_value=None):
            self.assertEqual(windows.find_executable(ClaudeProvider.hints), str(claude))
            self.assertEqual(windows.find_executable(AntigravityProvider.hints), str(agy))
            self.assertIsNone(windows.find_executable(CodexProvider.hints))

    def test_store_package_lookup_runs_once_and_hides_the_console(self):
        package = self.home / "OpenAI.Codex_1.0"
        codex = package / "app" / "resources" / "codex.exe"
        codex.parent.mkdir(parents=True)
        codex.touch()
        windows = WindowsPlatform()
        with patch("commonroom.platform.windows.shutil.which", return_value=None), \
                patch("commonroom.platform.windows.subprocess.run",
                      return_value=SimpleNamespace(returncode=0, stdout=f"{package}\r\n", stderr="")) as run:
            self.assertEqual(windows.find_executable(CodexProvider.hints), str(codex))
            self.assertEqual(windows.find_executable(CodexProvider.hints), str(codex))
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertEqual(run.call_args.kwargs["env"]["COMMONROOM_APPX_PACKAGE"], "OpenAI.Codex")

    def test_store_lookup_failure_means_not_installed(self):
        windows = WindowsPlatform()
        with patch("commonroom.platform.windows.shutil.which", return_value=None), \
                patch("commonroom.platform.windows.subprocess.run", side_effect=OSError("no powershell")):
            self.assertIsNone(windows.find_executable(CodexProvider.hints))

    def test_popen_hides_console_and_kill_uses_taskkill_tree(self):
        windows = WindowsPlatform()
        self.assertEqual(windows.popen_options(), {"creationflags": CREATE_NO_WINDOW})
        process = Mock(pid=7)
        process.poll.return_value = 0
        with patch("commonroom.platform.windows.subprocess.run") as run:
            windows.kill_process_tree(process)
        self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", "7", "/T", "/F"])
        process.kill.assert_not_called()

    def test_picker_failure_and_cancel(self):
        windows = WindowsPlatform()
        with patch("commonroom.platform.windows.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="C:\\Projects\\One\r\n", stderr="")
            self.assertEqual(windows.pick_folder("Pick one"), "C:\\Projects\\One")
            self.assertEqual(run.call_args.kwargs["env"]["COMMONROOM_PICKER_PROMPT"], "Pick one")
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertIsNone(windows.pick_folder("Pick one"))
            run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(FolderPickerUnavailable):
                windows.pick_folder("Pick one")

    def test_same_folder_ignores_case_and_relative_spelling(self):
        windows = WindowsPlatform()
        (self.home / "Project").mkdir()
        with patch("commonroom.platform.windows.os.path.normcase", side_effect=str.lower):
            self.assertTrue(windows.same_folder(self.home / "PROJECT", self.home / "sub" / ".." / "Project"))
            self.assertFalse(windows.same_folder(self.home / "Project", self.home))

    @unittest.skipUnless(sys.platform == "win32", "msvcrt locking")
    def test_lock_excludes_a_second_owner(self):
        windows = WindowsPlatform()
        with (self.home / "room.lock").open("a+b") as first, (self.home / "room.lock").open("a+b") as second:
            windows.lock_exclusive(first)
            with self.assertRaises(DataFolderInUse):
                windows.lock_exclusive(second)

    def test_real_exe_wins_over_an_earlier_cmd_shim(self):
        # A bare-name lookup would return the npm shim that sits first on PATH.
        found = {"claude": "C:/Users/me/bin/claude.CMD", "claude.exe": "C:/Users/me/.local/bin/claude.exe"}
        with patch("commonroom.platform.windows.shutil.which", side_effect=found.get):
            self.assertEqual(WindowsPlatform().find_executable(ExecutableHints(("claude",))), found["claude.exe"])
        with patch("commonroom.platform.windows.shutil.which", side_effect={"claude": found["claude"]}.get):
            self.assertEqual(WindowsPlatform().find_executable(ExecutableHints(("claude",))), found["claude"])

    @unittest.skipUnless(sys.platform == "win32", "Windows sockets")
    def test_listener_refuses_a_port_another_program_already_listens_on(self):
        import socket
        with socket.socket() as other, socket.socket() as ours:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # what Python's HTTP servers do
            other.bind(("127.0.0.1", 0))
            other.listen()
            with self.assertRaises(OSError):
                WindowsPlatform().bind_listener(ours, other.getsockname())


class ExecutableHintsTests(unittest.TestCase):
    def test_builtin_hints_are_declared_as_data(self):
        self.assertEqual(CodexProvider.hints.store_packages["windows"], (("OpenAI.Codex", "app/resources/codex.exe"),))
        self.assertEqual(ClaudeProvider.hints, ExecutableHints(("claude",), {
            "windows": ("{home}/.local/bin/claude.exe",), "macos": ("{home}/.local/bin/claude",)}))
        self.assertEqual(AntigravityProvider.hints.commands, ("agy",))


if __name__ == "__main__":
    unittest.main()
