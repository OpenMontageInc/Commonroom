import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from commonroom.core.projects import DEFAULT_ID, ProjectNotFound, ProjectRooms, ProjectUnavailable
from commonroom.core.room import Room
from tests.fakes import FakeHarness, FakePlatform, FakeProviders, FakeSettings, wait_until_idle


class ProjectRoomTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="commonroom-projects-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.data = self.root / "application-data"
        self.default = self.root / "Default Project"
        self.default.mkdir()
        self.other = self.root / "Other Project"
        self.other.mkdir()
        self.providers = FakeProviders(FakeHarness())
        self.settings = FakeSettings()
        self.platform = FakePlatform()

    def make_room(self, storage, path=None):
        return Room(storage, self.providers, self.settings, project_path=path)

    def open_projects(self, *, seed=True):
        projects = ProjectRooms(self.data, self.platform, self.make_room, initial_project=self.default if seed else None)

        def close():
            for room in projects._rooms.values():
                wait_until_idle(room)
                room.db.close()
            projects.db.close()

        self.addCleanup(close)
        return projects

    def legacy_room(self, text):
        legacy = self.make_room(self.data)
        try:
            legacy.send(text, "old")
            wait_until_idle(legacy)
        finally:
            legacy.db.close()

    def test_fresh_install_starts_empty_and_the_first_project_becomes_default(self):
        projects = self.open_projects(seed=False)
        self.assertEqual(projects.snapshot(), {"projects": [], "defaultId": None})
        with self.assertRaises(ProjectNotFound):
            projects.room(DEFAULT_ID)
        self.assertFalse((self.data / "room.sqlite3").exists())

        project = projects.add(self.other)
        self.assertEqual(project["id"], DEFAULT_ID)
        self.assertEqual(projects.room(DEFAULT_ID).workspace, self.other)
        self.assertEqual(projects.snapshot(), {"projects": [project], "defaultId": DEFAULT_ID})
        self.assertEqual(self.open_projects(seed=False).snapshot(), projects.snapshot())

    def test_unclaimed_legacy_room_is_not_adopted_by_an_unrelated_first_project(self):
        self.legacy_room("The older transcript")
        projects = self.open_projects(seed=False)
        self.assertEqual(projects.snapshot(), {"projects": [], "defaultId": None})
        unrelated = projects.add(self.other)
        self.assertNotEqual(unrelated["id"], DEFAULT_ID)
        self.assertEqual(projects.snapshot()["defaultId"], unrelated["id"])
        self.assertEqual(projects.room(unrelated["id"]).state()["messages"], [])

        registered_legacy = self.open_projects()
        self.assertEqual(registered_legacy.default_id(), DEFAULT_ID)
        self.assertEqual(registered_legacy.add(self.other), unrelated)
        self.assertEqual([message["text"] for message in registered_legacy.room(DEFAULT_ID).state()["messages"]],
                         ["The older transcript"])

    def test_legacy_database_stays_in_place_and_projects_keep_separate_rooms(self):
        self.legacy_room("Existing conversation")
        projects = self.open_projects()
        default = projects.room(DEFAULT_ID)
        self.assertEqual(default.folder, self.data)
        self.assertEqual(default.workspace, self.default)
        self.assertEqual([message["text"] for message in default.state()["messages"]], ["Existing conversation"])

        entry = projects.add(str(self.other))
        other = projects.room(entry["id"])
        self.assertEqual(other.folder, self.data / "projects" / entry["id"])
        self.assertEqual(other.workspace, self.other)
        self.assertEqual(other.state()["messages"], [])
        other.send("Other conversation", "same-request")
        wait_until_idle(other)

        reopened = self.open_projects()
        self.assertEqual(reopened.add(str(self.other)), entry)
        self.assertEqual(reopened.snapshot(), projects.snapshot())
        self.assertEqual([message["text"] for message in reopened.room(entry["id"]).state()["messages"]], ["Other conversation"])
        self.assertEqual([message["text"] for message in default.state()["messages"]], ["Existing conversation"])

    def test_each_new_project_room_starts_with_the_current_lead(self):
        projects = self.open_projects()
        self.settings.data["lead"]["name"] = "Second lead"
        entry = projects.add(str(self.other))
        names = [m["name"] for m in projects.room(entry["id"]).state()["members"]]
        self.assertEqual(names, ["Second lead"])
        self.assertEqual([m["name"] for m in projects.room(DEFAULT_ID).state()["members"]], ["Chief of Staff"])

    def test_platform_decides_whether_two_paths_are_the_same_folder(self):
        projects = self.open_projects()
        entry = projects.add(str(self.other))
        alias = self.root / "Alias"
        alias.mkdir()
        self.platform.same_folder = lambda a, b: a == alias and b == self.other
        self.assertEqual(projects.add(str(alias)), entry)
        self.assertEqual(len(projects.snapshot()["projects"]), 2)

    def test_quoted_paths_are_accepted_and_relative_paths_rejected(self):
        projects = self.open_projects(seed=False)
        entry = projects.add(f'"{self.other}"')
        self.assertEqual(projects.add(f"'{self.other}'"), entry)
        with self.assertRaisesRegex(ValueError, "full path"):
            projects.add("relative/folder")
        with self.assertRaises(ValueError):
            projects.add(str(self.root / "missing"))

    @unittest.skipUnless(os.name == "nt", "Windows project path aliases")
    def test_concurrent_case_quoted_path_and_junction_registration_reuses_one_room(self):
        import _winapi

        junction = self.root / "Alias to Other Project"
        _winapi.CreateJunction(str(self.other), str(junction))
        projects = self.open_projects()
        candidates = [str(self.other), str(self.other).upper(), str(junction), f'"{junction}"', f"'{self.other}'"] * 2
        with ThreadPoolExecutor(max_workers=5) as pool:
            entries = list(pool.map(projects.add, candidates))
        self.assertTrue(all(entry == entries[0] for entry in entries))
        self.assertEqual(entries[0]["path"], str(self.other))
        self.assertEqual(len(projects.snapshot()["projects"]), 2)
        self.assertEqual(len(list((self.data / "projects").iterdir())), 1)

    def test_missing_default_keeps_registry_and_transcript_on_reconnect(self):
        projects = self.open_projects()
        project = projects.room(DEFAULT_ID)
        project.send("Keep this while offline", "one")
        wait_until_idle(project)
        entry = projects.add(str(self.other))
        moved = self.root / "Renamed Project"
        self.default.rename(moved)
        self.assertRaises(ProjectUnavailable, projects.room, DEFAULT_ID)

        reopened = self.open_projects()
        self.assertFalse(self.default.exists())
        self.assertEqual(reopened.snapshot(), projects.snapshot())
        self.assertRaises(ProjectUnavailable, reopened.room, DEFAULT_ID)
        self.assertEqual(reopened.room(entry["id"]).workspace, self.other)
        moved.rename(self.default)
        self.assertEqual([message["text"] for message in reopened.room(DEFAULT_ID).state()["messages"]],
                         ["Keep this while offline"])

    def test_corrupted_room_does_not_block_the_other_projects(self):
        projects = self.open_projects()
        entry = projects.add(str(self.other))
        room = projects.room(entry["id"])
        wait_until_idle(room)
        room.db.close()
        (room.folder / "room.sqlite3").write_bytes(b"corrupted local database")
        reopened = self.open_projects()
        self.assertEqual(reopened.snapshot(), projects.snapshot())
        self.assertEqual(reopened.room(DEFAULT_ID).workspace, self.default)
        with self.assertRaisesRegex(ProjectUnavailable, "saved room for Other Project could not be opened"):
            reopened.room(entry["id"])

    @unittest.skipUnless(os.name == "nt", "Windows project folder junctions")
    def test_previously_opened_path_redirect_is_unavailable(self):
        import _winapi

        projects = self.open_projects()
        original = self.root / "Original Project"
        self.default.rename(original)
        _winapi.CreateJunction(str(self.other), str(self.default))
        self.assertRaises(ProjectUnavailable, projects.room, DEFAULT_ID)
        reopened = self.open_projects()
        self.assertRaises(ProjectUnavailable, reopened.room, DEFAULT_ID)
        self.assertEqual(reopened.snapshot(), projects.snapshot())


if __name__ == "__main__":
    unittest.main()
