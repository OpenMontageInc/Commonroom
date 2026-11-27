"""HTTP routes only: map each request onto the App and its rooms."""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .core.projects import ProjectNotFound, ProjectUnavailable
from .core.room import MAX_ATTACHMENT_BYTES
from .platform.base import FolderPickerUnavailable

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"/": ("index.html", "text/html"), "/appearance.css": ("appearance.css", "text/css"),
          "/fonts/dm-sans.woff2": ("fonts/dm-sans.woff2", "font/woff2"),
          "/fonts/newsreader.woff2": ("fonts/newsreader.woff2", "font/woff2")}
PICKER_PROMPT = "Choose a project folder for Commonroom"
LOGO_PREFIX, LOGO_SUFFIX = "/api/providers/", "/logo.svg"


class UnsupportedContentType(ValueError):
    pass


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self):
        return self.server.app

    def reply(self, status, data, mime="application/json", cache="no-store", download=None):
        body = json.dumps(data, ensure_ascii=False).encode() if mime == "application/json" else data
        textual = mime.startswith("text/") or mime in {"application/json", "image/svg+xml"}
        self.send_response(status)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if textual else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if download is not None:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(download, safe=""))
            self.send_header("Content-Security-Policy", "default-src 'none'")
        if mime == "image/svg+xml":
            # An SVG opened on its own is a document: let it draw, never run scripts or load anything.
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"} and self.headers.get("Origin", f"http://{host}") == f"http://{host}" and self.headers.get("Sec-Fetch-Site", "none") != "cross-site"

    def room_route(self, path):
        if path.startswith("/api/projects/"):
            project_id, separator, action = path.removeprefix("/api/projects/").partition("/")
            if separator and project_id:
                return project_id, "/api/" + action
        return self.app.projects.default_id(), path

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise UnsupportedContentType("Expected JSON.")
        length = int(self.headers.get("Content-Length", 0))
        if length < 0:
            raise ValueError("Invalid content length.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def do_GET(self):
        if not self.trusted():
            return self.reply(403, {"error": "Use the local Commonroom address."})
        path = urlsplit(self.path).path
        if path in STATIC:
            name, mime = STATIC[path]
            if (WEB / name).is_file():
                return self.reply(200, (WEB / name).read_bytes(), mime)
            return self.reply(404, {"error": "Unknown address."})
        if path == "/api/health":
            return self.reply(200, {"service": "commonroom", "version": self.app.version})
        if path == "/api/setup":
            return self.reply(200, self.app.setup())
        if path == "/api/settings":
            return self.reply(200, self.app.settings.get())
        if path == "/api/providers":
            return self.reply(200, self.app.providers())
        if path.startswith(LOGO_PREFIX) and path.endswith(LOGO_SUFFIX):
            # Only a logo a registered provider declares is served; the id never becomes a path.
            logo = self.app.provider_logo(unquote(path[len(LOGO_PREFIX):-len(LOGO_SUFFIX)]))
            if logo is None:
                return self.reply(404, {"error": "Unknown address."})
            return self.reply(200, logo, "image/svg+xml", cache="private, max-age=3600")
        if path == "/api/projects":
            return self.reply(200, self.app.projects.snapshot())
        if path == "/api/models":
            return self.reply(200, self.app.models())
        project_id, path = self.room_route(path)
        if path.startswith("/api/attachments/"):
            try:
                room = self.app.projects.room(project_id)
            except ProjectNotFound as exc:
                return self.reply(404, {"error": str(exc)})
            except ProjectUnavailable as exc:
                return self.reply(409, {"error": str(exc)})
            stored = room.attachment_path(path.removeprefix("/api/attachments/"))
            if stored is None:
                return self.reply(404, {"error": "Unknown address."})
            record = room.describe_attachment(stored.parent.name)
            # Only plain raster images show inline. Everything else downloads and never runs here.
            if record["image"]:
                return self.reply(200, stored.read_bytes(), record["mime"], cache="private, max-age=86400")
            return self.reply(200, stored.read_bytes(), "application/octet-stream", download=record["name"])
        if path == "/api/state":
            try:
                after = int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0])
            except ValueError:
                return self.reply(400, {"error": "Invalid message number."})
            try:
                return self.reply(200, self.app.projects.room(project_id).state(after))
            except ProjectNotFound as exc:
                return self.reply(404, {"error": str(exc)})
            except ProjectUnavailable as exc:
                return self.reply(409, {"error": str(exc)})
        return self.reply(404, {"error": "Unknown address."})

    def mutate(self):
        if not self.trusted():
            return self.reply(403, {"error": "Use the local Commonroom address."})
        request = urlsplit(self.path)
        path = request.path
        if self.command == "POST" and path == "/api/shutdown":
            # Lets a newer desktop shell replace this service. Finish this reply first; turns still
            # running are recorded as interrupted when the next service opens the rooms.
            self.reply(200, {"ok": True})
            self.wfile.flush()
            threading.Thread(target=self.server.shutdown, name="commonroom-shutdown", daemon=True).start()
            return
        try:
            if self.command == "POST" and path == "/api/models/retry":
                provider = parse_qs(request.query).get("provider", [None])[0]
                self.app.catalog.refresh(provider, failed_only=True)
                return self.reply(200, self.app.models())
            if self.command == "POST" and path == "/api/setup/rescan":
                return self.reply(200, self.app.rescan())
            if self.command == "PUT" and path == "/api/settings":
                return self.reply(200, self.app.update_settings(self.json_body()))
            if self.command == "POST" and path == "/api/projects/browse":
                try:
                    return self.reply(200, {"path": self.app.platform.pick_folder(PICKER_PROMPT)})
                except FolderPickerUnavailable as exc:
                    return self.reply(409, {"error": str(exc)})
            if self.command == "POST" and path == "/api/projects":
                return self.reply(200, self.app.projects.add(self.json_body().get("path")))

            project_id, path = self.room_route(path)
            action, _, item_id = path.removeprefix("/api/").partition("/")
            if self.command == "DELETE" and action == "members" and item_id:
                self.app.projects.room(project_id).remove(item_id)
            elif self.command == "PATCH" and action == "members" and item_id:
                self.app.projects.room(project_id).update_member(item_id, self.json_body())
            elif self.command == "POST" and action == "stop" and item_id:
                self.app.projects.room(project_id).stop(int(item_id))
            elif self.command == "POST" and action == "retry" and item_id:
                self.app.projects.room(project_id).retry(int(item_id))
            elif self.command == "POST" and action == "requests" and item_id:
                data = self.json_body()
                self.app.projects.room(project_id).respond(item_id, data.get("choice"), data.get("text"))
            elif self.command == "POST" and path == "/api/attachments":
                length = int(self.headers.get("Content-Length", -1))
                if length < 0:
                    raise ValueError("Invalid content length.")
                if length > MAX_ATTACHMENT_BYTES:
                    self.close_connection = True
                    return self.reply(413, {"error": "Attachments can be up to 50 MB each."})
                name = unquote(self.headers.get("X-Filename", ""))
                record = self.app.projects.room(project_id).save_attachment(
                    name, self.headers.get("Content-Type", ""), self.rfile.read(length))
                return self.reply(200, record)
            elif self.command == "POST" and path == "/api/members":
                self.app.projects.room(project_id).add(self.json_body())
            elif self.command == "POST" and path == "/api/messages":
                data = self.json_body()
                self.app.projects.room(project_id).send(data.get("text"), data.get("request_id"), data.get("mentions"),
                                                        data.get("attachments"))
            else:
                return self.reply(404, {"error": "Unknown address."})
        except ProjectNotFound as exc:
            return self.reply(404, {"error": str(exc)})
        except ProjectUnavailable as exc:
            return self.reply(409, {"error": str(exc)})
        except sqlite3.IntegrityError:
            return self.reply(409, {"error": "A member already uses that name."})
        except UnsupportedContentType as exc:
            return self.reply(415, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            return self.reply(400, {"error": str(exc)})
        self.reply(200, {"ok": True})

    do_POST = do_DELETE = do_PATCH = do_PUT = mutate

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, platform=None):
        self.platform = platform
        super().__init__(address, Handler)

    def server_bind(self):
        if self.platform is None:
            return super().server_bind()
        # The platform decides the socket options, so a port another program holds fails to bind.
        self.platform.bind_listener(self.socket, self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


def make_server(app, port, host="127.0.0.1", platform=None):
    server = Server((host, port), platform)
    server.app = app
    return server
