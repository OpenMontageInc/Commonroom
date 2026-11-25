import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from commonroom import __version__
from commonroom.app import App, build_app, own_data_folder
from commonroom.core.identity import HUMAN_ID
from commonroom.core.projects import ProjectRooms
from commonroom.core.room import Room
from commonroom.core.settings import Settings
from commonroom.platform.base import DataFolderInUse
from commonroom.providers.base import Detection
from commonroom.server import PICKER_PROMPT, make_server
from tests.fakes import LEAD, FakeHarness, FakePlatform, FakeProviders, wait_until_idle


class FakeScan:
    def __init__(self, registry, installed=("codex", "claude")):
        self.registry, self.installed, self.rescans = registry, set(installed), 0
        self.scanning = False

    def result(self):
        return {"providers": [Detection(p.id, p.label, p.id in self.installed, p.install_url).to_dict()
                              for p in self.registry]}

    def peek(self):
        return None if self.scanning else self.result()

    def rescan(self):
        self.rescans += 1

    def installed_ids(self):
        return [p.id for p in self.registry if p.id in self.installed]


class FakeCatalog:
    def __init__(self):
        self.refreshes = []
        self.done = {"codex", "claude", "antigravity"}

    def discovered(self):
        return set(self.done)

    def snapshot(self):
        return {"models": {"claude": [{"id": "opus", "label": "Opus", "efforts": ["high"]}]}, "errors": {}, "loading": []}

    def refresh(self, provider=None, *, failed_only=False, providers=None):
        self.refreshes.append((provider, failed_only, providers))


class ServerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="commonroom-server-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.data = self.root / "data"
        self.project = self.root / "Project"
        self.other = self.root / "Other"
        self.project.mkdir()
        self.other.mkdir()
        self.platform = FakePlatform()
        self.registry = FakeProviders(FakeHarness())
        self.settings = Settings(self.data / "settings.json")
        self.settings.update({"displayName": "Sam", "lead": dict(LEAD, provider="codex")}, self.registry.ids())
        self.projects = ProjectRooms(self.data, self.platform,
                                     lambda storage, path: Room(storage, self.registry, self.settings, project_path=path),
                                     initial_project=self.project)
        self.scan, self.catalog = FakeScan(self.registry), FakeCatalog()
        self.app = App(self.platform, self.registry, self.scan, self.catalog, self.settings, self.projects)
        self.http = make_server(self.app, 0)
        listener = threading.Thread(target=self.http.serve_forever)
        listener.start()
        self.endpoint = f"http://127.0.0.1:{self.http.server_port}"

        def stop():
            self.http.shutdown()
            listener.join()
            self.http.server_close()
            for room in self.projects.rooms():
                wait_until_idle(room)
                room.db.close()
            self.projects.db.close()

        self.addCleanup(stop)

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        sent = {"Content-Type": "application/json"} if data else {}
        sent.update(headers or {})
        try:
            response = urlopen(Request(self.endpoint + path, data=data, headers=sent, method=method), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            kind = response.headers.get_content_type()
            return response.code, json.loads(raw) if kind == "application/json" else raw

    def test_health_setup_settings_and_providers(self):
        self.assertEqual(self.request("GET", "/api/health"), (200, {"service": "commonroom", "version": __version__}))
        status, setup = self.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertEqual(setup["platform"], {"name": "test", "label": "Test OS"})
        self.assertTrue(setup["python"]["version"])
        self.assertEqual([(p["id"], p["installed"]) for p in setup["providers"]],
                         [("codex", True), ("claude", True), ("antigravity", False)])
        self.assertEqual(set(setup["providers"][0]), {"id", "label", "installed", "installUrl", "path", "version", "error"})
        self.assertEqual(setup["settings"]["displayName"], "Sam")
        self.assertFalse(setup["setupComplete"])

        self.assertEqual(self.request("GET", "/api/providers")[1], {"providers": [
            {"id": "codex", "label": "Codex", "installed": True},
            {"id": "claude", "label": "Claude", "installed": True},
            {"id": "antigravity", "label": "Antigravity", "installed": False}]})

        status, updated = self.request("PUT", "/api/settings", {"displayName": "Samantha", "setupComplete": True})
        self.assertEqual(status, 200)
        self.assertEqual(updated["displayName"], "Samantha")
        self.assertEqual(self.request("GET", "/api/settings")[1], updated)
        self.assertTrue(self.request("GET", "/api/setup")[1]["setupComplete"])
        self.assertEqual(self.request("PUT", "/api/settings", {"lead": dict(LEAD, provider="nope")})[0], 400)
        self.assertEqual(self.request("PUT", "/api/settings", {"displayName": ""})[0], 400)
        self.assertEqual(self.request("GET", "/api/state")[1]["human"], {"id": HUMAN_ID, "name": "Samantha"})

    def test_rescan_redetects_and_rediscovers_models_for_installed_providers(self):
        self.scan.installed = {"claude"}
        status, setup = self.request("POST", "/api/setup/rescan")
        self.assertEqual(status, 200)
        self.assertEqual(self.scan.rescans, 1)
        self.assertEqual(self.catalog.refreshes, [(None, False, ["claude"])])
        self.assertEqual([p["id"] for p in setup["providers"] if p["installed"]], ["claude"])

    def test_models_include_defaults_from_the_lead_setting(self):
        status, models = self.request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["defaults"], {"codex": {"model": LEAD["model"], "effort": LEAD["effort"]}})
        self.assertIn("claude", models["models"])
        self.settings.update({"lead": None}, self.registry.ids())
        self.assertEqual(self.request("GET", "/api/models")[1]["defaults"], {})
        self.assertEqual(self.request("POST", "/api/models/retry?provider=claude")[0], 200)
        self.assertEqual(self.catalog.refreshes[-1], ("claude", True, None))

    def test_models_report_providers_without_a_first_discovery_as_loading(self):
        # While the first machine scan runs, nothing is known yet: every provider is loading.
        self.scan.scanning, self.catalog.done = True, set()
        self.assertEqual(self.request("GET", "/api/models")[1]["loading"], ["codex", "claude", "antigravity"])
        # Scan finished: installed providers load until discovered; missing ones never load.
        self.scan.scanning = False
        self.assertEqual(self.request("GET", "/api/models")[1]["loading"], ["codex", "claude"])
        self.catalog.done = {"claude"}
        self.assertEqual(self.request("GET", "/api/models")[1]["loading"], ["codex"])
        self.catalog.done = {"claude", "codex"}
        self.assertEqual(self.request("GET", "/api/models")[1]["loading"], [])

    def test_shutdown_answers_then_stops_the_service(self):
        self.assertEqual(self.request("POST", "/api/shutdown", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/shutdown"), (200, {"ok": True}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urlopen(self.endpoint + "/api/health", timeout=0.5).close()
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("The service kept answering after /api/shutdown.")

    def test_members_can_be_added_patched_and_removed(self):
        self.assertEqual(self.request("POST", "/api/members", {"name": "Helper", "provider": "unknown", "model": "m"})[0], 400)
        self.assertEqual(self.request("POST", "/api/members", {"name": "Helper", "provider": "claude", "model": "opus",
                                                               "effort": "high", "role": "Reviewer"})[0], 200)
        lead, helper = self.request("GET", "/api/state")[1]["members"]
        self.assertTrue(lead["lead"])
        self.assertEqual(set(helper), {"id", "name", "provider", "model", "effort", "role", "lead", "aliases"})
        self.assertEqual(self.request("PATCH", f"/api/members/{helper['id']}",
                                      {"name": "Critic", "model": "sonnet", "effort": "low", "role": "Critic"})[0], 200)
        _, helper = self.request("GET", "/api/state")[1]["members"]
        self.assertEqual((helper["name"], helper["model"], helper["effort"], helper["role"], helper["aliases"]),
                         ("Critic", "sonnet", "low", "Critic", ["Helper"]))
        self.assertEqual(self.request("PATCH", f"/api/members/{helper['id']}", {"model": ""})[0], 400)
        self.assertEqual(self.request("PATCH", f"/api/members/{helper['id']}", {"model": "x"},
                                      {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("DELETE", f"/api/members/{lead['id']}")[0], 200)
        self.assertEqual([m["name"] for m in self.request("GET", "/api/state")[1]["members"]], ["Critic"])

    def test_scoped_project_routes(self):
        projects = self.projects
        self.assertEqual(self.request("GET", "/api/projects"), (200, projects.snapshot()))
        status, project = self.request("POST", "/api/projects", {"path": str(self.other)})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("POST", "/api/projects", {"path": str(self.other)}), (200, project))
        self.assertEqual(self.request("POST", "/api/projects", {"path": "unknown"})[0], 400)
        self.assertEqual(self.request("GET", "/api/projects/missing/state")[0], 404)
        prefix = f"/api/projects/{project['id']}"
        lead = self.request("GET", prefix + "/state")[1]["members"][0]
        self.assertEqual(self.request("PATCH", prefix + f"/members/{lead['id']}", {"name": "Project Chief"})[0], 200)
        self.assertEqual(self.request("POST", prefix + "/messages", {"text": "Project message", "request_id": "one"})[0], 200)
        scoped = self.request("GET", prefix + "/state")[1]
        self.assertEqual(scoped["members"][0]["name"], "Project Chief")
        self.assertEqual(scoped["messages"][0]["text"], "Project message")
        self.assertEqual(scoped["messages"][0]["author"], HUMAN_ID)
        self.assertEqual(self.request("GET", prefix + "/state?after=1")[1]["messages"], [])
        self.assertEqual(self.request("GET", prefix + "/state?after=x")[0], 400)
        self.assertEqual(self.request("GET", "/api/state")[1]["messages"], [])
        self.assertEqual(self.request("POST", "/api/stop/999")[0], 400)
        self.assertEqual(self.request("POST", "/api/nowhere")[0], 404)

    def test_folder_picker_uses_the_platform(self):
        self.assertEqual(self.request("POST", "/api/projects/browse"), (200, {"path": None}))
        self.platform.picked = str(self.other)
        self.assertEqual(self.request("POST", "/api/projects/browse"), (200, {"path": str(self.other)}))
        self.assertEqual(self.platform.prompts[-1], PICKER_PROMPT)
        self.platform.picker_error = "No picker here. Enter the folder path instead."
        self.assertEqual(self.request("POST", "/api/projects/browse"),
                         (409, {"error": "No picker here. Enter the folder path instead."}))

    def test_untrusted_origins_are_refused(self):
        self.assertEqual(self.request("GET", "/api/health", headers={"Origin": "http://evil.test"})[0], 403)
        self.assertEqual(self.request("GET", "/api/health", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        # No body: the server refuses before reading one, which can reset a connection still sending it.
        self.assertEqual(self.request("PUT", "/api/settings", headers={"Host": f"evil.test:{self.http.server_port}"})[0], 403)
        self.assertEqual(self.settings.get()["displayName"], "Sam")

    def test_ui_files_are_served_from_the_package(self):
        web = Path(__file__).resolve().parents[1] / "commonroom" / "web"
        if not (web / "index.html").is_file():
            self.skipTest("UI files have not been moved into commonroom/web yet")
        status, body = self.request("GET", "/")
        self.assertEqual((status, body), (200, (web / "index.html").read_bytes()))
        self.assertEqual(self.request("GET", "/appearance.css")[0], 200)
        self.assertEqual(self.request("GET", "/fonts/missing.woff2")[0], 404)


class DataFolderLockTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "Commonroom has no platform for this OS yet")
    def test_real_platform_lock_excludes_a_second_owner_and_releases(self):
        from commonroom.platform import current_platform
        platform = current_platform()
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            with own_data_folder(data, platform):
                with self.assertRaises(DataFolderInUse):
                    with own_data_folder(data, platform):
                        self.fail("A second service acquired the same data folder")
            with own_data_folder(data, platform):
                self.assertTrue((data / "room.lock").is_file())

    def test_service_data_lock_excludes_a_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            data, platform = Path(folder) / "data", FakePlatform()
            with own_data_folder(data, platform):
                with self.assertRaises(DataFolderInUse):
                    with own_data_folder(data, platform):
                        self.fail("A second service acquired the same data folder")
            with own_data_folder(data, platform):
                self.assertTrue((data / "room.lock").is_file())


def _composition_modules_exist():
    return all(importlib.util.find_spec(name) for name in
               ("commonroom.providers.registry", "commonroom.providers.catalog", "commonroom.setup"))


@unittest.skipUnless(_composition_modules_exist(), "Provider registry, catalog and setup scan are not available yet")
class BuildAppTests(unittest.TestCase):
    def test_build_app_wires_registry_settings_and_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "Project").mkdir()
            app = build_app(root / "data", root / "Project", platform=FakePlatform())
            try:
                ids = list(app.registry.ids())
                self.assertTrue(ids)
                self.assertEqual(app.settings.path, root / "data" / "settings.json")
                self.assertEqual(app.projects.snapshot()["defaultId"], "default")
                self.assertEqual(app.models()["defaults"], {})
            finally:
                for room in app.projects.rooms():
                    wait_until_idle(room)
                    room.db.close()
                app.projects.db.close()


if __name__ == "__main__":
    unittest.main()
