"""One room: the shared transcript, its members and the delivery of every message to them."""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..providers.base import SessionLost, TurnContext, TurnRequest
from .identity import HUMAN_ID, SILENCE
from .mentions import tag_mentions
from .prompt import CONTEXT_MESSAGES, CONTEXT_TAIL, turn_prompt


def now():
    return datetime.now(timezone.utc).isoformat()


class Room:
    """``providers`` offers ``get(id) -> Provider`` and ``ids()``; ``settings`` offers ``get() -> dict``."""

    def __init__(self, folder, providers, settings, project_path=None):
        project = Path(project_path).expanduser().resolve() if project_path is not None else None
        if project is not None and not project.is_dir():
            raise FileNotFoundError(f"Project folder does not exist: {project}")
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.archive_workspace = self.folder / "workspace"
        self.archive_workspace.mkdir(exist_ok=True)
        self.archive = self.archive_workspace / "room-history.jsonl"
        self.pending_requests = {}
        self.providers = providers
        self.settings = settings
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.folder / "room.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.project_path = self.bind_project(project)
        except (OSError, ValueError, sqlite3.Error):
            self.db.close()
            raise
        self.workspace = self.project_path or self.archive_workspace
        fresh = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='members'").fetchone() is None
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY,name TEXT,provider TEXT,model TEXT,effort TEXT,
            lead INTEGER DEFAULT 0,active INTEGER DEFAULT 1,session TEXT DEFAULT '',cursor INTEGER DEFAULT 0);
          DROP INDEX IF EXISTS active_names;
          CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,author TEXT,name TEXT,text TEXT,created TEXT,request_id TEXT UNIQUE);
          CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,member TEXT,target INTEGER,status TEXT,activity TEXT DEFAULT '',error TEXT DEFAULT '');
          CREATE INDEX IF NOT EXISTS work ON jobs(member,status);
          UPDATE jobs SET status='failed',error='Commonroom closed during this turn. Retry to continue.' WHERE status='running';
        """)
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("CREATE TABLE IF NOT EXISTS aliases(member TEXT,name TEXT,PRIMARY KEY(member,name))")
            member_columns = {r[1] for r in self.db.execute("PRAGMA table_info(members)")}
            if "role" not in member_columns:
                self.db.execute("ALTER TABLE members ADD COLUMN role TEXT NOT NULL DEFAULT ''")
            if "session_workspace" not in member_columns:
                self.db.execute("ALTER TABLE members ADD COLUMN session_workspace TEXT NOT NULL DEFAULT ''")
                self.db.execute("UPDATE members SET session_workspace=? WHERE session<>''", (str(self.archive_workspace),))
            if "lead" not in member_columns:
                # Rooms before the refactor had one unremovable ("permanent") member: it becomes the lead.
                self.db.execute("ALTER TABLE members ADD COLUMN lead INTEGER NOT NULL DEFAULT 0")
                if "permanent" in member_columns:
                    self.db.execute("UPDATE members SET lead=1 WHERE permanent=1")
            if "mentions" not in {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}:
                self.db.execute("ALTER TABLE messages ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]'")
                for row in self.rows("SELECT id,text FROM messages WHERE instr(text,'@')>0"):
                    self.db.execute("UPDATE messages SET mentions=? WHERE id=?", (self.tagged(row["text"]), row["id"]))
            lead = settings.get().get("lead") if fresh else None
            if lead:
                self.db.execute("INSERT INTO members(id,name,provider,model,effort,role,lead) VALUES(?,?,?,?,?,'',1)",
                                (uuid.uuid4().hex, lead["name"], lead["provider"], lead["model"], lead.get("effort") or ""))

        self.workers = set()
        self.cancellations = {}
        self.refresh_archive()
        with self.lock:
            for row in self.rows("SELECT id FROM members WHERE active=1"):
                self.wake(row["id"])

    def bind_project(self, requested):
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS room_project(id INTEGER PRIMARY KEY CHECK(id=1),path TEXT NOT NULL)")
            stored = self.db.execute("SELECT path FROM room_project WHERE id=1").fetchone()
            if stored:
                project = Path(stored["path"])
                if requested is not None and project != requested:
                    raise ValueError(f"This room belongs to a different project: {project}")
                if not project.is_dir():
                    raise FileNotFoundError(f"Project folder does not exist: {project}")
                if project.resolve() != project:
                    raise ValueError(f"This room's project folder now points elsewhere: {project}")
                return project
            if requested is not None:
                self.db.execute("INSERT INTO room_project(id,path) VALUES(1,?)", (str(requested),))
            return requested

    def human(self):
        return {"id": HUMAN_ID, "name": self.settings.get()["displayName"]}

    def roster(self):
        members = self.rows("SELECT id,name,provider,model,effort,role,lead FROM members WHERE active=1 ORDER BY lead DESC,rowid")
        aliases = {}
        for row in self.db.execute("SELECT member,name FROM aliases ORDER BY rowid"):
            aliases.setdefault(row[0], []).append(row[1])
        for member in members:
            member["lead"] = bool(member["lead"])
            member["aliases"] = [a for a in aliases.get(member["id"], []) if a != member["name"]]
        return members

    def labels(self, member_id):
        return [r["name"] for r in self.rows("SELECT name FROM members WHERE id=? UNION SELECT name FROM aliases WHERE member=?", (member_id, member_id))]

    def tagged(self, text, selected=None):
        if "@" not in text:
            return "[]"
        return tag_mentions(text, self.roster(), self.labels, selected)

    def history(self, after):
        messages = self.rows("SELECT id,author,name,text,created,mentions FROM messages WHERE id>? ORDER BY id", (after,))
        for msg in messages:
            msg["mentions"] = json.loads(msg["mentions"])
        return messages

    @staticmethod
    def clean_name(name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Enter a name.")
        return name.strip()

    def _rename(self, member_id, old, name):
        if name != old:
            self.db.execute("INSERT OR IGNORE INTO aliases(member,name) VALUES(?,?)", (member_id, old))
            self.db.execute("UPDATE members SET name=? WHERE id=?", (name, member_id))

    def rename(self, member_id, name):
        self.update_member(member_id, {"name": name})

    def update_member(self, member_id, data):
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        changes = {}
        if "name" in data:
            changes["name"] = self.clean_name(data["name"])
        if "model" in data:
            if not isinstance(data["model"], str) or not data["model"].strip():
                raise ValueError("Choose a model.")
            changes["model"] = data["model"].strip()
        for key in ("effort", "role"):
            if key in data:
                if data[key] is not None and not isinstance(data[key], str):
                    raise ValueError(f"Enter a valid {key}.")
                changes[key] = (data[key] or "").strip()
        if not changes:
            raise ValueError("Nothing to change.")
        with self.lock, self.db:
            old = self.db.execute("SELECT name FROM members WHERE id=? AND active=1", (member_id,)).fetchone()
            if not old:
                raise ValueError("This member has left the room.")
            if "name" in changes:
                self._rename(member_id, old[0], changes.pop("name"))
            # Model and effort apply from the next turn; the saved session is kept.
            for key, value in changes.items():
                self.db.execute(f"UPDATE members SET {key}=? WHERE id=?", (value, member_id))

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def refresh_archive(self):
        temp = self.archive.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            for row in self.db.execute("SELECT id,name,author,text,created,mentions FROM messages ORDER BY id"):
                message = dict(row)
                message["mentions"] = json.loads(message["mentions"])
                stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        os.replace(temp, self.archive)
        self.archive_dirty = False

    def archive_message(self, mid):
        try:
            if self.archive_dirty or not self.archive.is_file():
                self.refresh_archive()
                return
            row = self.rows("SELECT id,name,author,text,created,mentions FROM messages WHERE id=?", (mid,))[0]
            row["mentions"] = json.loads(row["mentions"])
            with self.archive.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            # SQLite has committed. Repair the projection before the next delivery.
            self.archive_dirty = True

    def state(self, after=0):
        with self.lock:
            return dict(human=self.human(),
                        requests=[r["public"].copy() for r in self.pending_requests.values()],
                        members=self.roster(),
                        messages=self.history(after),
                        jobs=self.rows("""SELECT id,member,CASE status WHEN 'retrying' THEN 'queued' ELSE status END AS status,
                            activity,error FROM jobs WHERE status IN ('queued','retrying','running','failed') ORDER BY id"""))

    def add(self, data):
        name, provider, model, effort = (str(data.get(k) or "").strip() for k in ("name", "provider", "model", "effort"))
        if not all((name, model)) or provider not in set(self.providers.ids()):
            raise ValueError("Choose a member name, provider and model.")
        with self.lock, self.db:
            self.db.execute("INSERT INTO members(id,name,provider,model,effort,role) VALUES(?,?,?,?,?,?)",
                            (uuid.uuid4().hex, name, provider, model, effort, str(data.get("role") or "").strip()))

    def remove(self, member):
        with self.lock:
            with self.db:
                if not self.db.execute("UPDATE members SET active=0 WHERE active=1 AND id=?", (member,)).rowcount:
                    raise ValueError("This member cannot be removed.")
                removed = self.rows("""UPDATE jobs SET status='removed' WHERE member=?
                    AND status IN ('queued','retrying','running','failed') RETURNING id""", (member,))
            for job in removed:
                signal = self.cancellations.get(job["id"])
                if signal is not None:
                    signal.set()

    def enqueue(self, author, mid):
        for member in self.rows("SELECT id FROM members WHERE active=1 AND id<>?", (author,)):
            key = member["id"]
            if not self.db.execute("UPDATE jobs SET target=? WHERE id=(SELECT MAX(id) FROM jobs WHERE member=? AND status='queued')", (mid, key)).rowcount:
                self.db.execute("INSERT INTO jobs(member,target,status) VALUES(?,?,'queued')", (key, mid))
            self.wake(key)

    def send(self, text, request, mentions=None):
        if not isinstance(text, str) or not text.strip() or not isinstance(request, str) or not request:
            raise ValueError("A message and request ID are required.")
        name = self.settings.get()["displayName"]
        with self.lock:
            with self.db:
                cur = self.db.execute("INSERT OR IGNORE INTO messages(author,name,text,created,request_id,mentions) VALUES(?,?,?,?,?,?)",
                                      (HUMAN_ID, name, text, now(), request, self.tagged(text, mentions)))
                if cur.rowcount:
                    self.enqueue(HUMAN_ID, cur.lastrowid)
            if cur.rowcount:
                self.archive_message(cur.lastrowid)

    def wake(self, member):
        if member not in self.workers:
            self.workers.add(member)
            threading.Thread(target=self.worker, args=(member,), daemon=True).start()

    def retry(self, job):
        with self.lock, self.db:
            item = self.db.execute("SELECT member FROM jobs WHERE id=? AND status='failed'", (job,)).fetchone()
            if not item:
                raise ValueError("Turn is no longer awaiting retry.")
            self.db.execute("UPDATE jobs SET status='retrying',activity='',error='' WHERE id=?", (job,))
            self.wake(item[0])

    def stop(self, job):
        with self.lock:
            with self.db:
                changed = self.db.execute("UPDATE jobs SET status='stopped' WHERE id=? AND status IN ('running','failed')", (job,)).rowcount
                if not changed:
                    raise ValueError("Turn is no longer active.")
            signal = self.cancellations.get(job)
            if signal is not None:
                signal.set()
            member = self.db.execute("SELECT member FROM jobs WHERE id=?", (job,)).fetchone()[0]
            self.wake(member)

    def ask(self, member_id, cancel, title, detail, choices, withdrawn=None):
        key = uuid.uuid4().hex
        done = threading.Event()
        public = dict(id=key, member=member_id, title=title, detail=detail, choices=choices)
        entry = dict(public=public, done=done, cancel=cancel, withdrawn=withdrawn)
        with self.lock:
            if cancel.is_set():
                raise RuntimeError("Turn stopped.")
            self.pending_requests[key] = entry
        try:
            while not done.wait(0.2):
                if cancel.is_set() or (withdrawn and withdrawn.is_set()):
                    raise RuntimeError("Request closed.")
            return entry["answer"]
        finally:
            with self.lock:
                self.pending_requests.pop(key, None)

    def respond(self, key, choice, text=None):
        with self.lock:
            entry = self.pending_requests.get(key)
            if not entry or entry["done"].is_set() or entry["cancel"].is_set() or (entry["withdrawn"] and entry["withdrawn"].is_set()):
                raise ValueError("This request has already closed.")
            if isinstance(choice, list):
                options = entry["public"]["choices"]
                valid = {opt["id"] for opt in options} if any(opt.get("multi") for opt in options) else set()
                if not valid or not choice or any(not isinstance(x, str) or x not in valid for x in choice):
                    raise ValueError("Choose from the displayed options.")
                inputs = {opt["id"] for opt in options if opt.get("input")}
                if inputs.intersection(choice) and not isinstance(text, str):
                    raise ValueError("Enter your answer.")
                entry["answer"] = ", ".join(text if x in inputs else x for x in choice)
                entry["done"].set()
                return
            option = next((v for v in entry["public"]["choices"] if v["id"] == choice), None)
            if not option or (option.get("input") and not isinstance(text, str)):
                raise ValueError("Choose an available response.")
            entry["answer"] = text if option.get("input") else choice
            entry["done"].set()

    def prompt(self, member, target):
        roster = self.roster()
        identity = next(m for m in roster if m["id"] == member["id"])
        cursor = member["cursor"]
        first_new = cursor + 1 if member["session"] else 1
        recent = self.rows("""SELECT id,author,name,text,mentions FROM messages WHERE id BETWEEN ? AND ?
            ORDER BY id DESC LIMIT ?""", (max(1, first_new - CONTEXT_TAIL), target, CONTEXT_MESSAGES))
        return turn_prompt(identity=identity, roster=roster, human_name=self.settings.get()["displayName"],
                           cursor=cursor, first_new=first_new, target=target, recent=recent,
                           archive=self.archive, project_path=self.project_path)

    def request(self, member, prompt):
        return TurnRequest(member_id=member["id"], model=member["model"] or "", effort=member["effort"] or "",
                           prompt=prompt, workspace=self.workspace, session=member["session"] or "",
                           session_workspace=member["session_workspace"] or "")

    def worker(self, member_id):
        while True:
            with self.lock, self.db:
                jobs = self.rows("SELECT * FROM jobs WHERE member=? AND status IN ('queued','retrying','failed') ORDER BY id LIMIT 1", (member_id,))
                if not jobs or jobs[0]["status"] == "failed":
                    self.workers.discard(member_id)
                    return
                job = jobs[0]
                member = self.rows("SELECT * FROM members WHERE id=? AND active=1", (member_id,))[0]
                self.db.execute("UPDATE jobs SET status='running',activity='Reading the room' WHERE id=?", (job["id"],))
                cancel = threading.Event()
                self.cancellations[job["id"]] = cancel

            def remember_session(sid):
                if sid:
                    with self.lock, self.db:
                        self.db.execute("UPDATE members SET session=?,session_workspace=? WHERE id=? AND active=1", (sid, str(self.workspace), member_id))

            def activity(label):
                with self.lock, self.db:
                    self.db.execute("UPDATE jobs SET activity=? WHERE id=? AND status='running'", (str(label), job["id"]))

            def ask(title, detail, choices, withdrawn=None, turn_cancel=cancel):
                return self.ask(member_id, turn_cancel, title, detail, choices, withdrawn)

            context = TurnContext(cancel=cancel, activity=activity, remember_session=remember_session, ask=ask)
            try:
                provider = self.providers.get(member["provider"])
                with self.lock:
                    if cancel.is_set():
                        continue
                    if self.archive_dirty or not self.archive.is_file():
                        self.refresh_archive()
                    prompt = self.prompt(member, job["target"])
                try:
                    result = provider.run_turn(self.request(member, prompt), context)
                except SessionLost:
                    if cancel.is_set() or not member["session"]:
                        raise
                    activity("Restoring room history")
                    member["session"] = ""
                    member["session_workspace"] = ""
                    with self.lock:
                        if cancel.is_set():
                            continue
                        prompt = self.prompt(member, job["target"])
                    result = provider.run_turn(self.request(member, prompt), context)
                answer, session = result.text.strip(), result.session
                with self.lock:
                    mid = None
                    with self.db:
                        if cancel.is_set():
                            continue
                        self.db.execute("UPDATE members SET session=?,session_workspace=?,cursor=? WHERE id=?",
                                        (session, str(self.workspace) if session else "", job["target"], member_id))
                        self.db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
                        if answer and answer != SILENCE:
                            current = self.db.execute("SELECT name FROM members WHERE id=?", (member_id,)).fetchone()[0]
                            cur = self.db.execute("INSERT INTO messages(author,name,text,created,mentions) VALUES(?,?,?,?,?)",
                                                  (member_id, current, answer, now(), self.tagged(answer)))
                            mid = cur.lastrowid
                            self.enqueue(member_id, mid)
                    if mid is not None:
                        self.archive_message(mid)
            except Exception as exc:
                with self.lock, self.db:
                    self.db.execute("UPDATE jobs SET status='failed',error=? WHERE id=? AND status='running'", (str(exc), job["id"]))
            finally:
                with self.lock:
                    self.cancellations.pop(job["id"])
