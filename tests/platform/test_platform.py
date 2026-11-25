import errno
import os
from contextlib import ExitStack
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
        home = Path("/Users/tester")  # no drive letter, so a ":"-separated PATH splits cleanly on any OS
        with patch("commonroom.platform.macos.Path.home", return_value=home), patch.dict(os.environ, {"PATH": "/usr/bin"}), \
                patch.object(mac, "login_path", return_value=["/Users/x/.nvm/versions/node/v22/bin", "/usr/bin"]):
            self.assertEqual(mac.data_dir(), home / "Library" / "Application Support" / "Commonroom")
            path = mac.cli_environment()["PATH"]
            parts = path.split(":")
            # Inherited PATH first, then the login shell's, then the usual install folders, without repeats.
            self.assertEqual(parts[:3], ["/usr/bin", "/Users/x/.nvm/versions/node/v22/bin", str(home / ".local" / "bin")])
            self.assertEqual(parts[-2:], ["/opt/homebrew/bin", "/usr/local/bin"])
            self.assertEqual(len(parts), len(set(parts)))
            self.assertIn(str(home / ".volta" / "bin"), parts)
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
            self.assertIsNone(MacPlatform().find_executable(
                ExecutableHints(commands=("missing",), paths={"macos": ("{home}/missing",)})))

    def test_finds_codex_bundled_with_the_chatgpt_app(self):
        codex = self.home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex-cli" / "bin" / "codex"
        codex.parent.mkdir(parents=True)
        codex.touch()
        self.assertTrue(any(path.startswith("/Applications/ChatGPT.app/") for path in CodexProvider.hints.paths["macos"]))
        with patch("commonroom.platform.macos.Path.home", return_value=self.home), \
                patch("commonroom.platform.macos.shutil.which", return_value=None), \
                patch("commonroom.platform.macos.os.access", return_value=True), \
                patch.object(Path, "is_file", lambda candidate: candidate == codex):  # ignore a real /Applications
            self.assertEqual(MacPlatform().find_executable(CodexProvider.hints), str(codex))

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
                   side_effect=lambda name, **_: f"C:/bin/{name}" if name == "agy.exe" else None) as which:
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

    def isolated_windows_folders(self):
        """Point every Windows install folder at an empty temp folder, so a real copy on this machine
        (e.g. %LOCALAPPDATA%\\OpenAI\\Codex) never leaks into the test."""
        empty = self.home / "isolated"
        empty.mkdir(exist_ok=True)
        folders = {name: str(empty / name) for name in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramData")}
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, folders))
        stack.enter_context(patch("commonroom.platform.windows.Path.home", return_value=empty))
        return stack

    def test_newest_declared_copy_wins_over_an_older_leftover(self):
        local_data = self.home / "AppData" / "Local"
        stale = local_data / "OpenAI" / "Codex" / "bin" / "codex.exe"
        current = local_data / "OpenAI" / "Codex" / "bin" / "f71a721e" / "codex.exe"
        for binary, modified in ((stale, 1_000_000), (current, 2_000_000)):
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.touch()
            os.utime(binary, (modified, modified))
        windows = WindowsPlatform()
        windows._store_locations["OpenAI.Codex"] = None
        with self.isolated_windows_folders(), patch.dict(os.environ, {"LOCALAPPDATA": str(local_data)}), \
                patch("commonroom.platform.windows.shutil.which", return_value=None):
            self.assertEqual(windows.find_executable(CodexProvider.hints), str(current))

    def test_store_package_lookup_runs_once_and_hides_the_console(self):
        package = self.home / "OpenAI.Codex_1.0"
        codex = package / "app" / "resources" / "codex.exe"
        codex.parent.mkdir(parents=True)
        codex.touch()
        windows = WindowsPlatform()
        with self.isolated_windows_folders(), patch("commonroom.platform.windows.shutil.which", return_value=None), \
                patch("commonroom.platform.windows.subprocess.run",
                      return_value=SimpleNamespace(returncode=0, stdout=f"{package}\r\n", stderr="")) as run:
            self.assertEqual(windows.find_executable(CodexProvider.hints), str(codex))
            self.assertEqual(windows.find_executable(CodexProvider.hints), str(codex))
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertEqual(run.call_args.kwargs["env"]["COMMONROOM_APPX_PACKAGE"], "OpenAI.Codex")

    def test_store_lookup_failure_means_not_installed(self):
        windows = WindowsPlatform()
        with self.isolated_windows_folders(), patch("commonroom.platform.windows.shutil.which", return_value=None), \
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
        with patch("commonroom.platform.windows.shutil.which", side_effect=lambda name, **_: found.get(name)):
            self.assertEqual(WindowsPlatform().find_executable(ExecutableHints(("claude",))), found["claude.exe"])
        with patch("commonroom.platform.windows.shutil.which", side_effect=lambda name, **_: {"claude": found["claude"]}.get(name)):
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


