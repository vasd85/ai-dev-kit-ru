"""Inspect and move a task bundle without changing Git or replacing a destination."""

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
from stat import S_IMODE, S_ISDIR, S_ISREG
import sys
from urllib.parse import unquote, urlsplit

from read_support import bad, equal, obj, parse, sequence, string, unsupported
from task_actions import TaskActions, transition_status
from task_reader import CLOSED_OUTCOMES, material_address, profile_selection, unfinished_attempts


@contextmanager
def open_directory(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def rename_exclusive(source_fd, source, target_fd, target):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation, flags = getattr(libc, "renameatx_np", None), 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        operation, flags = getattr(libc, "renameat2", None), 1  # RENAME_NOREPLACE
    else:
        operation = None
    if operation is None:
        unsupported("move", "система не предоставляет переименование без замещения")
    operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    operation.restype = ctypes.c_int
    if operation(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_hashes(path, info, git_hash):
    raw = hashlib.sha256()
    blob = hashlib.new(git_hash, f"blob {info.st_size}\0".encode("ascii"), usedforsecurity=False)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        if stamp(os.fstat(handle.fileno())) != stamp(info):
            bad(path, "файл изменился при осмотре; повторите сверку")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            raw.update(chunk)
            blob.update(chunk)
        if stamp(os.fstat(handle.fileno())) != stamp(info):
            bad(path, "файл изменился при осмотре; повторите сверку")
    return {"sha256": raw.hexdigest(), "blob": blob.hexdigest()}


class TaskMove(TaskActions):
    def __init__(self, root, source):
        self.card_hashes = {}
        self.observed = {}
        super().__init__(root, source)

    def _read_card(self, path, where):
        self.observed[path] = stamp(path.lstat())
        data = path.read_bytes()
        self.card_hashes[where] = hashlib.sha256(data).hexdigest()
        return parse(data, where)

    def inspect_move(self, number):
        entry = self.find(number)
        self.repository()
        card_path = PurePosixPath(entry["path"])
        source = card_path.parent
        destination = source.parent.with_name(entry["expected_location"]) / source.name
        commit = self.git("rev-parse", "--verify", "HEAD").strip().decode("ascii")
        files, paths = self.git_inventory(number, source.parent.parent, commit)
        folder = self.root / source
        identity = folder.lstat()
        self.work_inventory(folder, identity, "sha256" if len(commit) == 64 else "sha1", files)
        if files.get("task.json", {}).get("worktree", {}).get("sha256") != self.card_hashes[entry["path"]]:
            bad(card_path, "карточка изменилась при осмотре; повторите сверку")
        if stamp(folder.lstat()) != stamp(identity):
            bad(source, "папка изменилась при осмотре; повторите сверку")
        self.source_identity = stamp(identity)
        if destination != source and os.path.lexists(self.root / destination):
            bad(number, f"назначение занято: {destination}; исходная папка {source}")
        phase, step = self.move_phase(entry, paths, files)
        inspection = {
            "task": number, "phase": phase, "next_step": step,
            "source": str(source), "destination": str(destination),
            "card_paths": {"worktree": entry["path"], **paths}, "head_commit": commit,
            "files": files, "owners": {}, "links": None,
        }
        if "waiting" in entry["card"]:
            inspection["waiting"] = entry["card"]["waiting"]
        return inspection

    def git_inventory(self, number, scope, commit):
        files, cards = {}, {}
        for layer, command in (
                ("index", ("ls-files", "--stage", "-z")),
                ("head", ("ls-tree", "-rz", "--full-tree", commit))):
            cards[layer] = []
            for item in self.git(*command, "--", ":(literal)" + str(scope)).split(b"\0")[:-1]:
                metadata, name = item.split(b"\t", 1)
                name = os.fsdecode(name)
                address = self.task_file_at(name)
                if address is None or address[0] != number:
                    continue
                mode, second, third = metadata.decode("ascii").split()
                saved = {"path": name, "mode": mode, "blob": third if layer == "head" else second}
                if layer == "index":
                    saved["stage"] = int(third)
                if address[1] == "task.json" and name not in cards[layer]:
                    cards[layer].append(name)
                files.setdefault(address[1], {}).setdefault(layer, []).append(saved)
        return files, cards

    def work_inventory(self, folder, identity, git_hash, files):
        pending = [folder]
        while pending:
            path = pending.pop()
            name = path.relative_to(folder).as_posix()
            info = path.lstat()
            self.observed[path] = stamp(info)
            if info.st_dev != identity.st_dev:
                bad(path, "в комплекте есть другой том; перенос остановлен")
            if path.name.lower() == ".git":
                bad(path, "в комплекте есть служебная папка Git; выясните границы и владельца")
            item = {"mode": oct(S_IMODE(info.st_mode))}
            if S_ISDIR(info.st_mode):
                item["type"] = "directory"
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif S_ISREG(info.st_mode):
                item["type"] = "file"
                item.update(file_hashes(path, info, git_hash))
            else:
                bad(path, "нужен обычный файл или каталог; выясните назначение и владельца")
            files.setdefault(name, {})["worktree"] = item

    def move_phase(self, entry, paths, files):
        card = entry["card"]
        last = (card.get("decisions") or [{}])[-1]
        waiting = card.get("waiting")
        if entry["location"] != entry["expected_location"]:
            return "rename", "Редактору сверить полномочия, владельцев материалов и обязательные ссылки."
        closing = last.get("outcome") in CLOSED_OUTCOMES and entry["expected_location"] == "open"
        if not last or closing:
            if waiting:
                return "waiting", waiting["next_step"]
            if closing:
                return "waiting", "Установите исход попыток перед закрытием: " + ", ".join(unfinished_attempts(card))
            return "decision", "Редактору сверить результат по критериям и записать решение."
        card_versions = files["task.json"]
        if any(paths[layer] != [entry["path"]] or
               [v["blob"] for v in card_versions.get(layer, [])] != [card_versions["worktree"]["blob"]]
               for layer in ("index", "head")):
            return "git", "Папка на месте. Владельцу Git сверить и сохранить согласованные пути и байты."
        folder = PurePosixPath(entry["path"]).parent
        if any(not PurePosixPath(v["path"]).is_relative_to(folder) or v.get("stage", 0)
               for versions in files.values() for layer in ("index", "head")
               for v in versions.get(layer, [])):
            return "git", "Папка на месте. Владельцу Git разобрать прежние пути или конфликтные стадии."
        if waiting:
            return "waiting", waiting["next_step"]
        return "placed", "Карточка на месте в рабочей папке, индексе и HEAD. Сверьте нужные материалы."

    def check_editor(self, profile, entry, actor):
        self.profile = profile
        number = entry["id"]
        scope_id = number.split("/")[0]
        scope = self.scope_record(scope_id)
        equal(actor, scope["editor"]["session"], number + "/actor")
        return scope_id, scope

    def check_move(self, profile, entry, actor):
        number, card = entry["id"], entry["card"]
        files = [profile, *self.scopes.values(), self.scope_record("P")["resources"]]
        selection = profile_selection(self.file(profile, number).read_text(encoding="utf-8"), profile)
        if pause_path := selection.get("pause"):
            pause, _, step = transition_status(self, profile, selection, pause_path)
            if pause["state"] != "reviewed":
                bad(pause_path, step)
            files.append(pause_path)
        for name in files:
            path = self.file(name, number)
            self.observed[path] = stamp(path.lstat())
        scope_id, scope = self.check_editor(profile, entry, actor)
        last = card["decisions"][-1]
        equal(last["role"], scope["editor"]["role"], number + "/decision/role")
        self.check_conflicts(entry, None, {"write", "git"})
        book, grants = self.resource_grants()
        for key in ("write", "git"):
            if grant := grants.get(key):
                holder = self.find(grant["work"])
                if grant["holder"] == "P" and not any(
                        a["id"] == grant["attempt"] and a["revision"] == grant["revision"]
                        for a in holder["card"].get("attempts", [])):
                    bad(book, f"выясните держателя {key}: нет записанной попытки {grant['attempt']}")
        if last["outcome"] == "accepted":
            self.action(profile, number, "handoff", actor, last.get("attempt"))
            return
        self.check_decision_layers(entry)
        self.check_history(entry)
        self.read_source(last.get("source"), number + "/decision")
        assignment = card.get("assignment")
        if assignment:
            equal(assignment["acceptor"], scope["editor"]["role"], number + "/assignment/acceptor")
            if "revision" in last:
                equal(last["revision"], assignment["revision"], number + "/decision/revision")
            self.check_assignment_layers(entry, "HEAD")
        if scope_id != "P":
            self.check_mandate(scope_id, scope, assignment or {"write_scope": [], "stage": "prepare"})

    def check_review(self, review, inspection):
        number = inspection["task"]
        fields = inspection.keys() - {"waiting"}
        obj(review, "review", fields, ["waiting"])
        sealed = fields - {"owners", "links"}
        if any(review[key] != inspection[key] for key in sealed):
            bad(number, "файлы, решение или адреса Git изменились; обновите осмотр и сверку")
        for layer in ("index", "head"):
            paths = inspection["card_paths"][layer]
            if len(paths) > 1:
                bad(number, f"в слое {layer} несколько адресов карточки: " + " и ".join(paths))
        owners = obj(review["owners"], "review/owners", [], inspection["files"])
        for name, versions in inspection["files"].items():
            if any(v.get("stage", 0) for v in versions.get("index", [])):
                bad(name, "неразрешённые стадии индекса; владелец Git должен установить исход")
            if name != ".":
                if name not in owners:
                    bad(name, "владелец материала не указан; редактору выяснить происхождение всех версий")
                value = owners[name]
                if isinstance(value, dict):
                    obj(value, name + "/owner", versions)
                    for owner in value.values():
                        string(owner, name + "/owner")
                else:
                    string(value, name + "/owner")
        if review["links"] is None:
            bad(number, "редактору сверить обязательные ссылки и заполнить links")
        for link in sequence(review["links"], "review/links"):
            self.check_move_link(link, inspection)

    def check_move_link(self, link, inspection):
        where = "review/links"
        obj(link, where, ["document", "target", "owner"], ["relative", "next_step"])
        owner = string(link["owner"], where + "/owner")
        if "next_step" in link:
            bad(where, owner + " — " + string(link["next_step"], where + "/next_step"))
        document, origin = self.move_link_path(link["document"], inspection)
        target = link["target"]
        if isinstance(target, dict) and "commit" in target and "relative" not in link:
            self.read_material(target)
            return
        before, after = self.move_link_path(target, inspection)
        if "relative" not in link:
            if isinstance(target, str) and before != after:
                bad(document, f"{owner}: замените прежний путь {before} адресом по номеру задачи")
            return
        href = string(link["relative"], where + "/relative")
        ref = urlsplit(href)
        path = unquote(ref.path)
        if ref.scheme or ref.netloc or path.startswith("/") or ref.query:
            bad(document, "ожидается относительная ссылка внутри проекта")
        if href.encode("utf-8") not in (self.root / document).read_bytes():
            bad(document, f"{owner}: заявленная ссылка {href!r} отсутствует в документе")
        projected = posixpath.normpath(posixpath.join(str(origin.parent), path)) if path else str(origin)
        if projected != str(after):
            bad(document, f"{owner}: после переноса ссылка {href!r} ведёт в {projected}, "
                f"исправьте её для {after}")

    def move_link_path(self, address, inspection):
        if isinstance(address, dict):
            material_address(address, "review/links")
            if "task" not in address:
                bad("review/links", "нужен живой адрес документа или цели относительной ссылки")
            folder = PurePosixPath(self.find(address["task"])["path"]).parent
            address = str(folder / address["path"])
        current = PurePosixPath(self.name(self.file(address, "review/links")))
        path = self.root / current
        self.observed[path] = stamp(path.lstat())
        source = PurePosixPath(inspection["source"])
        after = current
        if current.is_relative_to(source):
            after = PurePosixPath(inspection["destination"]) / current.relative_to(source)
        return current, after

    def apply_move(self, profile, number, actor, review_file):
        entry = self.find(number)
        self.check_editor(profile, entry, actor)
        inspection = self.inspect_move(number)
        if inspection["phase"] in ("placed", "git"):
            return inspection
        if inspection["phase"] != "rename":
            bad(number, inspection["next_step"])
        review = parse(Path(review_file).read_bytes(), review_file)
        self.check_review(review, inspection)

        # Verify current evidence using a fresh view. The reviewed inventory
        # cannot carry over to changed bytes or another task path.
        fresh = TaskMove.from_profile(self.root, profile)
        current = fresh.inspect_move(number)
        if inspection != current or self.source_identity != fresh.source_identity:
            bad(number, "состояние изменилось при проверке; повторите осмотр")
        fresh.check_move(profile, fresh.find(number), actor)
        fresh.check_review(review, current)
        fresh.relocate(current)
        after = TaskMove.from_profile(self.root, profile).inspect_move(number)
        expected = str(PurePosixPath(inspection["destination"]) / "task.json")
        equal(after["card_paths"]["worktree"], expected, number + "/после переноса")
        equal(after["files"], inspection["files"], number + "/состав после переноса")
        return after

    def relocate(self, inspection):
        source = self.root / inspection["source"]
        target = self.root / inspection["destination"]
        try:
            with open_directory(source.parent) as old, open_directory(target.parent.parent) as scope:
                try:
                    os.mkdir(target.parent.name, dir_fd=scope)
                except FileExistsError:
                    pass
                with open_directory(target.parent) as new:
                    for path, before in self.observed.items():
                        if stamp(path.lstat()) != before:
                            bad(path, "файл или каталог изменился при сверке; повторите осмотр")
                    actual = os.stat(source.name, dir_fd=old, follow_symlinks=False)
                    equal(stamp(actual), self.source_identity, inspection["source"] + "/сверка папки")
                    if actual.st_dev != os.fstat(new).st_dev:
                        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
                    rename_exclusive(old, source.name, new, target.name)
        except OSError as error:
            where = f"{inspection['source']} → {inspection['destination']}"
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                bad(where, "назначение занято; выясните владельца, обе стороны сохранены")
            if error.errno == errno.EXDEV:
                bad(where, "разные файловые системы; перенос между томами запрещён")
            if error.errno in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL):
                unsupported(where, "файловая система не подтвердила переименование без замещения")
            bad(where, f"переименование не выполнено: {error.strerror or error}")
