"""Serve the selected project's task overview on the IPv4 loopback."""

from collections import OrderedDict
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from stat import S_ISREG
from string import Template
from threading import Lock, Thread
from time import monotonic
from urllib.parse import parse_qs
import webbrowser

from read_support import BaseChecker, Problem, bad, parse
from task_reader import TaskReader, card_source, full_id, task_directory, unfinished_attempts


POLL_MS = 2000
SHARE_SECONDS = 0.25
READ_ERRORS = (Problem, OSError, ValueError, RecursionError)
PAGE = Path(__file__).with_name("overview_page")
CLOSED_LABELS = {"cancelled": "Отменена", "rejected": "Отклонена", "replaced": "Заменена"}


def file_key(info):
    # ctime and inode also detect replacements with preserved size and mtime.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class OverviewFiles:
    """Cache only content. The reader still validates placements, fields and links."""

    def __init__(self):
        self.cards = {}
        self.descriptions = OrderedDict()

    def signature(self, path):
        info = path.lstat()
        if not S_ISREG(info.st_mode):
            bad(path, "ожидается обычный файл")
        return file_key(info)

    def unchanged(self, path, handle, key):
        info = os.fstat(handle.fileno())
        if not S_ISREG(info.st_mode) or file_key(info) != key:
            bad(path, "файл изменился во время чтения")

    def read(self, path, key):
        # Open each component without following links, including if it changes
        # between the reader's path validation and the content read.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(path.anchor, flags)
        try:
            for component in path.parts[1:-1]:
                child = os.open(component, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as file:
                self.unchanged(path, file, key)
                content = file.read()
                self.unchanged(path, file, key)
            return content
        finally:
            os.close(directory)

    def profile(self, path):
        return self.read(path, self.signature(path)).decode("utf-8")

    def card(self, path, where):
        key = self.signature(path)
        cached = self.cards.get(path)
        if cached is None or cached[0] != key:
            cached = self.cards[path] = (key, parse(self.read(path, key), where))
        return cached[1]

    def description(self, path):
        key = self.signature(path)
        cached = self.descriptions.get(path)
        if cached is None or cached[0] != key:
            cached = self.descriptions[path] = (key, self.read(path, key))
        self.descriptions.move_to_end(path)
        if len(self.descriptions) > 16:
            self.descriptions.popitem(last=False)
        return cached[1]


class OverviewReader(TaskReader):
    def __init__(self, root, source, files):
        self.files = files
        super().__init__(root, source)

    def _read_card(self, path, where):
        return self.files.card(path, where)

    def _read_description(self, entry, name):
        path = entry["path"].removesuffix("task.json") + name
        return self.files.description(self.file(path, "material"))


def task_row(entry):
    card = entry["card"]
    location, expected = entry["location"], entry["expected_location"]
    if expected == "done":
        status = "Завершена" if location == expected else "Принято, осталось перенести"
        kind = "done"
    elif expected == "closed":
        status = CLOSED_LABELS[card["decisions"][-1]["outcome"]]
        if location != expected:
            status += ", осталось перенести"
        kind = "closed"
    elif location != expected:
        status, kind = "Открыта, осталось вернуть", "open"
        if card.get("decisions") and card["decisions"][-1]["outcome"] == "reopened":
            status = "Повторно открыта, осталось вернуть"
    elif task_waiting(card):
        status, kind = "Ожидает", "waiting"
    else:
        status, kind = ("Открыта" if "assignment" in card else "Идея"), "open"
    return {
        "id": entry["id"], "title": card["title"], "parent": card.get("parent"),
        "status": status, "kind": kind, "location": location,
        "dependencies": list(dict.fromkeys(link["task"] for link in card.get("dependencies", []))),
    }


def task_detail(entry):
    card = entry["card"]
    assignment = card.get("assignment", {})
    detail = {
        "id": entry["id"], "goal": card["goal"], "waiting": task_waiting(card),
        "criteria": assignment.get("criteria", []),
        "description": entry.get("description_text", assignment.get("description")),
    }
    last = (card.get("decisions") or [{}])[-1]
    if last.get("outcome") in CLOSED_LABELS:
        detail["closure"] = {"reason": last["reason"], "replacement": last.get("replacement")}
    return detail


def task_waiting(card):
    if "waiting" in card:
        return card["waiting"]
    last = (card.get("decisions") or [{}])[-1]
    if last.get("outcome") in CLOSED_LABELS and (attempts := unfinished_attempts(card)):
        return {"remaining": "Завершить закрытие задачи.",
                "reason": "Исход попыток ещё не установлен: " + ", ".join(attempts),
                "next_step": "Редактору установить исход и освобождение удерживаемых ресурсов."}


def problem_text(error):
    if isinstance(error, Problem):
        return f"{error.where}: {error.message}"
    if isinstance(error, OSError) and error.filename:
        return f"{error.filename}: {error.strerror}"
    return str(error)


class Overview:
    """Publish one validated summary per pass, shared by requests to this run."""

    def __init__(self, root, profile):
        self.base = BaseChecker(root, None)
        self.profile = profile
        self.files = OverviewFiles()
        self.source = self._source()
        task_directory(self.base, self.source)
        self.lock = Lock()
        self.reader = None
        self.rows = None
        self.numbers = set()
        self.missing = set()
        self.version = 0
        self.error = None
        self.next_scan = 0

    @property
    def root(self):
        return self.base.root

    def _source(self):
        return card_source(self.base, self.profile, read_text=self.files.profile)

    def _scan(self):
        if self._source() != self.source:
            bad(self.profile, "источник задач изменился, перезапустите обзор из проекта")
        return OverviewReader(self.root, self.source, self.files)

    def _refresh(self, selected, requested_at, *, force=False):
        needs_confirmation = selected and selected not in self.numbers and selected not in self.missing
        if not force and requested_at < self.next_scan and (self.error or not needs_confirmation):
            return
        first_problem = None
        first_missing = set()
        changing = "Данные изменились при повторной сверке. Обзор повторит чтение."
        try:
            for attempt in range(2):
                try:
                    reader = self._scan()
                    entries = reader.list_tasks()
                    numbers = {entry["id"] for entry in entries}
                    missing = self.numbers - numbers
                    if selected and selected not in numbers:
                        missing.add(selected)
                    if attempt == 0 and missing:
                        first_missing = missing
                        continue
                except READ_ERRORS as error:
                    problem = problem_text(error)
                    if attempt == 0:
                        first_problem = problem
                        continue
                    self.error = problem if problem == first_problem else changing
                    return
                if missing - first_missing:
                    self.error = changing
                    return
                rows = [task_row(entry) for entry in entries]
                if rows != self.rows:
                    self.rows = rows
                    self.version += 1
                paths = {self.root / entry["path"] for entry in entries}
                self.files.cards = {path: self.files.cards[path] for path in paths}
                self.reader, self.numbers, self.missing = reader, numbers, missing
                self.error = None
                self.checked_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                return
        finally:
            self.next_scan = monotonic() + SHARE_SECONDS

    def _card_matches(self, entry):
        path = self.root / entry["path"]
        try:
            return self.files.signature(path) == self.files.cards[path][0]
        except (FileNotFoundError, NotADirectoryError):
            return False

    def _selected_detail(self, selected):
        entry = self.reader.find(selected)
        try:
            detail = self.reader.show(selected)
        except READ_ERRORS:
            if self._card_matches(entry):
                raise  # The known card is intact, so report the material's error.
            return None
        return task_detail(detail) if self._card_matches(entry) else None

    def data(self, selected, version):
        requested_at = monotonic()
        with self.lock:
            for force in (False, True):
                self._refresh(selected, requested_at, force=force)
                if self.error is not None:
                    return {"error": self.error}
                try:
                    task = self._selected_detail(selected) if selected else None
                except READ_ERRORS as error:
                    return {"error": problem_text(error)}
                if selected and task is None:
                    # The shared reader's selected path no longer matches.
                    # Refresh by full number before pairing detail with a version.
                    continue
                data = {"version": str(self.version), "checked_at": self.checked_at, "task": task}
                if version != data["version"]:
                    data["tasks"] = self.rows
                return data
            return {"error": f"{selected}: карточка изменилась во время повторной сверки"}


class Handler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *args):
        # Keep the session URL out of request logs.
        pass

    def respond(self, status, media, content):
        self.send_response(status)
        self.send_header("Content-Type", media + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; connect-src 'self'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The tab can cancel a request when the selection changes.

    def do_GET(self):
        if self.headers.get_all("Host") != [self.server.host]:
            self.respond(HTTPStatus.MISDIRECTED_REQUEST, "text/plain", b"Invalid host")
            return
        path, _, query = self.path.partition("?")
        if path == self.server.prefix + "data":
            try:
                params = parse_qs(query, max_num_fields=2, keep_blank_values=True)
                if params.keys() - {"selected", "version"} or any(len(v) != 1 for v in params.values()):
                    raise ValueError
                selected = params.get("selected", [None])[0]
                if selected is not None:
                    full_id(selected, "selected")
            except (ValueError, Problem):
                self.respond(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid parameters")
                return
            data = self.server.overview.data(selected, params.get("version", [None])[0])
            data["run"] = self.server.prefix.strip("/")
            self.respond(HTTPStatus.OK, "application/json", json.dumps(data).encode("ascii"))
        elif not query and path in self.server.assets:
            self.respond(HTTPStatus.OK, *self.server.assets[path])
        else:
            self.respond(HTTPStatus.NOT_FOUND, "text/plain", b"Overview session or page not found")


class OverviewServer(ThreadingHTTPServer):
    def __init__(self, overview, port=0):
        self.overview = overview
        self.prefix = "/" + secrets.token_urlsafe(24) + "/"
        page = Template((PAGE / "index.html").read_text(encoding="utf-8"))
        html = page.substitute(project=escape(overview.root.name), root=escape(str(overview.root)),
                               poll_ms=POLL_MS)
        self.assets = {
            self.prefix: ("text/html", html.encode("utf-8", errors="backslashreplace")),
            self.prefix + "style.css": ("text/css", (PAGE / "style.css").read_bytes()),
            self.prefix + "app.js": ("text/javascript", (PAGE / "app.js").read_bytes()),
        }
        super().__init__(("127.0.0.1", port), Handler)
        self.host = f"127.0.0.1:{self.server_port}"
        self.url = f"http://{self.host}{self.prefix}"


def open_browser(url):
    try:
        if webbrowser.open_new_tab(url):
            return
    except (OSError, webbrowser.Error):
        pass
    print("Откройте напечатанный адрес в браузере.", flush=True)


def serve(root, profile):
    try:
        with OverviewServer(Overview(root, profile)) as server:
            print(server.url, flush=True)
            Thread(target=open_browser, args=(server.url,), daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except READ_ERRORS as error:
        code = error.code if isinstance(error, Problem) else 1
        raise Problem(str(Path(root).absolute()), problem_text(error), code) from error
