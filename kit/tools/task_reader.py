"""Read the task folders selected by a product profile."""

from graphlib import CycleError, TopologicalSorter
from pathlib import PurePosixPath
import re

from read_support import (
    BaseChecker, bad, equal, obj, parse, relative_path, revision, sequence,
    string, strings, unsupported,
)
from versions import TASK_FORMAT, record_format


SOURCE_MARKER = "<!-- ai-kit:task-source "
SCOPE_ID = r"P|F-[a-z0-9]+(?:-[a-z0-9]+)*"
TASK_NUMBER = r"T-[0-9]{3,}"
TASK_FOLDER = rf"({TASK_NUMBER})(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?"
FULL_ID = rf"(?:{SCOPE_ID})/{TASK_NUMBER}"
LOCATIONS = ("open", "done", "closed")
CLOSED_OUTCOMES = ("rejected", "cancelled", "replaced")
LIVE_PHASES = ("assigned", "running", "unknown")


def profile_selection(text, profile, *, optional=False):
    markers = [line.strip() for line in text.splitlines()
               if line.strip().startswith("<!-- ai-kit:task-source")]
    if optional and not markers:
        return None
    if len(markers) != 1:
        bad(profile, f"нужна одна запись ai-kit:task-source; найдено {len(markers)}")
    marker = markers[0]
    if not marker.startswith(SOURCE_MARKER) or not marker.endswith(" -->"):
        bad(profile, "неверная запись ai-kit:task-source")
    source = obj(parse(marker[len(SOURCE_MARKER):-4], profile), profile,
                 ["format_version", "path"], ["prepared", "pause"])
    version = record_format(source["format_version"])
    if version is None:
        unsupported(profile + "/format_version", "неизвестный формат источника")
    relative_path(source["path"], profile + "/path")
    if "pause" in source:
        relative_path(source["pause"], profile + "/pause")
    if "prepared" in source:
        where = profile + "/prepared"
        if version != TASK_FORMAT:
            bad(where, "адрес подготовки относится к карточкам")
        prepared = obj(source["prepared"], where, ["profile", "commits"])
        relative_path(prepared["profile"], where + "/profile")
        equal(prepared["profile"], str(PurePosixPath(source["path"]).parent / "profile.md"),
              where + "/profile")
        for commit in strings(prepared["commits"], where + "/commits"):
            commit_id(commit, where + "/commits")
    return {**source, "format_version": version}


def source_from_profile(reader, profile, *, read_text=None):
    """Return (canonical format, relative path) from the named active profile."""
    path = reader.file(profile, "profile")
    text = path.read_text(encoding="utf-8") if read_text is None else read_text(path)
    selection = profile_selection(text, profile)
    return selection["format_version"], selection["path"]


def switch_pause(reader, profile, source, path):
    pause = obj(parse(reader.file(path, "pause").read_bytes(), path), path,
                ["state", "profile", "target", "actor", "owners", "attempts", "external",
                 "reserves", "index", "next_step"], ["evidence"])
    equal(pause["profile"], profile, path + "/profile")
    relative_path(pause["target"], path + "/target")
    if source is not None:
        equal(pause["target"] + "/tasks", source, path + "/target")
    for key in ("actor", "owners", "attempts", "external", "reserves", "index", "next_step"):
        string(pause[key], path + "/" + key)
    if pause["state"] not in ("paused", "reviewed", "stopped"):
        bad(path, "состояние выдачи должно быть paused, reviewed или stopped")
    if pause["state"] == "reviewed":
        strings(pause.get("evidence"), path + "/evidence", nonempty=True)
    return pause


def card_source(reader, profile, *, read_text=None):
    version, source = source_from_profile(reader, profile, read_text=read_text)
    if version != TASK_FORMAT:
        unsupported(profile, "для карточек нужен выбранный источник " + TASK_FORMAT)
    return source


def task_directory(reader, name, optional=False):
    if name == ".":
        bad("source", "укажите выделенный каталог учёта внутри проекта")
    path = reader.local(name, "source/" + name, must_exist=not optional)
    if optional and not path.exists():
        return None
    if not path.is_dir():
        bad(name, "ожидается каталог")
    return path


