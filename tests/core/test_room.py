import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from commonroom.core.identity import HUMAN_ID, SILENCE
from commonroom.core.room import Room
from commonroom.providers.base import SessionLost
from tests.fakes import LEAD, FakeHarness, FakeProviders, FakeSettings, wait_until_idle

LEGACY_SCHEMA = """
  CREATE TABLE members(id TEXT PRIMARY KEY,name TEXT,provider TEXT,model TEXT,effort TEXT,
    permanent INTEGER DEFAULT 0,active INTEGER DEFAULT 1,session TEXT DEFAULT '',cursor INTEGER DEFAULT 0);
  CREATE TABLE messages(id INTEGER PRIMARY KEY,author TEXT,name TEXT,text TEXT,created TEXT,request_id TEXT UNIQUE);
  CREATE TABLE jobs(id INTEGER PRIMARY KEY,member TEXT,target INTEGER,status TEXT,activity TEXT DEFAULT '',error TEXT DEFAULT '');
  INSERT INTO members(id,name,provider,model,effort,permanent,session) VALUES('chief','Chief','codex','old-model','medium',1,'native-chief');
  INSERT INTO members(id,name,provider,model,effort) VALUES('helper','Helper','claude','opus','high');
  INSERT INTO messages(author,name,text,created,request_id) VALUES('human','Sam','Hello @Chief','2026-01-01T00:00:00+00:00','old-1');
  INSERT INTO messages(author,name,text,created) VALUES('chief','Chief','Hi there','2026-01-01T00:00:01+00:00');
"""


class RoomTestCase(unittest.TestCase):
    lead = LEAD

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.harness = FakeHarness()
        self.providers = FakeProviders(self.harness)
        self.settings = FakeSettings(self.lead)
        self.room = self.open()
        self.addCleanup(self.close_room)

    def open(self, folder=None, project_path=None):
        return Room(folder or self.folder.name, self.providers, self.settings, project_path=project_path)

    def idle(self):
        wait_until_idle(self.room)

    def close_room(self):
        if self.room is not None:
            self.idle()
            self.room.db.close()
            self.room = None

    def reopen(self):
        self.close_room()
        self.room = self.open()
        self.idle()

    @property
    def lead_id(self):
        return next(m["id"] for m in self.room.state()["members"] if m["lead"])

    def add(self, name, provider, model, effort):
        self.room.add(dict(name=name, provider=provider, model=model, effort=effort))
        return next(member for member in self.room.state()["members"] if member["name"] == name)

    def send(self, text, request):
        self.room.send(text, request)
        self.idle()

    def turns(self, name):
        return [turn for turn in self.harness.calls if turn.member["name"] == name]

    def archive_messages(self):
        return [json.loads(line) for line in self.room.archive.read_text(encoding="utf-8").splitlines()]


