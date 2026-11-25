import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from commonroom.platform.base import ExecutableHints
from commonroom.providers.base import Detection, Model, Provider, TurnResult
from commonroom.providers.catalog import ModelCatalog
from commonroom.providers.registry import BUILTIN_PROVIDERS, ProviderRegistry, build_registry
from commonroom.providers.rpc import RpcProcess
from commonroom.setup import MachineScan

from .fakes import FakePlatform, context, turn


class CopilotProvider(Provider):
    """A provider added purely as a new class, the way a contributor would."""
    id = "copilot"
    label = "Copilot"
    install_url = "https://example.test/copilot"
    hints = ExecutableHints(commands=("copilot",))
    version_args = None

    def run_turn(self, request, context):
        context.activity("Thinking")
        context.remember_session("copilot-session")
        return TurnResult(f"echo: {request.prompt}", "copilot-session")

    def list_models(self, workspace, cancel):
        return [Model("copilot-1", "Copilot One", ["low", "high"], default_effort="high")]


def fake_provider(provider_id, models=None, detection=None):
    """A provider instance whose discovery and detection are mocks."""
    cls = type(f"Fake{provider_id}", (Provider,), {
        "id": provider_id, "label": provider_id.title(), "install_url": f"https://example.test/{provider_id}",
        "hints": ExecutableHints(commands=(provider_id,)),
        "run_turn": lambda self, request, context: TurnResult("", ""),
        "list_models": lambda self, workspace, cancel: models(workspace, cancel),
    })
    provider = cls(FakePlatform())
    if detection:
        provider.detect = detection
    return provider