def full_id(value, where):
    if not isinstance(value, str) or not re.fullmatch(FULL_ID, value):
        bad(where, "нужен полный номер: например P/T-008 или F-search/T-008")
    return value


def commit_id(value, where):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        bad(where, "нужен полный идентификатор Git-коммита")


def git_address(value, where):
    obj(value, where, ["commit", "path"])
    material_address(value, where)


def mandate_address(value, where):
    obj(value, where, ["task", "basis"])
    if not full_id(value["task"], where + "/task").startswith("P/"):
        bad(where, "мандат функции выдаётся общей карточкой")
    git_address(value["basis"], where + "/basis")


def mandate_terms(assignment, where):
    mandate = obj(assignment.get("mandate"), where, ["scope", "role"], ["stages"])
    scope = mandate["scope"]
    if not isinstance(scope, str) or not re.fullmatch(SCOPE_ID, scope) or scope == "P":
        bad(where + "/scope", "нужен номер области функции")
    string(mandate["role"], where + "/role")
    stage = assignment.get("stage")
    if stage not in ("prepare", "implement"):
        bad(where + "/stage", "для мандата нужен явный общий этап prepare или implement")
    available = ["prepare"] if stage == "prepare" else ["prepare", "implement"]
    stages = strings(mandate.get("stages", available), where + "/stages", nonempty=True)
    if not set(stages) <= set(available):
        bad(where + "/stages", "местные этапы должны входить в разрешённые общим этапом")
    return mandate, stages


def resources(value, where, implicit_write=False):
    for key in strings(value, where):
        if key == "git" or (key == "write" and not implicit_write):
            continue
        if not re.fullmatch(r"external:[a-z0-9][a-z0-9._/-]*", key):
            bad(where, "нужен ресурс write, git или external:цель; write задаётся через write_scope")
    return value


def retained_resources(attempt):
    if attempt["phase"] in LIVE_PHASES:
        return attempt["resources"]
    if attempt.get("external_outcome") == "unknown":
        return [key for key in attempt["resources"] if key.startswith("external:")]
    return []


def unfinished_attempts(card):
    return [a["id"] for a in card.get("attempts", [])
            if a["phase"] in LIVE_PHASES or retained_resources(a)]


def review_requirements(card, attempt, where):
    if attempt["phase"] != "finished" or attempt.get("result", {}).get("outcome") != "submitted":
        bad(where, "нужен представленный результат подтверждённо завершившейся попытки")
    review = next((r for r in reversed(card.get("research_reviews", []))
                   if r["attempt"] == attempt["id"]), None)
    if review is None:
        if card["assignment"].get("research_review"):
            bad(where, "отсутствует заключение по исследованию")
        return None
    if review["result"] != attempt["result"]["source"]:
        bad(where, "заключение относится к другой редакции отчёта")
    if review["open_required"]:
        bad(where, "остались обязательные замечания: " + ", ".join(review["open_required"]))
    if not review["decisive_evidence"]:
        bad(where, "отсутствует решающее свидетельство исследования")
    return review


def material_address(value, where):
    if isinstance(value, dict) and "commit" in value:
        obj(value, where, ["commit", "path"], ["select"])
        commit_id(value["commit"], where + "/commit")
        if "select" in value:
            from records import saved_selector
            saved_selector(value["select"], where + "/select")
    else:
        obj(value, where, ["task", "path"])
        full_id(value["task"], where + "/task")
    if relative_path(value["path"], where + "/path").as_posix() == ".":
        bad(where + "/path", "нужен путь обычного файла")


def keyed_history(value, kind, where):
    material_address(value, where)
    select = value.get("select", {})
    if select.get("kind") != kind or (kind != "result" and "work" not in select):
        bad(where, f"нужен исторический адрес прежней записи {kind} с ключом")
    return select


def named(items, where, optional_id=False):
    result = {}
    for item in items:
        if optional_id and "id" not in item:
            continue
        key = string(item.get("id"), where + "/id")
        if key in result:
            bad(where, "повтор ключа " + key)
        result[key] = item
    return result


