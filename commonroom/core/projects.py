"""Keep one durable Commonroom room for each selected project folder."""
import os
import sqlite3
import threading
import uuid
from pathlib import Path

DEFAULT_ID = "default"


class ProjectNotFound(LookupError):
    pass


class ProjectUnavailable(RuntimeError):
    pass


def _canonical_folder(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("Enter a project folder path.")
    if isinstance(value, str):
        value = value.strip()
        # Pasted paths often arrive wrapped in quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
    try:
        folder = Path(value).expanduser()
        if not folder.is_absolute():
            raise ValueError("Enter the full path to the project folder.")
        folder = folder.resolve(strict=True)
        if folder.is_dir():
            return folder
    except (OSError, RuntimeError) as exc:
        raise ValueError("The project folder could not be opened.") from exc
    raise ValueError("Choose an existing project folder.")


def _project_info(row):
    path = Path(row["path"])
    return {"id": row["id"], "name": path.name or path.anchor, "path": str(path)}


def _require_original_folder(path):
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_dir() and os.path.normcase(str(resolved)) == os.path.normcase(str(path)):
            return
    except (OSError, RuntimeError) as exc:
        raise ProjectUnavailable(f"The folder for {path.name or path.anchor} is unavailable.") from exc
    raise ProjectUnavailable(f"The folder for {path.name or path.anchor} is unavailable.")


class ProjectRooms:
    """``room_factory(storage_folder, project_path) -> Room``.

    The room with id ``default`` lives directly in the data folder (where rooms
    from before multi-project support were stored); others under ``projects/<id>``.
    ``initial_project`` optionally registers a folder as the default project.
    """

    def __init__(self, data, platform, room_factory, initial_project=None):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.platform = platform
        self.room_factory = room_factory
        self.lock = threading.RLock()
        self._rooms = {}
        self.db = sqlite3.connect(self.data / "projects.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, canonical TEXT NOT NULL UNIQUE
            )""")
            if initial_project is not None and self.db.execute("SELECT 1 FROM projects WHERE id=?", (DEFAULT_ID,)).fetchone() is None:
                path = _canonical_folder(initial_project)
                self.db.execute("INSERT OR IGNORE INTO projects VALUES (?, ?, ?)",
                                (DEFAULT_ID, str(path), os.path.normcase(str(path))))
        for row in self.db.execute("SELECT id, path FROM projects ORDER BY rowid").fetchall():
            try:
                self._rooms[row["id"]] = self._open_room(row["id"], Path(row["path"]))
            except ProjectUnavailable:
                # A disconnected drive should not prevent other projects from opening.
                continue

    def _open_room(self, project_id, path):
        storage = self.data if project_id == DEFAULT_ID else self.data / "projects" / project_id
        _require_original_folder(path)
        try:
            return self.room_factory(storage, path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ProjectUnavailable(f"The folder for {path.name or path.anchor} is unavailable.") from exc
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise ProjectUnavailable(f"The saved room for {path.name or path.anchor} could not be opened.") from exc

    def rooms(self):
        with self.lock:
            return list(self._rooms.values())

    def snapshot(self):
        with self.lock:
            rows = self.db.execute("SELECT id, path FROM projects ORDER BY rowid").fetchall()
            return {"projects": [_project_info(row) for row in rows], "defaultId": self.default_id()}

    def default_id(self):
        with self.lock:
            row = self.db.execute("SELECT id FROM projects ORDER BY (id=?) DESC,rowid LIMIT 1", (DEFAULT_ID,)).fetchone()
            return row["id"] if row else None

    def room(self, project_id):
        with self.lock:
            if room := self._rooms.get(project_id):
                _require_original_folder(room.workspace)
                return room
            row = self.db.execute("SELECT path FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise ProjectNotFound("This project is not open in Commonroom.")
            room = self._open_room(project_id, Path(row["path"]))
            self._rooms[project_id] = room
            return room

    def add(self, value):
        path = _canonical_folder(value)
        canonical = os.path.normcase(str(path))
        with self.lock:
            existing = self.db.execute("SELECT id, path FROM projects WHERE canonical=?", (canonical,)).fetchone()
            if existing is None:
                for row in self.db.execute("SELECT id,path FROM projects ORDER BY rowid").fetchall():
                    if self.platform.same_folder(path, Path(row["path"])):
                        existing = row
                        break
            if existing is not None:
                self.room(existing["id"])
                return _project_info(existing)
            # An unregistered legacy transcript belongs to its original project.
            first = self.default_id() is None and not (self.data / "room.sqlite3").exists()
            project_id = DEFAULT_ID if first else uuid.uuid4().hex
            with self.db:
                self.db.execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, str(path), canonical))
                room = self._open_room(project_id, path)
            self._rooms[project_id] = room
            return _project_info({"id": project_id, "path": str(path)})