class WindowsSearchPathTests(Folder):
    def setUp(self):
        super().setUp()
        self.env = {"PATH": r"C:\Windows\system32", "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
                    "APPDATA": r"C:\Users\me\AppData\Roaming", "ProgramFiles": r"C:\Program Files",
                    "ProgramData": r"C:\ProgramData", "SCOOP": r"D:\scoop",
                    # Path.home() reads these on Windows and HOME elsewhere; clearing them breaks it.
                    "USERPROFILE": str(self.home), "HOME": str(self.home)}

    def search(self, saved=()):
        windows = WindowsPlatform()
        with patch.dict(os.environ, self.env, clear=True), \
                patch("commonroom.platform.windows._registry_path", return_value=list(saved)) as registry, \
                patch("commonroom.platform.windows.Path.home", return_value=Path(r"C:\Users\me")):
            return windows, windows._search_path(), registry

    def test_saved_path_and_package_manager_folders_follow_the_inherited_path(self):
        _windows, folders, _registry = self.search([r"C:\Users\me\AppData\Local\Programs\fresh"])
        text = [folder.replace("/", "\\") for folder in folders]
        self.assertEqual(text[0], r"C:\Windows\system32")
        self.assertEqual(text[1], r"C:\Users\me\AppData\Local\Programs\fresh")  # installed after we started
        for expected in (r"AppData\Roaming\npm", r"Microsoft\WinGet\Links", r"D:\scoop\shims",
                         r"chocolatey\bin", r"Users\me\.local\bin"):
            self.assertTrue(any(expected in folder for folder in text), expected)

    def test_saved_path_is_cached_until_refresh(self):
        windows = WindowsPlatform()
        with patch.dict(os.environ, self.env, clear=True), \
                patch("commonroom.platform.windows._registry_path", return_value=[]) as registry:
            windows._search_path()
            windows._search_path()
            registry.assert_called_once()
            windows.refresh()
            windows._search_path()
            self.assertEqual(registry.call_count, 2)

    def test_claude_skips_the_desktop_app_alias_folder(self):
        aliases = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps"
        self.env["PATH"] = aliases + ";" + r"C:\tools"
        windows = WindowsPlatform()
        windows._store_locations["OpenAI.Codex"] = None
        with patch.dict(os.environ, self.env, clear=True), \
                patch("commonroom.platform.windows._registry_path", return_value=[]), \
                patch("commonroom.platform.windows.shutil.which", return_value=None) as which:
            windows.find_executable(ClaudeProvider.hints)
            claude_search = which.call_args.kwargs["path"].split(";")
            windows.find_executable(CodexProvider.hints)
            codex_search = which.call_args.kwargs["path"].split(";")
        self.assertNotIn(aliases, claude_search)
        self.assertIn(r"C:\tools", claude_search)
        self.assertIn(aliases, codex_search)  # other tools may use app aliases

    def test_children_get_the_same_search_path(self):
        windows = WindowsPlatform()
        with patch.dict(os.environ, self.env, clear=True), \
                patch("commonroom.platform.windows._registry_path", return_value=[r"C:\nodejs"]):
            self.assertIn(r"C:\nodejs", windows.cli_environment()["PATH"].split(";"))


class DeclaredPathTests(Folder):
    def test_wildcard_picks_the_newest_copy(self):
        from commonroom.platform.base import declared_file
        old, new = (self.home / "bin" / name / "codex.exe" for name in ("aaa", "bbb"))
        for index, binary in enumerate((new, old)):
            binary.parent.mkdir(parents=True)
            binary.touch()
            os.utime(binary, (1_000_000 + index * 10, 1_000_000 + index * 10))
        os.utime(new, (2_000_000, 2_000_000))
        self.assertEqual(declared_file("{home}/bin/*/codex.exe", Path.is_file, home=str(self.home)), new)
        self.assertIsNone(declared_file("{home}/none/*/codex.exe", Path.is_file, home=str(self.home)))
        self.assertIsNone(declared_file("{localappdata}/x.exe", Path.is_file, home=str(self.home)))

    def test_environment_override_wins_over_path(self):
        chosen = self.home / "my codex"
        chosen.touch()
        with patch.dict(os.environ, {"CODEX_CLI_PATH": f'"{chosen}"'}), \
                patch("commonroom.platform.macos.os.access", return_value=True), \
                patch("commonroom.platform.macos.shutil.which", return_value="/usr/local/bin/codex"):
            self.assertEqual(MacPlatform().find_executable(CodexProvider.hints), str(chosen))
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.home / "missing")}), \
                patch("commonroom.platform.macos.MacPlatform.cli_environment", return_value={"PATH": ""}), \
                patch("commonroom.platform.macos.shutil.which", return_value="/usr/local/bin/codex"):
            self.assertEqual(MacPlatform().find_executable(CodexProvider.hints), "/usr/local/bin/codex")

    def test_windows_prefers_the_store_apps_runnable_copy(self):
        local = self.home / "Local"
        copy = local / "OpenAI" / "Codex" / "bin" / "f00d" / "codex.exe"
        copy.parent.mkdir(parents=True)
        copy.touch()
        windows = WindowsPlatform()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local), "PATH": ""}), \
                patch("commonroom.platform.windows._registry_path", return_value=[]), \
                patch("commonroom.platform.windows.Path.home", return_value=self.home), \
                patch("commonroom.platform.windows.shutil.which", return_value=None), \
                patch.object(windows, "_store_location") as store:
            self.assertEqual(windows.find_executable(CodexProvider.hints), str(copy))
        store.assert_not_called()  # the WindowsApps copy is only a last resort