class TaskReader(BaseChecker):
    """One pass through all scopes. Construct again to observe later edits."""

    @classmethod
    def from_profile(cls, root, profile):
        base = BaseChecker(root, None)
        return cls(root, card_source(base, profile))

    def __init__(self, root, source):
        super().__init__(root, source)
        self.scopes = {}
        self._tasks = {}
        self._read_scope("P", self.directory(source))
        if features := self.directory(source + "/features", optional=True):
            for directory in sorted(features.iterdir()):
                if not directory.is_symlink() and directory.is_file():
                    continue
                if not re.fullmatch(SCOPE_ID, directory.name) or directory.name == "P":
                    bad(self.name(directory), "папка области функции должна иметь имя F-…")
                self._read_scope(directory.name, self.directory(self.name(directory)))
        self._check_links()

    def name(self, path):
        return path.relative_to(self.root).as_posix()

    def directory(self, name, optional=False):
        return task_directory(self, name, optional)

    def _read_card(self, path, where):
        return parse(path.read_bytes(), where)

    def _read_scope(self, scope, directory):
        self.scopes[scope] = self.name(self.file(self.name(directory / "scope.md"), scope))
        paths = {}
        for location in LOCATIONS:
            bucket = self.directory(self.name(directory / location), optional=True)
            if bucket is None:
                continue
            for folder in sorted(bucket.iterdir()):
                if not folder.is_symlink() and folder.is_file() and not folder.name.startswith("T-"):
                    continue
                match = re.fullmatch(TASK_FOLDER, folder.name)
                if match is None:
                    bad(self.name(folder), "ожидается папка с номером T-000 и необязательным суффиксом")
                where = self.name(folder / "task.json")
                if folder.is_symlink() or (folder / "task.json").is_symlink():
                    bad(where, "символические ссылки не поддерживаются; чтение запрещено")
                number = f"{scope}/{match[1]}"
                if number in paths:
                    bad(number, f"дубль полного номера: {paths[number][1]} и {where}; "
                        "проверьте занятые пути")
                paths[number] = (location, where)
        for number, (location, where) in paths.items():
            path = self.file(where, where)
            card = self._read_card(path, where)
            if not isinstance(card, dict):
                bad(where, "ожидается объект карточки")
            if card.get("format_version") != TASK_FORMAT:
                unsupported(where + "/format_version", "ожидается " + TASK_FORMAT)
            equal(card.get("id"), number.split("/")[1], where + "/id")
            expected = self._check_card(card, where)
            if location != expected:
                if "open" not in (location, expected):
                    bad(where, f"решение требует {expected}, карточка найдена в {location}")
                step = "Принято, осталось перенести" if expected == "done" else "остался перенос папки"
                self.infos.append(f"{number}: карточка в {location}, решение требует {expected}; {step}")
            self._tasks[number] = {"id": number, "path": where, "location": location,
                                   "expected_location": expected, "card": card}

    def _check_card(self, card, where):
        obj(card, where, ["format_version", "id", "title", "goal"],
            ["parent", "assignment", "waiting", "dependencies", "observations", "decisions", "origin",
             "attempts", "revisions", "research_reviews"])
        for key in ("title", "goal"):
            string(card[key], where + "/" + key)
        if "parent" in card:
            full_id(card["parent"], where + "/parent (один родитель)")
        for link in sequence(card.get("dependencies", []), where + "/dependencies"):
            obj(link, where + "/dependencies", ["task", "criterion", "acceptance"],
                ["basis", "compatibility", "confirmation"])
            full_id(link["task"], where + "/dependencies/task")
            string(link["criterion"], where + "/dependencies/criterion")
            if link["acceptance"] is not None:
                string(link["acceptance"], where + "/dependencies/acceptance")
            if "basis" in link:
                git_address(link["basis"], where + "/dependencies/basis")
            if "compatibility" in link:
                w = where + "/dependencies/compatibility"
                claim = obj(link["compatibility"], w, ["by", "commit", "reason"])
                commit_id(claim["commit"], w + "/commit")
                for key in ("by", "reason"):
                    string(claim[key], w + "/" + key)
            if "confirmation" in link:
                w = where + "/dependencies/confirmation"
                value = obj(link["confirmation"], w,
                            ["task", "assignment", "result", "acceptance", "criterion", "check"],
                            ["compatibility"])
                full_id(value["task"], w + "/task")
                string(value["check"], w + "/check")
                for kind in ("assignment", "result", "acceptance", "criterion"):
                    keyed_history(value[kind], kind, w + "/" + kind)
                if link["acceptance"] is None:
                    bad(w, "для выбранного подтверждения нужна приёмка обязательства")

        assignment = card.get("assignment")
        criteria = self._check_assignment(assignment, where) if "assignment" in card else {}
        attempts = self._check_attempts(card, where)
        if "waiting" in card:
            waiting = obj(card["waiting"], where + "/waiting", ["remaining", "reason", "next_step"])
            for key, value in waiting.items():
                string(value, where + "/waiting/" + key)
        if "origin" in card:
            origin = obj(card["origin"], where + "/origin", ["status", "source"])
            string(origin["status"], where + "/origin/status")
            self._check_source(origin["source"], where + "/origin/source")

        observations = sequence(card.get("observations", []), where + "/observations")
        for value in observations:
            w = where + "/observations"
            obj(value, w, ["id", "by", "date", "revision", "criteria", "fact", "source"], ["attempt"])
            for key in ("by", "date", "fact"):
                string(value[key], w + "/" + key)
            revision(value["revision"], w + "/revision")
            if "attempt" in value:
                self._attempt_revision(attempts, value, w)
            strings(value["criteria"], w + "/criteria", nonempty=True)
            if assignment is not None and value["revision"] == assignment["revision"]:
                for key in value["criteria"]:
                    if key not in criteria:
                        bad(w + "/criteria",
                            f"в редакции поручения {value['revision']} отсутствует критерий {key!r}")
            self._check_source(value["source"], w + "/source")
        evidence = named(observations, where + "/observations")

        decisions = sequence(card.get("decisions", []), where + "/decisions")
        for decision in decisions:
            self._check_decision(decision, evidence, attempts, where + "/decisions")
        named(decisions, where + "/decisions", optional_id=True)
        self._check_history(card, decisions, attempts, where)
        if not decisions:
            return "open"
        last = decisions[-1]
        if last["outcome"] == "accepted":
            if assignment is None:
                bad(where + "/decisions", "для приёмки нужно поручение")
            equal(last["revision"], assignment["revision"], where + "/decisions/revision")
            equal(last["role"], assignment["acceptor"], where + "/decisions/role")
            covered = {cid for key in last["observations"] for cid in evidence[key]["criteria"]}
            if covered != criteria.keys():
                bad(where + "/decisions", "приёмка должна ссылаться на все критерии своей редакции")
            if "waiting" in card:
                bad(where + "/waiting", "у принятого результата осталось обязательство")
            if unfinished := unfinished_attempts(card):
                bad(where + "/decisions", f"исход попытки {unfinished[0]} ещё не установлен")
            if "attempt" in last:
                review_requirements(card, attempts[last["attempt"]], where)
            elif assignment.get("research_review"):
                bad(where + "/decisions", "для приёмки исследования требуется представленная попытка")
            return "done"
        if last["outcome"] in CLOSED_OUTCOMES and "waiting" not in card:
            if unfinished_attempts(card):
                self.infos.append(f"{where}: завершите разбор исхода попытки перед закрытием")
                return "open"
            return "closed"
        return "open"

    def _check_assignment(self, value, where):
        w = where + "/assignment"
        obj(value, w, ["revision", "description", "boundaries", "write_scope", "authority",
                       "acceptor", "criteria"],
            ["plan", "stage", "inputs", "resources", "research_review", "mandate"])
        revision(value["revision"], w + "/revision")
        for key in ("boundaries", "acceptor"):
            string(value[key], w + "/" + key)
        if isinstance(value["description"], dict):
            obj(value["description"], w + "/description", ["path"])
            equal(value["description"]["path"], "description.md", w + "/description/path")
            self.file(where.removesuffix("task.json") + "description.md", w + "/description")
        else:
            string(value["description"], w + "/description")
        authority = obj(value["authority"], w + "/authority", ["by", "source"])
        string(authority["by"], w + "/authority/by")
        self._check_source(authority["source"], w + "/authority/source")
        for path in strings(value["write_scope"], w + "/write_scope"):
            if relative_path(path, w + "/write_scope").as_posix() == ".":
                bad(w + "/write_scope", "нужен ограниченный отрезок записи внутри корня")
            self.local(path, w + "/write_scope", must_exist=False)
        if "plan" in value:
            string(value["plan"], w + "/plan")
        if value.get("stage", "implement") not in ("prepare", "implement"):
            bad(w + "/stage", "ожидается prepare или implement")
        if "mandate" in value:
            mandate_terms(value, w + "/mandate")
        if value.get("stage") == "prepare" and (value["write_scope"] or value.get("resources")):
            bad(w + "/stage", "подготовка не даёт права записи, Git или внешнего действия")
        for path in strings(value.get("inputs", []), w + "/inputs"):
            if relative_path(path, w + "/inputs").as_posix() == ".":
                bad(w + "/inputs", "нужен путь существенного файла")
        resources(value.get("resources", []), w + "/resources", implicit_write=True)
        if "research_review" in value and type(value["research_review"]) is not bool:
            bad(w + "/research_review", "ожидается логическое значение")
        criteria = sequence(value["criteria"], w + "/criteria", nonempty=True)
        for criterion in criteria:
            obj(criterion, w + "/criteria", ["id", "expectation"])
            string(criterion["expectation"], w + "/criteria/expectation")
        return named(criteria, w + "/criteria")

    def _check_source(self, value, where):
        if isinstance(value, str):
            string(value, where)
        else:
            material_address(value, where)

    def _check_decision(self, value, evidence, attempts, where):
        obj(value, where, ["outcome", "by", "role", "date", "reason"],
            ["id", "revision", "observations", "source", "replacement", "attempt", "revokes", "restored_from",
             "basis", "mandate"])
        for key in ("by", "role", "date", "reason"):
            string(value[key], where + "/" + key)
        if "source" in value:
            self._check_source(value["source"], where + "/source")
        outcome = value["outcome"]
        if outcome not in (*CLOSED_OUTCOMES, "accepted", "reopened", "revoked"):
            bad(where + "/outcome", "неизвестное решение")
        if outcome == "revoked":
            string(value.get("revokes"), where + "/revokes")
        elif "revokes" in value:
            bad(where + "/revokes", "ссылка на отзыв допустима только при revoked")
        if "restored_from" in value:
            git_address(value["restored_from"], where + "/restored_from")
        if "basis" in value:
            if outcome != "accepted" or "attempt" in value:
                bad(where + "/basis", "снимок указывают для приёмки по наблюдениям без отдельной сдачи")
            git_address(value["basis"], where + "/basis")
        if "mandate" in value:
            if outcome != "accepted":
                bad(where + "/mandate", "мандат здесь указывают для приёмки")
            mandate_address(value["mandate"], where + "/mandate")
        if outcome == "replaced":
            full_id(value.get("replacement"), where + "/replacement")
        elif "replacement" in value:
            bad(where + "/replacement", "номер замены допустим только при replaced")
        if "revision" in value or outcome == "accepted":
            revision(value.get("revision"), where + "/revision")
        if "attempt" in value:
            self._attempt_revision(attempts, value, where)
        if "observations" in value or outcome == "accepted":
            for key in strings(value.get("observations"), where + "/observations", outcome == "accepted"):
                if key not in evidence:
                    bad(where + "/observations", "нет наблюдения " + key)
                equal(evidence[key]["revision"], value.get("revision"), where + "/observations/revision")
                if "attempt" in evidence[key]:
                    equal(evidence[key]["attempt"], value.get("attempt"), where + "/observations/attempt")

    def _attempt_revision(self, attempts, record, where):
        key = string(record["attempt"], where + "/attempt")
        if key not in attempts:
            bad(where + "/attempt", "неизвестная попытка " + key)
        equal(record.get("revision"), attempts[key]["revision"], where + "/attempt/revision")

    def _check_attempts(self, card, where):
        w = where + "/attempts"
        attempts = sequence(card.get("attempts", []), w)
        for attempt in attempts:
            obj(attempt, w, ["id", "executor", "revision", "basis", "phase", "resources"],
                ["launch", "termination", "result", "external_outcome"])
            string(attempt["executor"], w + "/executor")
            revision(attempt["revision"], w + "/revision")
            git_address(attempt["basis"], w + "/basis")
            if "launch" in attempt:
                git_address(attempt["launch"], w + "/launch")
            phase = attempt["phase"]
            if phase not in ("proposed", *LIVE_PHASES, "finished", "stopped"):
                bad(w + "/phase", "неизвестная фаза попытки")
            used = resources(attempt["resources"], w + "/resources")
            if phase in ("finished", "stopped"):
                proof = obj(attempt.get("termination"), w + "/termination", ["by", "source"])
                string(proof["by"], w + "/termination/by")
                self._check_source(proof["source"], w + "/termination/source")
            elif "termination" in attempt:
                bad(w + "/termination", "исход указан у незавершённой попытки")
            external = [r for r in used if r.startswith("external:")]
            if len(external) > 1:
                bad(w + "/resources", "одна попытка описывает не более одной внешней операции")
            if external:
                if attempt.get("external_outcome") not in ("pending", "unknown", "done", "not-done"):
                    bad(w + "/external_outcome", "нужно записать исход внешнего действия")
                if phase in ("finished", "stopped") and attempt["external_outcome"] == "pending":
                    bad(w + "/external_outcome", "для прекратившейся попытки установите исход или запишите unknown")
            elif "external_outcome" in attempt:
                bad(w + "/external_outcome", "нет внешнего ресурса")
            if "result" in attempt:
                if phase not in ("finished", "stopped"):
                    bad(w + "/result", "сначала установите прекращение попытки")
                result = obj(attempt["result"], w + "/result", ["outcome", "source"], ["artifacts"])
                if result["outcome"] not in ("submitted", "failed"):
                    bad(w + "/result/outcome", "ожидается submitted или failed")
                git_address(result["source"], w + "/result/source")
                for name in strings(result.get("artifacts", []), w + "/result/artifacts"):
                    if relative_path(name, w + "/result/artifacts").as_posix() == ".":
                        bad(w + "/result/artifacts", "нужен путь проверенного файла")
        return named(attempts, w)

    def _check_history(self, card, decisions, attempts, where):
        accepted = {}
        revoked = set()
        for decision in decisions:
            if decision["outcome"] == "revoked":
                key = decision["revokes"]
                if key not in accepted or key in revoked:
                    bad(where + "/decisions/revokes", "нужна ранее записанная неотозванная приёмка")
                equal(decision["role"], accepted[key]["role"], where + "/decisions/role")
                revoked.add(key)
            if decision["outcome"] == "accepted" and "id" in decision:
                accepted[decision["id"]] = decision
        w = where + "/revisions"
        old_revisions = set()
        for old in sequence(card.get("revisions", []), w):
            obj(old, w, ["revision", "basis", "authorized_by", "source", "reason"])
            revision(old["revision"], w + "/revision")
            if old["revision"] in old_revisions or old["revision"] >= card.get("assignment", {}).get("revision", 0):
                bad(w, "нужна уникальная прежняя редакция поручения")
            old_revisions.add(old["revision"])
            git_address(old["basis"], w + "/basis")
            for key in ("authorized_by", "reason"):
                string(old.get(key), w + "/" + key)
            self._check_source(old.get("source"), w + "/source")
        w = where + "/research_reviews"
        for review in sequence(card.get("research_reviews", []), w):
            obj(review, w, ["attempt", "revision", "by", "result", "source", "decisive_evidence", "open_required"])
            self._attempt_revision(attempts, review, w)
            string(review["by"], w + "/by")
            git_address(review["result"], w + "/result")
            git_address(review["source"], w + "/source")
            for evidence in sequence(review["decisive_evidence"], w + "/decisive_evidence"):
                git_address(evidence, w + "/decisive_evidence")
            strings(review["open_required"], w + "/open_required")

    def _check_links(self):
        parents, dependencies = {}, {}
        for number, entry in self._tasks.items():
            card = entry["card"]
            parent = card.get("parent")
            parents[number] = [parent] if parent else []
            dependencies[number] = []
            if parent and parent not in self._tasks:
                bad(number + "/parent", "отсутствует родитель " + parent)
            seen = set()
            for link in card.get("dependencies", []):
                target = link["task"]
                key = (target, link["criterion"])
                if key in seen:
                    bad(number + "/dependencies", "повтор обязательства " + "/".join(key))
                seen.add(key)
                dependencies[number].append(target)
                self._check_dependency(number, link)
            for decision in card.get("decisions", []):
                if "replacement" in decision and decision["replacement"] not in self._tasks:
                    bad(number + "/replacement", "отсутствует задача-замена " + decision["replacement"])
        for graph, label in ((parents, "вложенности"), (dependencies, "зависимостей")):
            try:
                tuple(TopologicalSorter(graph).static_order())
            except CycleError as error:
                bad("tasks", f"цикл {label}: " + " -> ".join(error.args[1]))

    def _check_dependency(self, number, link):
        w = number + "/dependencies"
        target = self._tasks.get(link["task"])
        if target is None:
            bad(w, "отсутствует цель " + link["task"])
        if "confirmation" in link and link["confirmation"]["task"] not in self._tasks:
            bad(w, "отсутствует работа, давшая выбранное подтверждение: " + link["confirmation"]["task"])
        card = target["card"]
        decisions = card.get("decisions", [])
        acceptance = link["acceptance"]
        if acceptance is None:
            criteria = [criterion["id"] for criterion in card.get("assignment", {}).get("criteria", [])]
        else:
            selected = next((d for d in decisions if d.get("id") == acceptance), None)
            if selected is None or selected["outcome"] != "accepted":
                bad(w, f"нет выбранной приёмки {acceptance} у {link['task']}")
            criteria = {cid for o in card.get("observations", []) if o["id"] in selected["observations"]
                        for cid in o["criteria"]}
            if selected is not decisions[-1] or target["location"] != "done":
                self.infos.append(f"{number}: приёмка {acceptance} у {link['task']} доступна как "
                                  "история; проверьте её применимость")
        if link["criterion"] not in criteria:
            bad(w, f"нет обязательства {link['criterion']} у {link['task']} в выбранной редакции")

    def list_tasks(self):
        return [self._tasks[number] for number in sorted(self._tasks)]

    def check(self):
        self.repository()
        return f"{TASK_FORMAT}; {len(self._tasks)} карточек; структура и связи"

    def find(self, number):
        full_id(number, "task")
        if number not in self._tasks:
            bad(number, "карточка отсутствует в выбранном источнике")
        return self._tasks[number]

    def read_material(self, address):
        material_address(address, "address")
        if "task" in address:
            entry = self.find(address["task"])
            folder = entry["path"].removesuffix("/task.json")
            return self.file(folder + "/" + address["path"], "material").read_bytes()
        return read_history(self, address)

    def show(self, number):
        entry = self.find(number)
        description = entry["card"].get("assignment", {}).get("description")
        if isinstance(description, dict):
            path = description["path"]
            where = f"{number}/{path}"
            content = self._read_description(entry, path)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                bad(where, "описание должно быть в кодировке UTF-8")
            return {**entry, "description_text": string(text, where)}
        return entry

    def _read_description(self, entry, path):
        return self.read_material({"task": entry["id"], "path": path})


def read_history(reader, address):
    material_address(address, "address")
    reader.repository()
    ref, name = address["commit"], address["path"]
    reader.commit(ref, "address/commit")
    if "select" in address:
        from records import saved_record
        return saved_record(reader, ref, name, address["select"])
    return reader.saved_file(ref, name)
