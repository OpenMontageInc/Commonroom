import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from commonroom.core.settings import Settings

PROVIDERS = ("codex", "claude")
LEAD = {"name": "Lead", "provider": "claude", "model": "opus", "effort": "high"}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "data" / "settings.json"
        self.settings = Settings(self.path)

    def test_defaults_use_the_os_user_name_and_no_lead(self):
        with patch("commonroom.core.settings.getpass.getuser", return_value="sam"):
            self.assertEqual(self.settings.get(), {"displayName": "sam", "lead": None, "setupComplete": False})
        with patch("commonroom.core.settings.getpass.getuser", side_effect=OSError("no user")):
            self.assertEqual(self.settings.get()["displayName"], "You")
        self.assertFalse(self.path.exists())

    def test_partial_updates_persist_atomically_and_merge(self):
        self.assertEqual(self.settings.update({"displayName": "  Sam  "}, PROVIDERS)["displayName"], "Sam")
        full = self.settings.update({"lead": dict(LEAD, model=" opus "), "setupComplete": True}, PROVIDERS)
        self.assertEqual(full, {"displayName": "Sam", "lead": LEAD, "setupComplete": True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), full)
        self.assertEqual(Settings(self.path).get(), full)
        self.assertFalse(self.path.with_name("settings.json.tmp").exists())
        self.assertIsNone(self.settings.update({"lead": None}, PROVIDERS)["lead"])
        self.assertEqual(self.settings.update({"lead": dict(LEAD, effort=None)}, PROVIDERS)["lead"]["effort"], "")

    def test_invalid_values_are_rejected_without_writing(self):
        bad = [
            {"displayName": ""}, {"displayName": "   "}, {"displayName": 5}, {"displayName": "x" * 81},
            {"lead": "claude"}, {"lead": dict(LEAD, provider="unknown")}, {"lead": dict(LEAD, model="")},
            {"lead": dict(LEAD, name="")}, {"lead": dict(LEAD, effort=3)},
            {"setupComplete": "yes"}, {"unknown": 1},
        ]
        for partial in bad:
            with self.subTest(partial=partial), self.assertRaises(ValueError):
                self.settings.update(partial, PROVIDERS)
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            self.settings.update([], PROVIDERS)
        self.assertEqual(self.settings.update({"displayName": "x" * 80}, PROVIDERS)["displayName"], "x" * 80)

    def test_corrupt_file_falls_back_to_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.settings.get()["lead"])
        self.path.write_text(json.dumps({"displayName": "", "lead": "bad", "setupComplete": 1}), encoding="utf-8")
        data = self.settings.get()
        self.assertIsNone(data["lead"])
        self.assertFalse(data["setupComplete"])
        self.assertTrue(data["displayName"])

    def test_hand_edited_lead_is_validated_on_read(self):
        self.path.parent.mkdir(parents=True)
        settings = Settings(self.path, lambda: PROVIDERS)
        for bad in ({"provider": "claude"}, {"name": "Lead", "provider": "claude"}, {"name": "", "provider": "claude", "model": "m"},
                    {"name": "Lead", "provider": "nobody", "model": "m"}, {"name": "Lead", "provider": "claude", "model": 5},
                    {"name": "Lead", "provider": "claude", "model": "m", "effort": ["high"]}, ["Lead"]):
            with self.subTest(lead=bad):
                self.path.write_text(json.dumps({"lead": bad, "setupComplete": True}), encoding="utf-8")
                self.assertIsNone(settings.get()["lead"])
        self.path.write_text(json.dumps({"lead": dict(LEAD, model=" opus ", extra=1)}), encoding="utf-8")
        self.assertEqual(settings.get()["lead"], LEAD)
        # Without a provider list only the shape is checked.
        self.path.write_text(json.dumps({"lead": dict(LEAD, provider="other")}), encoding="utf-8")
        self.assertEqual(self.settings.get()["lead"]["provider"], "other")

    def test_concurrent_updates_keep_a_valid_file(self):
        def worker(number):
            for _ in range(20):
                self.settings.update({"displayName": f"Name {number}"}, PROVIDERS)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["displayName"].startswith("Name "))


if __name__ == "__main__":
    unittest.main()