class ExecutableHintsTests(unittest.TestCase):
    def test_builtin_hints_are_declared_as_data(self):
        self.assertEqual(CodexProvider.hints.store_packages["windows"], (("OpenAI.Codex", "app/resources/codex.exe"),))
        self.assertIn("{home}/.local/bin/claude.exe", ClaudeProvider.hints.paths["windows"])
        self.assertIn("{home}/.local/bin/claude", ClaudeProvider.hints.paths["macos"])
        self.assertEqual(ClaudeProvider.hints.exclude["windows"], ("{localappdata}/Microsoft/WindowsApps",))
        self.assertEqual(AntigravityProvider.hints.commands, ("agy",))


if __name__ == "__main__":
    unittest.main()


class MacLoginPathTests(Folder):
    def test_reads_path_between_markers_and_caches_until_refresh(self):
        mac = MacPlatform()
        noisy = "Welcome!\n__COMMONROOM_PATH__/a/bin:/b/bin:relative__COMMONROOM_PATH__"
        with patch("commonroom.platform.macos.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=noisy, stderr="")) as run:
            self.assertEqual(mac.login_path(), ["/a/bin", "/b/bin"])
            self.assertEqual(mac.login_path(), ["/a/bin", "/b/bin"])
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertIn("-ilc", run.call_args.args[0])
            mac.refresh()
            mac.login_path()
            self.assertEqual(run.call_count, 2)

    def test_a_broken_or_slow_shell_adds_nothing(self):
        for failure in (subprocess.TimeoutExpired("zsh", 8), OSError("no shell")):
            mac = MacPlatform()
            with patch("commonroom.platform.macos.subprocess.run", side_effect=failure):
                self.assertEqual(mac.login_path(), [])
        mac = MacPlatform()
        with patch("commonroom.platform.macos.subprocess.run",
                   return_value=SimpleNamespace(returncode=1, stdout="zsh: error", stderr="")):
            self.assertEqual(mac.login_path(), [])


class MacLifecycleTests(Folder):
    def test_kill_reaches_the_group_after_its_leader_was_reaped(self):
        process = Mock(pid=42)
        with patch("commonroom.platform.macos.os.getpgid", create=True, side_effect=ProcessLookupError), \
                patch("commonroom.platform.macos.os.getpgrp", create=True, return_value=1), \
                patch("commonroom.platform.macos.os.killpg", create=True) as killpg:
            MacPlatform().kill_process_tree(process)
        self.assertEqual(killpg.call_args.args[0], 42)
        process.terminate.assert_not_called()

    def test_bind_refuses_a_port_another_program_answers_on(self):
        sock = Mock()
        with patch("commonroom.platform.macos.socket.create_connection") as connect:
            with self.assertRaises(OSError) as raised:
                MacPlatform().bind_listener(sock, ("127.0.0.1", 8742))
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
        connect.assert_called_once()
        sock.bind.assert_not_called()
        with patch("commonroom.platform.macos.socket.create_connection", side_effect=ConnectionRefusedError):
            MacPlatform().bind_listener(sock, ("127.0.0.1", 8742))
        sock.bind.assert_called_once_with(("127.0.0.1", 8742))
        with patch("commonroom.platform.macos.socket.create_connection") as connect:
            MacPlatform().bind_listener(Mock(), ("127.0.0.1", 0))  # port 0: the OS picks a free one
        connect.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS process groups")
    def test_leaving_a_harness_never_waits_for_processes_it_left_behind(self):
        import time
        from commonroom.providers.rpc import RpcProcess
        started = time.monotonic()
        with RpcProcess(MacPlatform(), ["/bin/sh", "-c", "sleep 30 & read line"], self.home) as rpc:
            pass
        self.assertLess(time.monotonic() - started, 6)
        MacPlatform().kill_process_tree(rpc.proc)  # end the leftover sleep

    def test_stopping_the_service_ends_running_harnesses(self):
        from commonroom.providers import rpc as rpc_module
        platform = Mock()
        running, finished = Mock(), Mock()
        running.poll.return_value, finished.poll.return_value = None, 0
        harnesses = [SimpleNamespace(platform=platform, proc=running), SimpleNamespace(platform=platform, proc=finished)]
        with patch.object(rpc_module, "_live", harnesses):
            rpc_module.end_all_processes()
        platform.kill_process_tree.assert_called_once_with(running)