class RegistryTests(unittest.TestCase):
    def test_builtin_registry_keeps_order_and_rejects_unknown_ids(self):
        registry = build_registry(FakePlatform())
        self.assertEqual(registry.ids(), ("codex", "claude", "antigravity"))
        self.assertEqual([provider.id for provider in registry], ["codex", "claude", "antigravity"])
        self.assertIn("claude", registry)
        self.assertNotIn("copilot", registry)
        self.assertNotIn(None, registry)
        with self.assertRaisesRegex(ValueError, "Choose an available provider."):
            registry.get("copilot")

    def test_builtin_providers_ship_their_logo_and_a_new_provider_needs_none(self):
        for provider in build_registry(FakePlatform()):
            path = provider.logo_file()
            self.assertIsNotNone(path, provider.id)
            self.assertTrue(path.read_bytes().lstrip().startswith(b"<svg"), provider.id)
            self.assertNotIn(b"<script", path.read_bytes().lower(), provider.id)
        # A provider without a logo is still valid: the UI draws its initial instead.
        self.assertIsNone(CopilotProvider.logo)
        self.assertIsNone(CopilotProvider(FakePlatform()).logo_file())

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderRegistry([CopilotProvider(FakePlatform()), CopilotProvider(FakePlatform())])

    def test_new_provider_class_works_everywhere_without_touching_other_code(self):
        platform = FakePlatform({"copilot": "/tools/copilot"})
        registry = build_registry(platform, (*BUILTIN_PROVIDERS, CopilotProvider))
        provider = registry.get("copilot")
        ctx = context()
        self.assertEqual(provider.run_turn(turn("hi"), ctx), TurnResult("echo: hi", "copilot-session"))
        ctx.remember_session.assert_called_once_with("copilot-session")

        scan = MachineScan(platform, registry).result()
        self.assertEqual(scan["providers"][-1], {"id": "copilot", "label": "Copilot", "installed": True,
                                                 "installUrl": "https://example.test/copilot",
                                                 "path": "/tools/copilot", "version": None, "error": None})

        with TemporaryDirectory() as workspace:
            catalog = ModelCatalog(registry, workspace)
            catalog.refresh("copilot")
            deadline = time.monotonic() + 2
            while catalog.snapshot()["loading"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(catalog.snapshot()["models"]["copilot"], [
                {"id": "copilot-1", "label": "Copilot One", "efforts": ["low", "high"], "defaultEffort": "high"}])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = folder.name

    def wait_for(self, catalog, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = catalog.snapshot()
            if predicate(state):
                return state
            time.sleep(0.01)
        self.fail(f"Catalog did not reach expected state. {catalog.snapshot()}")

    def test_discovery_is_independent_and_retry_refreshes_the_failed_provider(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        model = [Model("provider-choice", "Provider choice")]
        expected = [{"id": "provider-choice", "label": "Provider choice", "efforts": []}]

        def blocked(workspace, cancel):
            if not gate.wait(4):
                raise TimeoutError
            return model

        second = Mock(side_effect=[RuntimeError("Second unavailable"), model])
        third = Mock(return_value=model)
        catalog = ModelCatalog(ProviderRegistry([fake_provider("first", blocked), fake_provider("second", second),
                                                 fake_provider("third", third)]), self.workspace)
        self.assertNotIn("defaults", catalog.snapshot())
        catalog.refresh()
        state = self.wait_for(catalog, lambda snapshot: snapshot["loading"] == ["first"] and snapshot["errors"]
                              and snapshot["models"]["third"])
        self.assertEqual(state["errors"], {"second": "Second unavailable"})
        self.assertEqual(state["models"]["third"], expected)
        catalog.refresh(failed_only=True)
        state = self.wait_for(catalog, lambda snapshot: snapshot["models"]["second"] == expected)
        self.assertEqual(state["errors"], {})
        self.assertEqual(state["loading"], ["first"])
        third.assert_called_once()
        gate.set()
        self.wait_for(catalog, lambda snapshot: not snapshot["loading"])

    def test_providers_argument_limits_loading_to_installed_ids(self):
        loaded = Mock(return_value=[Model("m", "M")])
        skipped = Mock(return_value=[Model("m", "M")])
        catalog = ModelCatalog(ProviderRegistry([fake_provider("installed", loaded), fake_provider("absent", skipped)]),
                               self.workspace)
        catalog.refresh(providers=("installed",))
        state = self.wait_for(catalog, lambda snapshot: snapshot["models"]["installed"])
        self.assertEqual(state["models"]["absent"], [])
        skipped.assert_not_called()

    def test_error_messages_match_the_failure_kind(self):
        catalog = ModelCatalog(ProviderRegistry([
            fake_provider("empty", Mock(return_value=[])),
            fake_provider("broken", Mock(side_effect=KeyError("data"))),
            fake_provider("slow", Mock(side_effect=TimeoutError())),
            fake_provider("nameless", Mock(return_value=[Model("", "No id")])),
        ]), self.workspace)
        catalog.refresh()
        state = self.wait_for(catalog, lambda snapshot: len(snapshot["errors"]) == 4)
        self.assertEqual(state["errors"]["empty"], "The provider did not advertise any models.")
        self.assertIn("unexpected model catalog", state["errors"]["broken"])
        self.assertIn("unexpected model catalog", state["errors"]["nameless"])
        self.assertIn("timed out", state["errors"]["slow"])

    def test_discovered_lists_providers_whose_first_discovery_finished(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        catalog = ModelCatalog(ProviderRegistry([
            fake_provider("ok", Mock(return_value=[Model("m", "M")])),
            fake_provider("broken", Mock(side_effect=RuntimeError("down"))),
            fake_provider("slow", lambda workspace, cancel: gate.wait(4) and [Model("m", "M")]),
            fake_provider("idle"),
        ]), self.workspace)
        self.assertEqual(catalog.discovered(), set())
        catalog.refresh(providers=("ok", "broken", "slow"))
        self.wait_for(catalog, lambda snapshot: snapshot["loading"] == ["slow"])
        self.assertEqual(catalog.discovered(), {"ok", "broken"})
        gate.set()
        self.wait_for(catalog, lambda snapshot: not snapshot["loading"])
        self.assertEqual(catalog.discovered(), {"ok", "broken", "slow"})

    def test_unknown_provider_is_rejected(self):
        catalog = ModelCatalog(ProviderRegistry([]), self.workspace)
        with self.assertRaisesRegex(ValueError, "model catalog"):
            catalog.refresh("nobody")


class MachineScanTests(unittest.TestCase):
    def test_scan_reports_platform_python_and_providers_in_registry_order_concurrently(self):
        started = threading.Barrier(2, timeout=2)

        def detect(provider_id, installed):
            def run():
                started.wait()  # Both detections must be in flight at once.
                return Detection(provider_id, provider_id.title(), installed, f"https://example.test/{provider_id}",
                                 f"/bin/{provider_id}" if installed else None, "1.0" if installed else None)
            return run

        registry = ProviderRegistry([fake_provider("alpha", detection=detect("alpha", True)),
                                     fake_provider("beta", detection=detect("beta", False))])
        scan = MachineScan(FakePlatform(), registry)
        result = scan.result()
        self.assertEqual(result["platform"], {"name": "fake", "label": "Fake OS"})
        self.assertEqual(result["python"]["version"], "%d.%d.%d" % sys.version_info[:3])
        self.assertEqual([item["id"] for item in result["providers"]], ["alpha", "beta"])
        self.assertEqual(result["providers"][0]["version"], "1.0")
        self.assertEqual(scan.installed_ids(), ("alpha",))

    def test_result_is_cached_until_rescan_and_a_crashing_detector_is_contained(self):
        detect = Mock(side_effect=[OSError("boom"), Detection("alpha", "Alpha", True, "u", "/bin/alpha")])
        scan = MachineScan(FakePlatform(), ProviderRegistry([fake_provider("alpha", detection=detect)]))
        first = scan.result()
        self.assertEqual(first["providers"][0]["error"], "boom")
        self.assertFalse(first["providers"][0]["installed"])
        first["providers"].clear()
        self.assertEqual(len(scan.result()["providers"]), 1)
        self.assertEqual(detect.call_count, 1)
        self.assertEqual(scan.rescan()["providers"][0]["installed"], True)
        self.assertEqual(scan.installed_ids(), ("alpha",))

    def test_peek_never_waits_for_a_running_scan(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def detect():
            gate.wait(4)
            return Detection("alpha", "Alpha", True, "u", "/bin/alpha")

        scan = MachineScan(FakePlatform(), ProviderRegistry([fake_provider("alpha", detection=detect)]))
        self.assertIsNone(scan.peek())
        worker = threading.Thread(target=scan.rescan)
        worker.start()
        started = time.monotonic()
        self.assertIsNone(scan.peek())
        self.assertLess(time.monotonic() - started, 1)
        gate.set()
        worker.join(4)
        self.assertTrue(scan.peek()["providers"][0]["installed"])

    def test_missing_provider_reports_install_url_without_running_anything(self):
        scan = MachineScan(FakePlatform(), build_registry(FakePlatform()))
        providers = scan.result()["providers"]
        self.assertEqual([item["installed"] for item in providers], [False, False, False])
        self.assertEqual(providers[1]["installUrl"], "https://docs.claude.com/en/docs/claude-code/setup")


class RpcProcessTests(unittest.TestCase):
    def test_round_trip_uses_the_platform_for_environment_and_options(self):
        platform = FakePlatform()
        platform.cli_environment = Mock(return_value=None)
        platform.popen_options = Mock(return_value={})
        script = ("import json,sys\n"
                  "req=json.loads(sys.stdin.readline())\n"
                  "print(json.dumps({'jsonrpc':'2.0','method':'note','params':{'n':1}}),flush=True)\n"
                  "print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'echo':req['params']}}),flush=True)\n")
        events = []
        with TemporaryDirectory() as workspace:
            with RpcProcess(platform, [sys.executable, "-c", script], workspace) as rpc:
                result = rpc.call("ping", {"x": 1}, lambda m, p: events.append((m, p)), Mock())
        self.assertEqual(result, {"echo": {"x": 1}})
        self.assertEqual(events, [("note", {"n": 1})])
        platform.cli_environment.assert_called_once()
        platform.popen_options.assert_called_once()


if __name__ == "__main__":
    unittest.main()
