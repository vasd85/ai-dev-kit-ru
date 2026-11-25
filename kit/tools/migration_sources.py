"""Inventory the selected legacy sources and compare their Git and working layers."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
from urllib.parse import unquote, urlsplit

from read_support import (
    BaseChecker, Problem, bad, equal, json_record, obj, parse, relative_path,
    sequence, string, strings, unsupported,
)
from records import contains
from task_reader import material_address, read_history, source_from_profile
from versions import CURRENT_FORMAT, LEGACY_FORMAT, record_format


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return (json_record(data, pretty=True) + "\n").encode("utf-8")


def read_json(path):
    return parse(Path(path).read_bytes(), str(path))


def read_review(path):
    data = Path(path).read_bytes()
    review = obj(parse(data, path), "review",
                 ["entry", "profile", "source", "editor", "scopes", "works"],
                 ["materials", "links", "rows", "exceptions"])
    strings(review["entry"], "review/entry", nonempty=True)
    string(review["profile"], "review/profile")
    string(review["editor"], "review/editor")
    source = obj(review["source"], "review/source", ["kind", "path", "diagnostic"])
    string(source["path"], "review/source/path")
    if source["kind"] not in ("state", "partial"):
        bad("review/source/kind", "ожидается state или partial")
    if source["kind"] == "state" and "rows" in review:
        bad("review/rows", "для файла состояния строки берутся из штатного читателя")
    diagnostic = obj(source["diagnostic"], "review/source/diagnostic", ["cwd", "command", "scope"])
    for key in ("command", "scope"):
        string(diagnostic[key], "review/source/diagnostic/" + key)
    for key in ("works", "scopes"):
        if not isinstance(review[key], dict):
            bad("review/" + key, "ожидается объект")
    for scope, choice in review["scopes"].items():
        w = "review/scopes/" + scope
        obj(choice, w, ["source", "continuation", "next_step"], ["editor"])
        string(choice["source"], w + "/source")
    for old, work in review["works"].items():
        w = "review/works/" + old
        obj(work, w, ["new", "owner", "meaning", "next_step", "grounds"],
            ["history", "materials", "waiting", "accept", "proposal"])
        for key in ("owner", "meaning", "next_step"):
            string(work[key], w + "/" + key)
        if not isinstance(work.get("history", {}), dict):
            bad(w + "/history", "ожидается объект именованных исторических адресов")
        if "accept" in work:
            spec = obj(work["accept"], w + "/accept", ["decision", "evidence", "observed_at", "decided_at"],
                       ["dates", "current_decision"] if source["kind"] == "partial" else ["dates"])
            if spec.get("current_decision") is not None:
                string(spec["current_decision"], w + "/accept/current_decision")
    return review, digest(data)


class LegacySources:
    def __init__(self, base, review, state_checker):
        self.base, self.review = base, review
        self.name = review["source"]["path"]
        self.files = set(review["entry"]) | {review["profile"], self.name}
        base.file(review["profile"], "profile")
        for name in self.files:
            base.file(name, "действующий вход")
        cwd = base.local(review["source"]["diagnostic"]["cwd"], "diagnostic/cwd")
        if not cwd.is_dir():
            bad("diagnostic/cwd", "нужна рабочая папка штатной команды")
        self.states, self.works, self.checker = {}, {}, None
        self.version = self.label = None
        self.diagnostic = {"code": None, "state": "частичный источник проверяет редактор"}
        self.stopped = False
        self.selection = "entry/profile"
        text = base.file(review["profile"], "profile").read_text(encoding="utf-8")
        marker = None
        if any(line.strip().startswith("<!-- ai-kit:task-source") for line in text.splitlines()):
            try:
                marker = source_from_profile(base, review["profile"])
            except Problem as error:
                raise Problem(error.where, error.message + "; сверьте запись профиля "
                              "с прежним источником и совместимым читателем", error.code) from error
            equal(review["source"]["kind"], "state", "profile/source")
            equal(self.name, marker[1], "profile/source")
            self.selection = "ai-kit:task-source"
        if review["source"]["kind"] == "state":
            self.read_state(state_checker, marker)
        else:
            self.read_partial()
        equal(set(self.works), set(review["works"]),
              "соответствия исходных работ (источник и редакторская сверка)")
        equal(set(self.states), {value["source"] for value in review["scopes"].values()},
              "соответствия областей")
        self.history = self.read_history()
        self.current_decisions = self.read_current_decisions()
        self.links = self.read_links()
        self.files.update(strings(review.get("materials", []), "review/materials"))
        for work in review["works"].values():
            self.files.update(strings(work.get("materials", []), "review/works/materials"))

    def read_current_decisions(self):
        current = {}
        if self.version is not None:
            return current
        for old, choice in self.review["works"].items():
            if "accept" not in choice:
                continue
            spec = choice["accept"]
            if "current_decision" in spec:
                name = spec["current_decision"]
            else:
                # An older review may only name the historical file. A live copy
                # there needs review before it can be ignored as superseded.
                key = string(spec["decision"], old + "/accept/decision")
                saved_name = self.history[old].get(key, {}).get("address", {}).get("path")
                name = saved_name if saved_name and self.base.local(
                    saved_name, old + "/decision", must_exist=False).is_file() else None
            if name is not None:
                self.base.file(name, old + "/current_decision")
                current[old] = name
                self.files.add(name)
        return current

    def read_state(self, state_checker, marker):
        data = parse(self.base.file(self.name, "state").read_bytes(), self.name)
        self.label = data.get("format_version") if isinstance(data, dict) else None
        self.version = record_format(self.label)
        if self.version not in (CURRENT_FORMAT, LEGACY_FORMAT):
            unsupported(self.name + "/format_version",
                        f"прежний формат {self.label!r} не распознан; установите редакцию "
                        "источника и совместимый читатель до подготовки")
        if marker:
            equal(self.version, marker[0], "profile/source/format_version")
        self.checker = checker = state_checker(str(self.base.root), self.name, self.version)
        try:
            status = checker.check()
            self.diagnostic = {"code": 0, "state": status, "infos": checker.infos}
        except Problem as error:
            task = data.get("task")
            attempt = task.get("attempt") if isinstance(task, dict) else None
            if not (self.version == LEGACY_FORMAT and error.code == 2 and error.where == "task/status"
                    and isinstance(attempt, dict) and task.get("status") == "paused"
                    and attempt.get("phase") == "stopped"):
                raise Problem(error.where, error.message + "; разберите действующий источник "
                              "и повторите инвентаризацию", error.code) from error
            self.stopped = True
            self.diagnostic = {"code": 2, "where": error.where, "state": error.message}
        if self.version == CURRENT_FORMAT:
            for name, state in checker.states.items():
                self.states[name] = {key: value for key, value in state.items() if key != "works"}
            for old, (work, _, name, _) in checker.works.items():
                self.works[old] = (name, {k: v for k, v in work.items() if k != "_assignment"},
                                   work.get("_assignment"))
            self.files.update(path.relative_to(self.base.root).as_posix() for path in checker.sources)
            self.files.update(state["profile"] for state in checker.states.values())
            self.files.update(item["source"] for item in checker.decisions.values())
            self.files.update(limit["source"]["path"] for limit, _, _ in checker.limits.values())
            for result, *_ in checker.results.values():
                self.files.update(result["artifacts"])
        else:
            self.states[self.name] = {key: value for key, value in data.items() if key != "task"}
            task = data["task"]
            old = string(task.get("id"), self.name + "/task/id")
            assignment = task.get("assignment")
            if self.stopped:
                # This is a human-readable stopped record outside the old diagnostic.
                # Use only the saved fields needed by the card, with no executable handoff.
                if not isinstance(assignment, dict):
                    bad(self.name + "/assignment", "для разбора нужно прежнее поручение")
                equal(assignment.get("acceptor"), data["coordinator"], self.name + "/assignment/acceptor")
                for key in ("id", "executor"):
                    string(task["attempt"].get(key), self.name + "/attempt/" + key)
                equal(task["attempt"].get("assignment_revision"), assignment.get("revision"),
                      self.name + "/attempt/assignment_revision")
                continuation = data.get("continuation", {})
                if not isinstance(continuation, dict):
                    bad(self.name, "установите точку продолжения остановленной попытки")
            self.works[old] = (self.name, task, assignment)
            self.files.add(data["profile"])
            self.files.update(task.get("result", {}).get("artifacts", []))
        for _, _, assignment in self.works.values():
            if assignment:
                self.files.update(strings(assignment.get("inputs"), "assignment/inputs", nonempty=True))

    def read_partial(self):
        lines = self.base.file(self.name, "source").read_text(encoding="utf-8").splitlines()
        for row in sequence(self.review.get("rows"), "review/rows"):
            obj(row, "review/rows", ["id", "line", "text", "status", "by", "date",
                                    "title", "goal", "criteria"], ["dependencies"])
            for key in ("id", "text", "status", "by", "date", "title", "goal"):
                string(row[key], "review/rows/" + key)
            for criterion in sequence(row["criteria"], "review/rows/criteria", nonempty=True):
                obj(criterion, "review/rows/criteria", ["id", "expectation"])
                for key, value in criterion.items():
                    string(value, "review/rows/criteria/" + key)
            line = row["line"]
            if type(line) is not int or not 1 <= line <= len(lines):
                bad(self.name, "нет заявленной строки " + str(line))
            equal(lines[line - 1], row["text"], f"{self.name}:{line}")
            if row["id"] in self.works or any(work[1]["line"] == line for work in self.works.values()):
                bad(self.name, "повтор номера или строки: " + row["id"])
            self.works[row["id"]] = (self.name, row, None)
        self.states[self.name] = {"scope": "partial", "source": self.name}

    def read_history(self):
        history = {}
        for old, work in self.review["works"].items():
            history[old] = {}
            for key, address in work.get("history", {}).items():
                w = old + "/history/" + key
                material_address(address, w)
                if "commit" not in address:
                    bad(w, "нужны полный коммит и путь на момент события")
                try:
                    value = read_history(self.base, address)
                except Problem as error:
                    bad(w, f"история {address['commit']}:{address['path']} недоступна; "
                        f"восстановите исходный объект или уточните адрес ({error.message})")
                content = value if isinstance(value, bytes) else encoded(value)
                history[old][key] = {"address": address, "sha256": digest(content),
                                     "file_sha256": digest(self.base.saved_file(
                                         address["commit"], address["path"]))}
        return history

    def read_links(self):
        links = []
        for link in sequence(self.review.get("links", []), "review/links"):
            obj(link, "review/links", ["document", "link", "work"])
            where = link["document"]
            literal = string(link["link"], where)
            string(link["work"], where + "/work")
            if link["work"] not in self.works:
                bad(where, "ссылка сопоставлена с неизвестной работой")
            text = self.base.file(where, "управляемый документ").read_text(encoding="utf-8")
            parsed = urlsplit(literal)
            if (parsed.scheme or parsed.netloc or parsed.query or parsed.path.startswith("/") or
                    not (parsed.path or parsed.fragment) or f"]({literal})" not in text):
                bad(where, "нужна существующая прямая Markdown-ссылка на локальный файл: " + literal)
            target = (posixpath.normpath(posixpath.join(posixpath.dirname(where), unquote(parsed.path)))
                      if parsed.path else where)
            self.base.file(target, where + " → " + literal)
            self.files.update((where, target))
            links.append({**link, "target": target, "new": self.review["works"][link["work"]]["new"]})
        return links


class GitLayers(BaseChecker):
    """Read bytes and object IDs without invoking a clean filter or refreshing the index."""

    def __init__(self, root, exclude=None):
        super().__init__(root, None)
        self.repository()
        self.head = self.git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
        self.excluded = exclude
        self.blobs = {}
        self.tree_entries = {name: [part.decode("ascii") for part in entry]
                             for name, entry in self.tree(self.head).items() if self.included(name)}
        self.index = {}
        self.excluded_conflicts = {}
        for entry in self.git("ls-files", "--stage", "-z").split(b"\0")[:-1]:
            metadata, raw_path = entry.split(b"\t", 1)
            name = os.fsdecode(raw_path)
            if self.included(name):
                self.index.setdefault(name, []).append(metadata.decode("ascii").split())
            elif metadata.rsplit(b" ", 1)[-1] != b"0":
                self.excluded_conflicts.setdefault(name, []).append(metadata.decode("ascii").split())
        self.meta = {}
        for name in ("config", "info/attributes", "info/exclude", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                     "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer", "index.lock"):
            path = self.root / ".git" / name
            self.meta[name] = "directory" if path.is_dir() else digest(path.read_bytes()) if path.is_file() else None

    def included(self, name):
        return self.excluded is None or not contains(self.excluded, name)

    def blob(self, entry, index=False):
        mode, oid = (entry[0], entry[1 if index else 2])
        if mode not in ("100644", "100755"):
            return None
        if oid not in self.blobs:
            self.blobs[oid] = digest(self.git("cat-file", "blob", oid))
        return self.blobs[oid]

    def selected_files(self, paths):
        names = set()
        for name in paths:
            path = self.local(name, "snapshot", must_exist=False)
            if path == self.root:
                bad(name, "укажите отдельные файлы или ограниченную папку")
            if path.is_dir():
                for child in path.rglob("*"):
                    rel = child.relative_to(self.root).as_posix()
                    inspected = self.local(rel, name)
                    if not inspected.is_dir():
                        names.add(rel)
            else:
                names.add(name)
            names.update(n for n in self.tree_entries.keys() | self.index.keys()
                         if contains(name, n))
        for name in self.tree_entries.keys() | self.index.keys():
            head = self.tree_entries.get(name)
            staged = self.index.get(name)
            expected = [[head[0], head[2], "0"]] if head else None
            if staged != expected:
                names.add(name)
        ignored = set()
        if names:
            paths = [":(literal)" + name for name in sorted(names)]
            for start in range(0, len(paths), 128):
                data = self.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
                                *paths[start:start + 128])
                ignored.update(os.fsdecode(raw) for raw in data.split(b"\0")[:-1])
        files = {}
        for name in sorted(names):
            path = self.local(name, "snapshot", must_exist=False)
            if path.exists() and not path.is_file():
                bad(name, "в снимке нет обычного файла; разберите тип объекта")
            work = ({"sha256": digest(path.read_bytes()), "executable": bool(path.stat().st_mode & 0o111)}
                    if path.exists() else None)
            head = self.tree_entries.get(name)
            staged = self.index.get(name, [])
            files[name] = {"work": work, "head": head, "head_sha256": self.blob(head) if head else None,
                           "index": staged, "index_sha256": [self.blob(item, index=True) for item in staged],
                           "hidden": any(part.startswith(".") for part in PurePosixPath(name).parts),
                           "ignored": name in ignored}
        return files

    def snapshot(self, paths):
        return {"head": self.head, "tree_sha256": digest(encoded(dict(sorted(self.tree_entries.items())))),
                "index_entries_sha256": digest(encoded(dict(sorted(self.index.items())))),
                "index_file_sha256": digest((self.root / ".git/index").read_bytes()),
                "git_meta": self.meta, "files": self.selected_files(paths)}


def inventory(layers, review, review_hash, sources):
    return {"captured_at": datetime.now(timezone.utc).isoformat(), "review_sha256": review_hash,
            "entry": review["entry"], "profile": review["profile"], "source": review["source"],
            "selection": sources.selection, "source_label": sources.label, "format": sources.version,
            "old_diagnostic": sources.diagnostic, "paths": sorted(sources.files),
            "history": sources.history, "links": sources.links, "git": layers.snapshot(sources.files)}


def changes(expected, actual, copy=False):
    paths = sorted(expected["files"].keys() | actual["files"].keys())
    changed = [{"path": path, "layers": [key for key in
                ("work", "head", "index", "ignored") if expected["files"].get(path, {}).get(key) !=
                actual["files"].get(path, {}).get(key)]}
               for path in paths if expected["files"].get(path) != actual["files"].get(path)]
    keys = ("tree_sha256", "index_entries_sha256", "git_meta")
    if not copy:
        keys += ("head", "index_file_sha256")
    changed.extend({"path": ".git", "layers": [key]} for key in keys if expected[key] != actual[key])
    return changed