class RoomIntegrationTests(RoomTestCase):
    def test_project_room_restarts_a_session_saved_in_another_folder(self):
        storage = Path(self.folder.name)
        project = storage / "Selected project"
        project.mkdir()
        instructions = project / "AGENTS.md"
        instructions.write_text("# Selected project instructions\nUse the selected project marker.\n", encoding="utf-8")
        gemini = self.add("Gemini", "antigravity", "fake-gemini", "medium")
        lead_id = self.lead_id
        self.send("Earlier message in this same room", "earlier")
        original_archive = self.room.archive
        legacy_workspace = self.room.workspace
        self.close_room()
        with closing(sqlite3.connect(storage / "room.sqlite3")) as legacy, legacy:
            legacy.execute("ALTER TABLE members DROP COLUMN session_workspace")

        def respond(turn):
            if turn.member["id"] == gemini["id"]:
                if turn.request.session and turn.request.session_workspace != str(turn.workspace):
                    raise SessionLost("Session belongs to another folder")
                return SILENCE, "gemini-in-project"
            return self.harness.silent(turn)

        self.harness.respond = respond
        self.room = self.open(storage, project)
        self.send("Continue in the selected project", "next")

        chief = self.turns("Chief of Staff")[-1]
        self.assertEqual(chief.workspace, project)
        self.assertEqual(chief.member["session"], f"native-{lead_id}")
        self.assertEqual(chief.member["session_workspace"], str(legacy_workspace))
        self.assertIn(f"Your working directory and project for this room is {project}", chief.prompt)
        self.assertIn(str(instructions), chief.prompt)
        self.assertIn("Use the selected project marker.", chief.prompt)
        _, previous, current = self.turns("Gemini")
        self.assertEqual(previous.member["session"], f"native-{gemini['id']}")
        self.assertEqual(current.member["session"], "")
        self.assertEqual(current.workspace, project)
        self.assertEqual(current.messages[0]["text"], "Earlier message in this same room")
        self.assertEqual(self.room.archive, original_archive)
        self.assertEqual(self.archive_messages(), self.room.state()["messages"])

        self.harness.respond = self.harness.silent
        self.reopen()
        self.send("Keep working in the project", "third")
        self.assertEqual(self.turns("Gemini")[-1].member["session"], "gemini-in-project")
        self.assertEqual(self.turns("Gemini")[-1].member["session_workspace"], str(project))

    def test_projects_keep_room_context_and_native_sessions_separate(self):
        self.close_room()
        root = Path(self.folder.name)
        first, second = root / "Project A", root / "Project B"
        for project, marker in [(first, "FIRST_PROJECT_ONLY"), (second, "SECOND_PROJECT_ONLY")]:
            project.mkdir()
            (project / "AGENTS.md").write_text(marker, encoding="utf-8")

        first_storage = root / "first-room"
        self.room = self.open(first_storage, first)
        self.send("First room transcript marker", "first-message")
        self.close_room()
        with self.assertRaisesRegex(ValueError, "different project"):
            self.open(first_storage, second)

        self.room = self.open(root / "second-room", second)
        self.send("Second room message", "second-message")
        turn = self.turns("Chief of Staff")[-1]
        self.assertEqual(turn.workspace, second)
        self.assertEqual(turn.member["session"], "")
        self.assertIn("SECOND_PROJECT_ONLY", turn.prompt)
        self.assertNotIn("FIRST_PROJECT_ONLY", turn.prompt)
        self.assertNotIn("First room transcript marker", turn.prompt)
        self.assertEqual([message["text"] for message in self.archive_messages()], ["Second room message"])
        (second / "AGENTS.md").write_text("UPDATED_SECOND_PROJECT", encoding="utf-8")
        self.send("Refresh project instructions", "third-message")
        self.assertIn("UPDATED_SECOND_PROJECT", self.turns("Chief of Staff")[-1].prompt)

    def test_open_floor_delivers_an_addressed_message_and_keeps_silence_private(self):
        self.add("Claude", "claude", "fake-opus", "high")
        self.add("Gemini", "antigravity", "fake-gemini", "low")
        self.send("@Claude, here is some context for later.", "open-floor")

        self.assertCountEqual([turn.member["name"] for turn in self.harness.calls], ["Chief of Staff", "Claude", "Gemini"])
        for turn in self.harness.calls:
            self.assertEqual(turn.target["text"], "@Claude, here is some context for later.")
            self.assertEqual(turn.target["name"], "Sam")
            self.assertEqual(turn.target["author"], HUMAN_ID)
            self.assertIn("Commonroom, Sam's local group chat. Current participants: Sam and", turn.prompt)
        self.assertEqual([item["name"] for item in self.room.state()["messages"]], ["Sam"])
        self.assertEqual(self.room.state()["jobs"], [])

    def test_turn_request_carries_member_model_effort_and_provider(self):
        self.add("Claude", "claude", "fake-opus", "high")
        self.send("Hello", "hello")
        claude, = self.turns("Claude")
        self.assertEqual((claude.provider, claude.request.model, claude.request.effort), ("claude", "fake-opus", "high"))
        chief, = self.turns("Chief of Staff")
        self.assertEqual((chief.provider, chief.request.model, chief.request.effort), ("codex", "fake-lead", "medium"))

    def test_peer_reply_reaches_other_members_and_native_sessions_stay_separate(self):
        self.add("Sol", "codex", "fake-sol", "low")
        self.add("Claude", "claude", "fake-opus", "high")

        def respond(turn):
            silence, session = self.harness.silent(turn)
            if turn.member["name"] == "Chief of Staff" and turn.target["text"] == "Chief of Staff, what did you find?":
                return "I found the missing frame.", session
            return silence, session

        self.harness.respond = respond
        self.send("Chief of Staff, what did you find?", "ask-lead")

        self.assertEqual([(msg["name"], msg["text"]) for msg in self.room.state()["messages"]], [
            ("Sam", "Chief of Staff, what did you find?"), ("Chief of Staff", "I found the missing frame.")
        ])
        self.assertEqual(len(self.turns("Chief of Staff")), 1)
        for name in ("Sol", "Claude"):
            turns = self.turns(name)
            self.assertEqual(turns[-1].target["name"], "Chief of Staff")
            self.assertEqual(turns[-1].target["text"], "I found the missing frame.")
            self.assertIn("Chief of Staff, what did you find?", [message["text"] for message in turns[-1].messages])

        self.send("Thanks, everyone.", "confirm-sessions")
        sessions = set()
        for name in ("Chief of Staff", "Sol", "Claude"):
            first, *_, last = self.turns(name)
            self.assertEqual(first.member["session"], "")
            self.assertEqual(last.member["session"], f"native-{first.member['id']}")
            self.assertEqual(last.target["text"], "Thanks, everyone.")
            sessions.add(last.member["session"])
        self.assertEqual(len(sessions), 3)

    def test_roster_add_remove_and_name_reuse_preserve_attribution(self):
        member = self.add("Claude", "claude", "fake-opus", "high")
        self.assertEqual({key: member[key] for key in ("provider", "model", "effort", "lead")}, {
            "provider": "claude", "model": "fake-opus", "effort": "high", "lead": False
        })
        self.assertNotIn("permanent", member)
        with self.assertRaises(ValueError):
            self.room.remove("nobody")
        with self.assertRaises(ValueError):
            self.room.add(dict(name="Ghost", provider="unregistered", model="x", effort=""))

        def respond(turn):
            if turn.member["id"] == member["id"]:
                return "I can confirm the edit.", "original-session"
            return self.harness.silent(turn)

        self.harness.respond = respond
        self.send("Claude, please check the edit.", "check-edit")
        self.room.remove(member["id"])
        replacement = self.add("Claude", "antigravity", "fake-gemini", "medium")
        self.send("What did the previous review say?", "check-history")

        self.assertNotEqual(replacement["id"], member["id"])
        first, second = self.turns("Claude")
        self.assertEqual(first.member["provider"], "claude")
        self.assertEqual(second.member["provider"], "antigravity")
        self.assertEqual(second.member["session"], "")
        self.assertIn("I can confirm the edit.", [message["text"] for message in second.messages])
        reply = next(msg for msg in self.room.state()["messages"] if msg["name"] == "Claude")
        self.assertEqual(reply["author"], member["id"])
        self.assertCountEqual([item["id"] for item in self.room.state()["members"]], [self.lead_id, replacement["id"]])

    def test_lead_sorts_first_and_can_be_removed(self):
        self.add("Claude", "claude", "fake-opus", "high")
        members = self.room.state()["members"]
        self.assertEqual([m["lead"] for m in members], [True, False])
        self.assertEqual(members[0]["name"], "Chief of Staff")
        self.room.remove(members[0]["id"])
        self.assertEqual([m["name"] for m in self.room.state()["members"]], ["Claude"])
        self.send("Anyone here?", "after-lead-left")
        self.assertEqual(self.turns("Chief of Staff"), [])
        self.assertEqual(len(self.turns("Claude")), 1)
        self.reopen()
        self.assertEqual([m["name"] for m in self.room.state()["members"]], ["Claude"])

    def test_state_reports_the_human_with_the_current_display_name(self):
        self.send("Before the rename", "one")
        self.settings.data["displayName"] = "Samantha"
        self.send("After the rename", "two")
        state = self.room.state()
        self.assertEqual(state["human"], {"id": HUMAN_ID, "name": "Samantha"})
        self.assertEqual([(m["author"], m["name"]) for m in state["messages"]],
                         [(HUMAN_ID, "Sam"), (HUMAN_ID, "Samantha")])

    def test_update_member_changes_model_and_effort_but_keeps_the_session(self):
        lead_id = self.lead_id
        self.send("First", "first")
        self.room.update_member(lead_id, {"model": "fake-bigger", "effort": "high", "role": "Planner"})
        self.send("Second", "second")
        first, second = self.turns("Chief of Staff")
        self.assertEqual((second.request.model, second.request.effort), ("fake-bigger", "high"))
        self.assertEqual(second.request.session, f"native-{lead_id}")
        member = self.room.state()["members"][0]
        self.assertEqual((member["model"], member["effort"], member["role"]), ("fake-bigger", "high", "Planner"))

        self.room.update_member(lead_id, {"name": "Operations"})
        self.assertEqual(self.room.state()["members"][0]["aliases"], ["Chief of Staff"])
        for bad in ({}, {"model": ""}, {"model": 3}, {"name": " "}, {"effort": 1}):
            with self.assertRaises(ValueError):
                self.room.update_member(lead_id, bad)
        with self.assertRaises(ValueError):
            self.room.update_member("nobody", {"model": "x"})
        self.assertEqual(self.room.state()["members"][0]["model"], "fake-bigger")

    def test_removing_a_running_member_discards_their_late_reply(self):
        member = self.add("Claude", "claude", "fake-opus", "high")
        started = Event()
        release = Event()
        self.addCleanup(release.set)

        def respond(turn):
            if turn.member["id"] == member["id"]:
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Timed out waiting for removal")
                return "Reply arrived after removal", "removed-session"
            return self.harness.silent(turn)

        self.harness.respond = respond
        self.room.send("Start the review.", "start-review")
        self.assertTrue(started.wait(5))
        running = next(job for job in self.room.state()["jobs"] if job["member"] == member["id"])
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["activity"], "Fake harness is processing the delivery")
        self.room.remove(member["id"])
        self.assertTrue(self.turns("Claude")[0].context.cancel.is_set())
        release.set()
        self.idle()
        self.send("Continue the review.", "continue-review")

        self.assertEqual(len(self.turns("Claude")), 1)
        self.assertEqual([msg["name"] for msg in self.room.state()["messages"]], ["Sam", "Sam"])
        self.assertEqual(self.room.state()["jobs"], [])

    def test_stop_signals_runner_discards_late_result_and_preserves_newer_delivery(self):
        started = Event()
        stopped = Event()
        release = Event()
        self.addCleanup(release.set)
        self.send("First delivery", "first")

        def respond(turn):
            if turn.target["id"] == 2:
                started.set()
                if not turn.context.cancel.wait(5):
                    raise RuntimeError("Runner never received the stop signal")
                stopped.set()
                if not release.wait(5):
                    raise RuntimeError("Timed out waiting to release the runner")
                return "Late reply from a stopped turn", "late-session"
            return self.harness.silent(turn)

        self.harness.respond = respond
        self.room.send("Delivery to stop", "stop-target")
        self.assertTrue(started.wait(5))
        running, = self.room.state()["jobs"]
        self.room.send("Newer delivery", "newer")
        self.room.stop(running["id"])
        self.assertTrue(stopped.wait(5))
        queued, = self.room.state()["jobs"]
        self.assertEqual(queued["status"], "queued")
        release.set()
        self.idle()

        self.assertEqual([turn.target["id"] for turn in self.turns("Chief of Staff")], [1, 2, 3])
        self.assertEqual(self.turns("Chief of Staff")[-1].member["session"], f"native-{self.lead_id}")
        self.assertEqual(self.turns("Chief of Staff")[-1].member["cursor"], 1)
        self.assertEqual([message["name"] for message in self.room.state()["messages"]], ["Sam", "Sam", "Sam"])
        self.assertEqual(self.archive_messages(), self.room.state()["messages"])

    def test_stopped_turn_does_not_retry_or_redispatch_after_restart(self):
        started = Event()

        def respond(turn):
            started.set()
            if turn.context.cancel.wait(5):
                raise RuntimeError("Runner cancelled")
            raise RuntimeError("Runner never received the stop signal")

        self.harness.respond = respond
        self.room.send("Delivery to stop", "stop-target")
        self.assertTrue(started.wait(5))
        running, = self.room.state()["jobs"]
        self.room.stop(running["id"])
        self.idle()
        self.reopen()

        self.assertTrue(self.turns("Chief of Staff")[0].context.cancel.is_set())
        self.assertEqual(len(self.turns("Chief of Staff")), 1)
        self.assertEqual(self.room.state()["jobs"], [])
        with self.assertRaises(ValueError):
            self.room.retry(running["id"])

    def test_interrupted_turn_can_retry_after_reopening_with_its_original_cursor(self):
        self.send("Before the interruption", "first")

        def respond(turn):
            if turn.target["id"] == 2:
                raise RuntimeError("Fake harness stopped")
            return self.harness.silent(turn)

        self.harness.respond = respond
        self.send("Interrupted delivery", "interrupted")
        failed, = self.room.state()["jobs"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "Fake harness stopped")
        self.assertEqual(self.turns("Chief of Staff")[-1].member["cursor"], 1)

        with self.room.lock, self.room.db:
            self.room.db.execute("UPDATE jobs SET status='running' WHERE id=?", (failed["id"],))
        self.harness.respond = self.harness.silent
        self.reopen()
        interrupted, = self.room.state()["jobs"]
        self.assertEqual(interrupted["id"], failed["id"])
        self.assertEqual(interrupted["status"], "failed")
        self.assertIn("closed", interrupted["error"])
        self.assertEqual(len(self.turns("Chief of Staff")), 2)

        self.room.retry(interrupted["id"])
        self.idle()
        retried = self.turns("Chief of Staff")[-1]
        self.assertEqual(retried.target["id"], 2)
        self.assertEqual(retried.member["cursor"], 1)
        self.assertEqual(retried.member["session"], f"native-{self.lead_id}")
        self.send("After retry", "after")
        self.assertEqual(self.turns("Chief of Staff")[-1].member["cursor"], 2)
        self.assertEqual(self.room.state()["jobs"], [])

    def test_retry_and_new_messages_keep_deliveries_in_order_without_duplicates(self):
        def fail(_turn):
            raise RuntimeError("Fake harness stopped")

        self.harness.respond = fail
        self.send("Delivery to retry", "retry-target")
        failed, = self.room.state()["jobs"]
        self.send("While the turn is failed", "first-waiting")
        self.harness.respond = self.harness.silent
        with self.room.lock:
            self.room.retry(failed["id"])
            self.room.send("While the retry is queued", "second-waiting")
        self.idle()

        self.assertEqual([turn.target["id"] for turn in self.turns("Chief of Staff")], [1, 1, 3])
        self.assertEqual([turn.member["cursor"] for turn in self.turns("Chief of Staff")], [0, 0, 1])
        self.assertEqual(self.room.state()["jobs"], [])

    def test_a_new_message_cannot_replace_a_retried_delivery(self):
        def fail(_turn):
            raise RuntimeError("Fake harness stopped")

        self.harness.respond = fail
        self.send("Delivery to retry", "retry-target")
        failed, = self.room.state()["jobs"]
        self.harness.respond = self.harness.silent
        with self.room.lock:
            self.room.retry(failed["id"])
            self.room.send("While the retry is queued", "first-waiting")
        self.idle()

        self.assertEqual([turn.target["id"] for turn in self.turns("Chief of Staff")], [1, 1, 2])
        self.assertEqual([turn.member["cursor"] for turn in self.turns("Chief of Staff")], [0, 0, 1])

    def test_unknown_provider_fails_the_turn_instead_of_crashing(self):
        with self.room.lock, self.room.db:
            self.room.db.execute("UPDATE members SET provider='retired'")
        self.send("Hello", "hello")
        failed, = self.room.state()["jobs"]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("retired", failed["error"])

    def test_duplicate_send_is_idempotent_across_reopening(self):
        self.send("The original", "same-request")
        self.send("The duplicate", "same-request")
        self.reopen()
        self.send("Retried after restarting", "same-request")

        self.assertEqual([item["text"] for item in self.room.state()["messages"]], ["The original"])
        self.assertEqual(len(self.turns("Chief of Staff")), 1)
        self.assertEqual(self.room.state()["jobs"], [])

    def test_archive_contains_a_reply_even_when_no_other_member_remains(self):
        self.harness.respond = lambda turn: ("It's in the final cut.", "native-lead")
        self.send("Where is the establishing shot?", "solo-reply")

        expected = self.room.state()["messages"]
        self.assertEqual(len(expected), 2)
        self.assertEqual(self.archive_messages(), expected)

    def test_archive_never_includes_a_message_from_a_rolled_back_send(self):
        self.send("Committed first message", "first")
        with self.room.lock, self.room.db:
            self.room.db.execute("""CREATE TRIGGER reject_delivery BEFORE INSERT ON jobs
                BEGIN SELECT RAISE(ABORT, 'Delivery transaction must roll back'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.room.send("Rolled back message", "failed")
        self.assertEqual(self.archive_messages(), self.room.state()["messages"])

        with self.room.lock, self.room.db:
            self.room.db.execute("DROP TRIGGER reject_delivery")
        self.send("Committed next message", "next")
        archive = self.archive_messages()
        self.assertEqual([message["text"] for message in archive], ["Committed first message", "Committed next message"])
        self.assertEqual(archive, self.room.state()["messages"])

    def test_resumed_session_gets_a_short_tail_and_all_recent_unseen_messages(self):
        with self.room.lock:
            for number in range(1, 16):
                self.room.send(f"Earlier message {number}", f"earlier-{number}")
        self.idle()
        with self.room.lock:
            for number in range(16, 19):
                self.room.send(f"New message {number}", f"new-{number}")
        self.idle()

        _, resumed = self.turns("Chief of Staff")
        self.assertEqual(resumed.member["cursor"], 15)
        self.assertEqual([message["id"] for message in resumed.messages], list(range(6, 19)))
        self.assertNotIn("Read archived message IDs", resumed.prompt)

    def test_archive_recovers_older_context_when_native_session_is_lost(self):
        seed = "The first version used the café on rue Victor-Hugo."
        self.send(seed, "seed")
        archived_during_restore = []

        def respond(turn):
            if turn.member["session"]:
                raise SessionLost("No conversation found")
            archive = turn.workspace / "room-history.jsonl"
            archived_during_restore.extend(json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines())
            return SILENCE, "restored-session"

        self.harness.respond = respond
        with self.room.lock:
            for number in range(2, 107):
                self.room.send(f"Later message {number}", f"later-{number}")
        self.idle()

        initial, lost, restored = self.turns("Chief of Staff")
        self.assertEqual(initial.target["text"], seed)
        self.assertEqual(lost.member["session"], f"native-{self.lead_id}")
        self.assertEqual(restored.member["session"], "")
        self.assertNotIn(seed, restored.prompt)
        self.assertIn(str(self.room.archive), restored.prompt)
        first_inline = restored.messages[0]["id"]
        self.assertIn(f"Read archived message IDs 2 through {first_inline - 1}", lost.prompt)
        self.assertIn(f"Read archived message IDs 1 through {first_inline - 1}", restored.prompt)
        self.assertEqual([item["id"] for item in archived_during_restore], list(range(1, 107)))
        self.assertEqual(archived_during_restore[0]["text"], seed)

        self.harness.respond = self.harness.silent
        self.reopen()
        self.send("Continue after restoring the room.", "after-restore")
        resumed = self.turns("Chief of Staff")[-1]
        self.assertEqual(resumed.member["cursor"], 106)
        self.assertEqual(resumed.member["session"], "restored-session")
        self.assertEqual([msg["id"] for msg in self.archive_messages()], list(range(1, 108)))

    def test_other_errors_mentioning_sessions_are_not_treated_as_a_lost_session(self):
        self.send("First", "first")

        def fail(turn):
            raise RuntimeError("No conversation found")

        self.harness.respond = fail
        self.send("Second", "second")
        failed, = self.room.state()["jobs"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(self.turns("Chief of Staff")), 2)

    def test_rename_keeps_native_session_history_and_resolves_full_names(self):
        lead_id = self.lead_id
        self.room.rename(lead_id, "Mira 🌙 Patel")
        self.send("👋 @Mira 🌙 Patel, hello", "named")
        first = self.room.state()["messages"][0]
        self.assertEqual(first["mentions"], [dict(start=3, end=17, member=lead_id, name="Mira 🌙 Patel")])
        self.room.rename(lead_id, "Chief of Staff")
        self.room.add(dict(name="Mira 🌙 Patel", model="sonnet", provider="claude", effort="medium"))
        self.send("@Mira 🌙 Patel and @Chief of Staff", "both")
        state = self.room.state()
        later = state["messages"][-1]["mentions"]
        self.assertNotEqual(later[0]["member"], lead_id)
        self.assertEqual(later[1]["member"], lead_id)
        self.assertEqual(state["messages"][0], first)
        self.reopen()
        self.assertEqual(self.room.state()["members"][0]["aliases"], ["Mira 🌙 Patel"])
        self.assertEqual(self.turns("Chief of Staff")[-1].member["session"], f"native-{lead_id}")

    def test_ambiguous_name_can_be_tagged_by_picker_identity(self):
        self.room.add(dict(name="Chief of Staff", model="opus", provider="claude", effort="medium", role="Designer"))
        member = self.room.state()["members"][1]
        text = "@Chief of Staff can you help?"
        self.assertEqual(json.loads(self.room.tagged(text)), [])
        selected = dict(start=0, end=15, member=member["id"])
        self.room.rename(member["id"], "Mira")
        tags = json.loads(self.room.tagged(text, [selected]))
        self.assertEqual(tags[0]["member"], member["id"])
        self.assertEqual(self.room.state()["members"][1]["role"], "Designer")

    def test_mentions_inside_code_and_links_are_ignored(self):
        lead_id = self.lead_id
        text = "`@Chief of Staff` https://x.test/@Chief and @chief of staff"
        tags = json.loads(self.room.tagged(text))
        self.assertEqual([(t["member"], t["name"]) for t in tags], [(lead_id, "chief of staff")])

    def test_rename_during_work_uses_current_attribution(self):
        entered, release = Event(), Event()
        lead_id = self.lead_id

        def reply(turn):
            entered.set()
            release.wait(4)
            return "Ready", "same-session"
        self.harness.respond = reply
        self.room.send("Hello", "midturn")
        self.assertTrue(entered.wait(2))
        self.room.rename(lead_id, "Operations")
        release.set()
        self.idle()
        self.assertEqual(self.room.state()["messages"][-1]["name"], "Operations")
        self.assertEqual(self.room.state()["messages"][-1]["author"], lead_id)

    def test_provider_can_ask_the_human_and_receive_the_answer(self):
        answers = []
        lead_id = self.lead_id

        def respond(turn):
            choices = [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]
            answers.append(turn.context.ask("Allow?", "Run the build", choices, None))
            return SILENCE, "s"

        self.harness.respond = respond
        self.room.send("Build it", "ask")
        requests = []
        for _ in range(500):
            requests = self.room.state()["requests"]
            if requests:
                break
            Event().wait(0.01)
        request, = requests
        self.assertEqual(request["member"], lead_id)
        with self.assertRaises(ValueError):
            self.room.respond(request["id"], "maybe")
        self.room.respond(request["id"], "yes")
        self.idle()
        self.assertEqual(answers, ["yes"])


class LeadSeedingAndMigrationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.harness = FakeHarness()
        self.providers = FakeProviders(self.harness)

    def open(self, folder, settings):
        room = Room(folder, self.providers, settings)
        self.addCleanup(room.db.close)
        self.addCleanup(wait_until_idle, room)
        return room

    def test_fresh_room_starts_with_the_lead_from_settings_only_once(self):
        settings = FakeSettings(LEAD)
        room = self.open(self.root / "a", settings)
        member, = room.state()["members"]
        self.assertEqual({k: member[k] for k in ("name", "provider", "model", "effort", "lead")}, {**LEAD, "lead": True})
        room.remove(member["id"])
        wait_until_idle(room)
        room.db.close()
        settings.data["lead"] = dict(LEAD, name="Another lead")
        reopened = self.open(self.root / "a", settings)
        self.assertEqual(reopened.state()["members"], [])

    def test_fresh_room_without_a_lead_starts_empty(self):
        room = self.open(self.root / "b", FakeSettings(None))
        self.assertEqual(room.state()["members"], [])
        room.send("Anyone?", "lonely")
        wait_until_idle(room)
        self.assertEqual(room.state()["jobs"], [])
        self.assertEqual(self.harness.calls, [])

    def test_legacy_room_migrates_permanent_member_and_is_not_reseeded(self):
        folder = self.root / "legacy"
        folder.mkdir()
        with closing(sqlite3.connect(folder / "room.sqlite3")) as db:
            db.executescript(LEGACY_SCHEMA)
        room = self.open(folder, FakeSettings(dict(LEAD, name="Should not appear")))
        state = room.state()
        self.assertEqual([(m["id"], m["lead"]) for m in state["members"]], [("chief", True), ("helper", False)])
        self.assertNotIn("permanent", state["members"][0])
        self.assertEqual([(m["author"], m["name"]) for m in state["messages"]], [(HUMAN_ID, "Sam"), ("chief", "Chief")])
        self.assertEqual(state["messages"][0]["mentions"], [dict(start=6, end=12, member="chief", name="Chief")])
        archived = [json.loads(line) for line in room.archive.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(archived[0]["author"], HUMAN_ID)
        session_workspace = room.db.execute("SELECT session_workspace FROM members WHERE id='chief'").fetchone()[0]
        self.assertEqual(session_workspace, str(room.archive_workspace))
        room.remove("chief")
        self.assertEqual([m["id"] for m in room.state()["members"]], ["helper"])

        room.send("New message", "new")
        wait_until_idle(room)
        helper_turn, = self.harness.calls
        self.assertIn('"author": "human"', helper_turn.prompt)


if __name__ == "__main__":
    unittest.main()
