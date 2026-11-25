"""Check one proposed or current action against card and resource records."""

import os
from functools import cached_property
from pathlib import PurePosixPath
import re

from read_support import Problem, bad, canonical, equal, obj, parse, revision, sequence, string, strings
from records import RecordsChecker, by_id, contains
from task_reader import (
    LIVE_PHASES, LOCATIONS, SCOPE_ID, TASK_FOLDER, TaskReader, full_id, git_address, keyed_history,
    mandate_address, mandate_terms,
    profile_selection, resources, retained_resources, review_requirements, switch_pause,
)
from versions import CURRENT_FORMAT, TASK_FORMAT, record_format


SCOPE_MARKER = "<!-- ai-kit:scope "
DECISION_ANNOTATIONS = {"id", "source", "basis", "mandate", "restored_from"}


def assignment_basis(card):
    return canonical((card.get("format_version"), card.get("id"), card.get("goal"),
                      card.get("assignment"), card.get("dependencies", [])))


def permitted(assignment):
    return set(assignment.get("resources", [])) | ({"write"} if assignment["write_scope"] else set())


def attempt_basis(attempt):
    return canonical(tuple(attempt.get(key) for key in ("id", "executor", "revision", "basis", "resources")))


def proposal(attempt):
    return attempt["phase"] == "proposed" and "launch" not in attempt


def entries(card, key, where):
    if not isinstance(card, dict):
        bad(where, "ожидается объект карточки")
    values = sequence(card.get(key, []), where + "/" + key)
    if not all(isinstance(value, dict) for value in values):
        bad(where + "/" + key, "нужны объекты записей карточки")
    return values


def decision_observations(card, decision, where):
    obs_where = where + "/observations"
    keys = strings(decision.get("observations", []), obs_where)
    selected = {}
    for observation in entries(card, "observations", where):
        key = observation.get("id")
        if key in keys:
            if key in selected:
                bad(obs_where, f"наблюдение {key} записано несколько раз")
            selected[key] = observation
    for key in keys:
        if key not in selected:
            bad(obs_where, f"не найдено наблюдение {key}")
    return [selected[key] for key in keys]


def decision_basis(card, acceptance, where):
    observations = decision_observations(card, acceptance, where)
    attempt = next((a for a in entries(card, "attempts", where)
                    if a.get("id") == acceptance.get("attempt")), None)
    review = next((r for r in reversed(entries(card, "research_reviews", where))
                   if r.get("attempt") == acceptance.get("attempt")), None)
    return canonical((assignment_basis(card), acceptance, observations, attempt, review))


class TaskActions(TaskReader):
    def check(self):
        status = super().check()
        for entry in self.list_tasks():
            if entry["card"].get("revisions") or any(
                    "restored_from" in decision for decision in entry["card"].get("decisions", [])):
                self.check_history(entry)
        return status

    def action(self, profile, number, action, actor, attempt_id):
        self.repository()
        self.profile = profile
        entry = self.find(number)
        card = entry["card"]
        scope_id = number.split("/")[0]
        scope = self.scope_record(scope_id)
        assignment = card.get("assignment")
        if assignment is None:
            bad(number, "сначала подготовьте поручение")
        last = (card.get("decisions") or [{}])[-1]
        outcome = last.get("outcome")
        if action == "handoff":
            equal(outcome, "accepted", number + "/decisions")
        elif outcome in ("accepted", "rejected", "cancelled", "replaced", "revoked"):
            bad(number, "после итогового решения нужно явное повторное открытие")
        if action in ("assign", "execute") and "waiting" in card:
            bad(number + "/waiting", card["waiting"]["next_step"])

        attempts = card.get("attempts", [])
        attempt = next((a for a in attempts if a["id"] == attempt_id), None)
        if attempt_id is not None and attempt is None:
            bad(number + "/attempts", "нет попытки " + attempt_id)
        if action == "handoff":
            equal(attempt_id, last.get("attempt"), number + "/decisions/attempt")
        if attempt is None and action in ("assign", "execute"):
            bad(number, "укажите --attempt с ключом действующей попытки")
        equal(actor, attempt["executor"] if action == "execute" else scope["editor"]["session"],
              number + "/actor")
        equal(scope["editor"]["role"], assignment["acceptor"], number + "/assignment/acceptor")
        self.check_decision_layers(entry)
        self.check_history(entry)
        mandate = self.check_mandate(scope_id, scope, assignment) if scope_id != "P" else None
        if action == "handoff" and mandate and last.get("mandate") != scope["mandate"]:
            bad(mandate[0] + "/mandate", "в местной приёмке нужен адрес мандата, по которому принято решение")
        self.check_dependencies(entry, set())

        if attempt is None:
            if assignment.get("research_review"):
                bad(number, "для приёмки исследования укажите --attempt с проверенной сдачей")
            self.check_conflicts(entry, None, set())
            if action == "handoff":
                basis = self.acceptance_basis(entry, last, number)
                ref, path = basis["commit"], basis["path"]
            else:
                ref, path = "HEAD", entry["path"]
            self.check_assignment_layers(entry, ref, path)
            self.check_files(assignment.get("inputs", []), ref, number)
            self.read_source(assignment["authority"]["source"])
        else:
            self.check_attempt(entry, attempt, action, scope_id, mandate)
        if action == "handoff":
            self.check_acceptance_sources(card, last, number)
        return f"{action}; {number}; редакция {assignment['revision']}"

    def scope_record(self, scope, ref=None, *, optional=False):
        name = self.scopes[scope]
        data = self.file(name, scope).read_bytes() if ref is None else self.saved_file(ref, name)
        lines = data.decode("utf-8").splitlines()
        markers = [line.strip() for line in lines if line.strip().startswith("<!-- ai-kit:scope")]
        if optional and not markers:
            return None
        if len(markers) != 1 or not markers[0].startswith(SCOPE_MARKER) or not markers[0].endswith(" -->"):
            bad(name, "для проверки действия нужна одна запись ai-kit:scope")
        raw = parse(markers[0][len(SCOPE_MARKER):-4], name)
        required = ["editor", "continuation", "resources" if scope == "P" else "mandate"]
        value = obj(raw, name, required)
        editor = obj(value["editor"], name + "/editor", ["role", "session"])
        for key in ("role", "session"):
            string(editor[key], name + "/editor/" + key)
        number = full_id(value["continuation"], name + "/continuation")
        if not number.startswith(scope + "/"):
            bad(name + "/continuation", "точка продолжения должна принадлежать этой области")
        if ref is None:
            self.find(number)
        return value

    def continuations(self):
        for scope in self.scopes:
            record = self.scope_record(scope, optional=True)
            if record is not None:
                entry = self.find(record["continuation"])
                editor = record["editor"]
                text = f"{entry['id']} {entry['card']['title']}; {editor['role']} ({editor['session']})"
                if waiting := entry["card"].get("waiting"):
                    text += "; следующий шаг — " + waiting["next_step"]
                yield text

    def check_mandate(self, scope_id, scope, local):
        mandate = scope["mandate"]
        mandate_address(mandate, "scope/mandate")
        number = mandate["task"]
        entry = self.find(number)
        card = entry["card"]
        if not card.get("assignment"):
            bad(number, "нет действующего этапа мандата")
        saved = self.snapshot(entry, mandate["basis"])
        if assignment_basis(card) != assignment_basis(saved):
            bad(number + "/mandate", "действующий мандат отличается от принятого областью снимка")
        assignment = self.mandate_limits(card, scope_id, scope["editor"]["role"], local, number + "/mandate")
        self.check_assignment_layers(entry, mandate["basis"]["commit"], mandate["basis"]["path"])
        self.check_files(assignment.get("inputs", []), mandate["basis"]["commit"], number)
        self.read_source(assignment["authority"]["source"])
        self.check_dependencies(entry, set())
        return number, assignment

    def mandate_limits(self, card, scope, role, local, where):
        assignment = card["assignment"]
        if "mandate" not in assignment:
            bad(where, "нет мандата на местную область")
        owner, stages = mandate_terms(assignment, where)
        equal(owner["scope"], scope, where + "/scope")
        equal(owner["role"], role, where + "/role")
        last = (entries(card, "decisions", where) or [{}])[-1]
        if last.get("outcome") not in (None, "reopened"):
            bad(where, "общее поручение функции закрыто")
        if "waiting" in card:
            waiting = obj(card["waiting"], where + "/waiting", ["remaining", "reason", "next_step"])
            bad(where + "/waiting", string(waiting["next_step"], where + "/waiting/next_step"))
        stage = local.get("stage", "implement")
        if stage not in stages:
            reason = ("мандат разрешает только подготовку" if stages == ["prepare"]
                      else f"местный этап {stage} не входит в мандат")
            bad(where, f"{reason}. Новое решение выдаёт общая роль «{assignment['acceptor']}».")
        paths = strings(assignment.get("write_scope"), where + "/write_scope")
        for path in local["write_scope"]:
            if not any(contains(parent, path) for parent in paths):
                bad(where, f"отрезок записи вне мандата местной роли: {path}")
        return assignment

    def check_acceptance_mandate(self, entry, decision, attempt, ref, where):
        scope = entry["id"].split("/")[0]
        if scope == "P":
            return
        where += "/mandate"
        mandate = decision.get("mandate")
        if mandate is None:
            bad(where, "в местной приёмке нужен сохранённый мандат принимающей роли")
        mandate_address(mandate, where)
        parent = self.find(mandate["task"])
        where += "/" + parent["id"]
        try:
            issued = self.scope_record(scope, ref)
        except Problem as error:
            bad(where, f"для приёмки нужен сохранённый scope.md местной области: {error.message}")
        if issued["mandate"] != mandate:
            bad(where, "мандат решения отличается от записанного в scope.md на снимке приёмки")
        equal(issued["editor"]["role"], decision["role"], where + "/role")
        names = [path for number, path in self.saved_task_paths(ref) if number == parent["id"]]
        if len(names) != 1:
            bad(where, "в снимке приёмки нужна одна общая карточка мандата")
        effective = self.snapshot(parent, {"commit": ref, "path": names[0]}, where)
        original = self.snapshot(parent, mandate["basis"], where)
        if assignment_basis(effective) != assignment_basis(original):
            bad(where, "общее поручение в снимке приёмки отличается от мандата решения")
        parent_terms = self.mandate_limits(effective, scope, decision["role"], entry["card"]["assignment"], where)
        inputs = [(name, name) for name in strings(parent_terms.get("inputs", []), where + "/inputs")]
        if isinstance(parent_terms.get("description"), dict):
            inputs.append((str(PurePosixPath(mandate["basis"]["path"]).with_name("description.md")),
                           str(PurePosixPath(names[0]).with_name("description.md"))))
        for old, current in inputs:
            if self.saved_file(mandate["basis"]["commit"], old) != self.saved_file(ref, current):
                bad(where, f"существенный вход мандата {current} изменился к снимку приёмки")
        if attempt:
            resources(parent_terms.get("resources", []), where + "/resources", implicit_write=True)
            if not set(attempt["resources"]) <= permitted(parent_terms):
                bad(where, "ресурс попытки вне мандата")

    def task_file_at(self, name):
        """Identify a file in a numbered bundle from its project-relative path."""
        prefix = self.state_name + "/"
        if not name.startswith(prefix):
            return None
        parts = name[len(prefix):].split("/")
        scope = "P"
        if len(parts) >= 5 and parts[0] == "features" and parts[1] != "P" and re.fullmatch(SCOPE_ID, parts[1]):
            scope, parts = parts[1], parts[2:]
        if len(parts) >= 3 and parts[0] in LOCATIONS:
            if match := re.fullmatch(TASK_FOLDER, parts[1]):
                return f"{scope}/{match[1]}", "/".join(parts[2:])

    def task_number_at(self, name):
        address = self.task_file_at(name)
        if address and address[1] == "task.json":
            return address[0]

    @cached_property
    def current_layer_files(self):
        layers = {}
        for ref in ("HEAD", ""):
            files = layers[ref] = {}
            for name in self.saved_paths(ref):
                if address := self.task_file_at(name):
                    files.setdefault(address, []).append(name)
        return layers

    def layer_card(self, ref, number, *, optional=False):
        paths = self.current_layer_files[ref].get((number, "task.json"), [])
        if optional and not paths:
            return None
        label = ref or "индекс"
        if len(paths) != 1:
            bad(number, f"{label}: нужен один путь карточки; найдено " +
                (" и ".join(paths) if paths else "0"))
        return paths[0]

    def check_decision_layers(self, entry):
        number, card = entry["id"], entry["card"]
        working = entries(card, "decisions", number)
        for ref, label in (("HEAD", "HEAD"), ("", "индексе")):
            path = self.layer_card(ref, number, optional=True)
            if path is None:
                continue
            saved = self.saved_json(ref, path)
            decisions = entries(saved, "decisions", path)
            equal(saved.get("id"), card["id"], path + "/id")
            equal(saved.get("format_version"), TASK_FORMAT, path + "/format_version")
            for position, recorded in enumerate(decisions):
                if position < len(working):
                    current = working[position]
                    added = current.keys() - recorded.keys()
                    if added <= DECISION_ANNOTATIONS and canonical(recorded) == canonical(
                            {key: value for key, value in current.items() if key in recorded}):
                        continue
                outcome = recorded.get("outcome", "?")
                bad(number, f"история решений расходится с записью в {label}: {path}, "
                    f"решение {position + 1} ({outcome}); восстановите сохранённые решения "
                    "и сверьте действующее решение с принимающей ролью")

    def layer_file(self, ref, name):
        address = self.task_file_at(name)
        if address is None:
            return name
        number, path = address
        card = self.find(number)["path"] if ref is None else self.layer_card(ref, number)
        if ref is not None:
            # A partial commit can put the card and the material in separate buckets.
            paths = self.current_layer_files[ref].get(address, [])
            if len(paths) > 1:
                label = ref or "индексе"
                bad(f"{number}/{path}", f"несколько адресов материала в {label}: " + " и ".join(paths))
            if paths:
                return paths[0]
        return str(PurePosixPath(card).parent / path)

    def snapshot(self, entry, address, where=None):
        where = where or entry["id"] + "/basis"
        git_address(address, where)
        ref, name = address["commit"], address["path"]
        if self.task_number_at(name) != entry["id"]:
            bad(where, "снимок должен указывать на эту карточку в её области")
        self.commit(ref, where + "/commit")
        card = self.saved_json(ref, name)
        if not isinstance(card, dict) or not isinstance(card.get("assignment"), dict):
            bad(name, "в снимке нет поручения карточки")
        equal(card.get("format_version"), TASK_FORMAT, name + "/format_version")
        equal(card.get("id"), entry["card"]["id"], name + "/id")
        return card

    def acceptance_basis(self, entry, decision, where, attempt=None):
        address = attempt["basis"] if attempt else decision.get("basis")
        if address is None:
            bad(where + "/basis", "укажите сохранённый снимок поручения и входов, по которым проверен результат")
        saved = self.snapshot(entry, address, where + "/basis")
        if assignment_basis(saved) != assignment_basis(entry["card"]):
            bad(where + "/basis", "текущее поручение отличается от проверенного снимка")
        return address

    def check_assignment_layers(self, entry, ref, old_path=None):
        current = entry["card"]
        name = entry["path"]
        where = entry["id"]
        expected = (TASK_FORMAT, self.state_name)
        active = profile_selection(self.file(self.profile, where).read_text(encoding="utf-8"), self.profile)
        equal((active["format_version"], active["path"]), expected, where + "/profile")
        prepared = active.get("prepared")
        for layer in ("HEAD", ""):
            selected = profile_selection(self.saved_file(layer, self.profile).decode("utf-8"), self.profile)
            equal((selected["format_version"], selected["path"]), expected, where + "/profile")
            equal(selected.get("prepared"), prepared, where + "/profile/prepared")
        if ref not in ("HEAD", ""):
            profile = self.profile
            if prepared and ref in prepared["commits"]:
                # Only the exact bases checked during activation can use
                # the preparation profile. Both current Git layers use the real profile.
                profile = prepared["profile"]
            selected = profile_selection(self.saved_file(ref, profile).decode("utf-8"), profile)
            equal((selected["format_version"], selected["path"]), expected, where + "/profile")
        for layer, label in (("HEAD", "HEAD"), ("", "индексе")):
            saved = self.saved_json(layer, self.layer_card(layer, where))
            if not isinstance(saved, dict) or assignment_basis(saved) != assignment_basis(current):
                bad(name, f"поручение отличается в {label}")
        self.check_description(entry, ref, old_path or name, where)

    def check_description(self, entry, ref, old_path, where):
        if isinstance(entry["card"]["assignment"]["description"], dict):
            previous = str(PurePosixPath(old_path).with_name("description.md"))
            live = str(PurePosixPath(entry["path"]).with_name("description.md"))
            if ref in ("HEAD", ""):
                previous = self.layer_file(ref, previous)
            self.file_layers(live, self.saved_file(ref, previous), where)

    def file_layers(self, name, expected, where):
        for layer, label in (("HEAD", "HEAD"), ("", "индексе")):
            path = self.layer_file(layer, name)
            if self.saved_file(layer, path) != expected:
                bad(where, f"существенный файл {path} отличается в {label}")
        worktree = self.layer_file(None, name)
        if self.file(worktree, worktree).read_bytes() != expected:
            bad(where, f"рабочие байты существенного файла {worktree} отличаются")

    def check_files(self, paths, ref, where):
        for name in paths:
            saved = self.layer_file(ref, name) if ref in ("HEAD", "") else name
            self.file_layers(name, self.saved_file(ref, saved), where)

    def read_source(self, source, where=None):
        if isinstance(source, dict):
            try:
                return self.read_material(source)
            except (Problem, OSError) as error:
                if where is None:
                    raise
                address = f"{source.get('commit', source.get('task'))}:{source['path']}"
                detail = error.message if isinstance(error, Problem) else type(error).__name__
                bad(where, f"свидетельство {address} недоступно: {detail}")

    def check_acceptance_sources(self, card, decision, where):
        for observation in decision_observations(card, decision, where):
            self.read_source(observation["source"], f"{where}/observations/{observation['id']}")
        if "source" in decision:
            self.read_source(decision["source"], where + "/decision/source")

    def check_history(self, entry):
        card = entry["card"]
        for item in card.get("revisions", []):
            old = self.snapshot(entry, item["basis"])
            equal(old["assignment"].get("revision"), item["revision"], entry["id"] + "/revisions")
            if isinstance(old["assignment"].get("description"), dict):
                desc = str(PurePosixPath(item["basis"]["path"]).with_name("description.md"))
                self.saved_file(item["basis"]["commit"], desc)
            self.read_source(item["source"])
        for decision in card.get("decisions", []):
            if "restored_from" not in decision:
                continue
            where = entry["id"] + "/restored_from"
            saved = self.snapshot(entry, decision["restored_from"], where)
            expected = {key: value for key, value in decision.items() if key != "restored_from"}
            matches = [item for item in entries(saved, "decisions", where)
                       if canonical(item) == canonical(expected)]
            if len(matches) != 1:
                bad(where, "прошлая дата требует однозначной записи решения в исторической карточке этой задачи")
            if "revision" in expected:
                equal(saved["assignment"].get("revision"), expected["revision"], where + "/revision")
            if canonical(decision_observations(saved, expected, where)) != canonical(
                    decision_observations(card, expected, where)):
                bad(where, "наблюдения отличаются от оснований прежнего решения этой задачи")

    def check_attempt(self, entry, attempt, action, scope, mandate):
        number, card = entry["id"], entry["card"]
        assignment = card["assignment"]
        equal(attempt["revision"], assignment["revision"], number + "/attempt/revision")
        eligible = [a for a in card["attempts"] if a["phase"] != "proposed" or action == "assign"]
        if not eligible or attempt is not eligible[-1]:
            bad(number, "поздний результат относится к прежней попытке")
        phases = {"assign": ("proposed", "assigned"), "execute": ("assigned", "running"),
                  "review": ("finished",), "handoff": ("finished",)}
        if attempt["phase"] not in phases[action]:
            bad(number, f"для {action} не подходит фаза {attempt['phase']} попытки {attempt['id']}")
        used = set(attempt["resources"])
        if not used <= permitted(assignment) or bool(assignment["write_scope"]) != ("write" in used):
            bad(number, "ресурсы попытки должны соответствовать поручению и отрезку записи")
        if mandate and not used <= permitted(mandate[1]):
            bad(mandate[0] + "/mandate", "ресурс попытки вне мандата")
        self.check_conflicts(entry, attempt, used if action in ("assign", "execute") else set())
        if used and (action in ("assign", "execute") or (mandate and action == "review")):
            self.check_grants(entry, attempt, scope, mandate)

        saved = self.snapshot(entry, attempt["basis"])
        if assignment_basis(card) != assignment_basis(saved):
            bad(number, "содержание поручения отличается от выданного снимка")
        ref = attempt["basis"]["commit"]
        self.check_assignment_layers(entry, ref, attempt["basis"]["path"])
        self.check_files(assignment.get("inputs", []), ref, number)
        self.read_source(assignment["authority"]["source"])
        if action != "assign" or attempt["phase"] == "assigned":
            self.check_launch(entry, attempt)
        if action == "execute" and attempt.get("external_outcome") not in (None, "pending"):
            bad(number, "для внешнего действия сначала выясните исход или оформите новый запуск")
        if action in ("review", "handoff"):
            self.read_source(attempt["termination"]["source"])
            review = review_requirements(card, attempt, number)
            result = attempt["result"]
            self.read_source(result["source"])
            self.check_files(result.get("artifacts", []), result["source"]["commit"], number)
            if review:
                self.read_source(review["source"])
                for address in review["decisive_evidence"]:
                    self.read_source(address)

    def check_launch(self, entry, attempt):
        where = entry["id"] + "/attempt/launch"
        launched, prior = self.launch_record(entry, attempt, where)
        if assignment_basis(launched) != assignment_basis(entry["card"]):
            bad(where, "поручение отличается от снимка назначения исполнителя")
        self.check_launch_identity(entry, attempt, prior, where)

    def launch_record(self, entry, attempt, where):
        if "launch" not in attempt:
            bad(where, "нужен сохранённый снимок назначения исполнителя и ресурсов")
        launched = self.snapshot(entry, attempt["launch"], where)
        matches = [a for a in entries(launched, "attempts", where) if a.get("id") == attempt["id"]]
        if len(matches) != 1 or matches[0].get("phase") not in ("proposed", "assigned"):
            bad(where, "в сохранённом снимке нет однозначного назначения этой попытки")
        resources(matches[0].get("resources"), where + "/resources")
        return launched, matches[0]

    def check_launch_identity(self, entry, attempt, prior, where):
        if attempt_basis(prior) != attempt_basis(attempt):
            bad(where, "назначение исполнителя или ресурсов отличается от сохранённого")
        for layer, label in (("HEAD", "HEAD"), ("", "индексе")):
            try:
                current = self.saved_json(layer, self.layer_card(layer, entry["id"]))
                matches = [a for a in entries(current, "attempts", where) if a.get("id") == attempt["id"]]
            except Problem as error:
                bad(where, f"не удалось сверить назначение в {label}: {error.message}")
            if len(matches) != 1 or attempt_basis(matches[0]) != attempt_basis(attempt):
                bad(where, f"назначение исполнителя или ресурсов отличается в {label}")

    def check_conflicts(self, entry, selected, used):
        self.check_saved_conflicts(entry, used)
        for other in self.list_tasks():
            same = other is entry
            if not used and not same:
                continue
            for attempt in other["card"].get("attempts", []):
                if attempt is selected:
                    if attempt.get("external_outcome") == "unknown":
                        bad(entry["id"], "неизвестен исход внешнего действия попытки " + attempt["id"])
                    continue
                if proposal(attempt):
                    continue
                self.check_previous_attempt(entry, other, attempt, used, same)

    def check_saved_conflicts(self, entry, used):
        # A reverted or missing working card can conceal a previously issued attempt.
        working = {row["id"]: {a["id"]: a for a in row["card"].get("attempts", [])}
                   for row in self.list_tasks() if used or row is entry}
        for ref, label in (("HEAD", "HEAD"), ("", "индексе")):
            for number, name in self.saved_task_paths(ref):
                if not used and number != entry["id"]:
                    continue
                saved = self.saved_json(ref, name)
                for attempt in entries(saved, "attempts", name):
                    key = string(attempt.get("id"), name + "/attempt/id")
                    where = f"{entry['id']}/прежняя попытка {number}/{key}"
                    string(attempt.get("phase"), where + "/phase")
                    recorded = set(resources(attempt.get("resources"), where + "/resources"))
                    if proposal(attempt) or (number != entry["id"] and not used & recorded):
                        continue
                    current = working.get(number, {}).get(key)
                    if current is None or proposal(current):
                        bad(where, f"назначение сохранено в {label}, а рабочая карточка его не подтверждает; "
                            "восстановите запись и установите исход прежней попытки")

    def saved_paths(self, ref):
        args = ("ls-tree", "-rz", "--name-only", ref) if ref else ("ls-files", "-z")
        names = self.git(*args, "--", ":(literal)" + self.state_name).split(b"\0")[:-1]
        for raw in dict.fromkeys(names):
            yield os.fsdecode(raw)

    def saved_task_paths(self, ref):
        for path in self.saved_paths(ref):
            if number := self.task_number_at(path):
                yield number, path

    def check_previous_attempt(self, entry, other, attempt, used, same):
        previous = f"{other['id']}/{attempt['id']}"
        where = f"{entry['id']}/прежняя попытка {previous}"
        claimed = set(attempt["resources"])
        try:
            _, saved = self.launch_record(other, attempt, where + "/launch")
        except Problem as error:
            if same or used & claimed or self.may_hold(other, attempt, used, where):
                bad(where, f"сверьте сохранённые права запуска: {error.message}")
            return
        if not same and not used & (claimed | set(saved["resources"])):
            return
        retained = set(retained_resources(attempt))
        if attempt["phase"] == "proposed":
            bad(where, "уточните фазу попытки с сохранённым назначением")
        self.check_launch_identity(other, attempt, saved, where)
        if retained & used or (same and (retained or attempt["phase"] in LIVE_PHASES)):
            bad(entry["id"], f"ресурс удерживает {previous}: {attempt['phase']}; "
                "установите исход прежней попытки")
        self.read_source(attempt["termination"]["source"], where + "/termination")

    def may_hold(self, entry, attempt, used, where):
        # If the launch is missing, the frozen order can still prove disjoint rights.
        try:
            assignment = self.snapshot(entry, attempt["basis"], where + "/basis")["assignment"]
            possible = set(resources(assignment.get("resources", []), where, implicit_write=True))
            if strings(assignment.get("write_scope"), where):
                possible.add("write")
        except Problem:
            return True
        return bool(used & possible)

    def resource_grants(self):
        name = self.scope_record("P")["resources"]
        book = obj(parse(self.file(name, "resources").read_bytes(), name), name, ["editor", "grants"])
        string(book["editor"], name + "/editor")
        grants = {}
        for grant in sequence(book["grants"], name + "/grants"):
            obj(grant, name, ["resource", "holder", "work", "revision"], ["attempt"])
            key = resources([grant["resource"]], name)[0]
            if key in grants:
                bad(name, "повторная выдача ресурса " + key)
            holder = string(grant["holder"], name + "/holder")
            if holder not in self.scopes:
                bad(name, "нет области держателя " + holder)
            full_id(grant["work"], name + "/work")
            revision(grant["revision"], name + "/revision")
            if holder == "P":
                string(grant.get("attempt"), name + "/attempt")
            elif "attempt" in grant or not grant["work"].startswith("P/"):
                bad(name, "ресурс функции закрепляется за общим мандатом")
            grants[key] = grant
        return name, grants

    def check_grants(self, entry, attempt, scope, mandate):
        name, grants = self.resource_grants()
        work, rev, launch = ((mandate[0], mandate[1]["revision"], None) if mandate
                             else (entry["id"], attempt["revision"], attempt["id"]))
        for key in attempt["resources"]:
            grant = grants.get(key)
            if grant is None:
                bad(name, "нет выдачи ресурса " + key)
            if (grant["holder"], grant["work"], grant["revision"], grant.get("attempt")) != (
                    scope, work, rev, launch):
                bad(name, f"ресурс {key} закреплён за {grant['holder']} ({grant['work']}); "
                    "проверьте выдачу и редакцию")

    def dependency_decision(self, target, selected, where, basis=None, *, require_basis=False):
        card = target["card"]
        last = (card.get("decisions") or [{}])[-1]
        if selected is None or selected != last.get("id") or last.get("outcome") != "accepted":
            bad(where, "нужна действующая выбранная приёмка обязательства")
        if require_basis and basis is None:
            bad(where, "нужен сохранённый снимок выбранной приёмки")
        accepted = decision_basis(card, last, where)
        if basis is not None:
            saved = self.snapshot(target, basis)
            prior = (entries(saved, "decisions", where) or [{}])[-1]
            if (prior.get("id") != selected or prior.get("outcome") != "accepted" or
                    decision_basis(saved, prior, where) != accepted):
                bad(where, "изменились обязательство, приёмка или её наблюдения")
        for layer, label in (("HEAD", "HEAD"), ("", "индексе")):
            snapshot = self.saved_json(layer, self.layer_card(layer, target["id"]))
            current = (entries(snapshot, "decisions", where) or [{}])[-1]
            if current.get("outcome") != "accepted" or decision_basis(snapshot, current, where) != accepted:
                bad(where, f"основание отличается в {label}")
        return last

    def check_confirmation(self, link, target, decision, where):
        chosen = link["confirmation"]
        where += f"/confirmation/{chosen['task']}/{chosen['check']}"
        target_key = keyed_history(decision.get("source"), "acceptance", where + "/target")
        criterion_key = chosen["criterion"]["select"]
        obligation = (target_key["work"], decision["revision"], link["criterion"])
        equal((criterion_key["work"], criterion_key["revision"], criterion_key["key"]),
              obligation, where + "/criterion")
        term = by_id(target["card"]["assignment"]["criteria"], link["criterion"], where + "/criterion")
        equal(canonical(self.read_source(chosen["criterion"], where)), canonical(term), where + "/criterion")

        proof_key = chosen["acceptance"]["select"]
        order_key = chosen["assignment"]["select"]
        confirmer = self.find(chosen["task"])
        accepted = (decision if confirmer["id"] == target["id"] else
                    self.dependency_decision(confirmer, proof_key["key"], where))
        equal(accepted.get("source"), chosen["acceptance"], where + "/acceptance")
        equal(accepted["revision"], order_key["revision"], where + "/revision")
        equal(proof_key["work"], order_key["work"], where + "/work")
        proof = self.read_source(chosen["acceptance"], where + "/acceptance")
        result = self.read_source(chosen["result"], where + "/result")
        order = self.read_source(chosen["assignment"], where + "/assignment")
        equal((proof.get("result"), proof.get("result_revision")), (result["id"], result["revision"]),
              where + "/result")
        equal((result["assignment_id"], result["assignment_revision"]), (order["id"], order["revision"]),
              where + "/assignment")
        for kind in ("assignment", "criterion"):
            equal(chosen[kind]["commit"], result["commit"], where + "/" + kind + "/commit")
        check = by_id(result["checks"], chosen["check"], where + "/check")
        accepted_checks = strings(proof.get("evidence"), where + "/acceptance/evidence")
        if chosen["check"] not in accepted_checks or check.get("outcome") != "passed":
            bad(where, "нужна успешная проверка из выбранной приёмки результата")
        expected = dict(zip(("work", "assignment_revision", "criterion"), obligation))
        if expected not in sequence(check.get("covers"), where + "/covers"):
            bad(where, "выбранная проверка не покрывает обязательство этой редакции")
        try:
            RecordsChecker(self.root, None).check_result_inputs(
                result["commit"], order["inputs"], where, obligation, chosen.get("compatibility"))
        except Problem as error:
            raise Problem(error.where, error.message + ". Повторите выбранную проверку или согласуйте новое основание.",
                          error.code) from error

    def check_dependencies(self, entry, checked):
        if entry["id"] in checked:
            return
        checked.add(entry["id"])
        for link in entry["card"].get("dependencies", []):
            target = self.find(link["task"])
            card = target["card"]
            where = f"{entry['id']}/dependencies/{link['task']}/{link['criterion']}"
            last = self.dependency_decision(target, link["acceptance"], where, link.get("basis"),
                                            require_basis=True)
            self.check_history(target)
            attempt = next((a for a in card.get("attempts", []) if a["id"] == last.get("attempt")), None)
            self.check_acceptance_mandate(target, last, attempt, link["basis"]["commit"], where)
            self.check_acceptance_sources(card, last, where)
            basis = self.acceptance_basis(target, last, where, attempt)
            input_ref = basis["commit"]
            self.check_description(target, input_ref, basis["path"], where)
            artifact_ref = attempt["result"]["source"]["commit"] if attempt else input_ref
            if claim := link.get("compatibility"):
                self.commit(claim["commit"], where + "/compatibility/commit")
                input_ref = artifact_ref = claim["commit"]
            self.check_files(card["assignment"].get("inputs", []), input_ref, where)
            if attempt:
                review = review_requirements(card, attempt, where)
                self.read_source(attempt["termination"]["source"], where + "/termination")
                self.read_source(attempt["result"]["source"], where + "/result/source")
                self.check_files(attempt["result"].get("artifacts", []), artifact_ref, where)
                if review:
                    self.read_source(review["source"], where + "/research_review")
                    for evidence in review["decisive_evidence"]:
                        self.read_source(evidence, where + "/decisive_evidence")
            if "confirmation" in link:
                self.check_confirmation(link, target, last, where)
            self.check_dependencies(target, checked)


def transition_status(reader, profile, selection, path, *, target=None):
    """Read the issuance decision separately from any action or command caller."""
    source, version = selection["path"], selection["format_version"]
    cards = version == TASK_FORMAT
    pause = switch_pause(reader, profile, source if cards else target, path)
    state = pause["state"]
    editor = None
    if cards:
        active = reader if isinstance(reader, TaskActions) else TaskActions(reader.root, source)
        equal(active.state_name, source, profile + "/path")
        scope = active.scope_record("P", optional=True)
        if scope is not None:
            editor = scope["editor"]
    elif version == CURRENT_FORMAT:
        data = parse(reader.file(source, "state").read_bytes(), source)
        if (not isinstance(data, dict) or record_format(data.get("format_version")) != CURRENT_FORMAT
                or data.get("scope") != "product"):
            bad(source, "для проверки общей выдачи нужен действующий общий файл состояния")
        editor = obj(data.get("editor"), source + "/editor", ["role", "session", "status"])
        for key in ("role", "session"):
            string(editor[key], source + "/editor/" + key)

    owner = f"«{editor['role']}» ({editor['session']})" if editor else "общий координатор"
    selected = f"Профиль {profile} выбирает {source}."
    if (cards and state == "stopped") or (not cards and state == "reviewed"):
        decision = ("остановку с продолжением по прежнему списку" if cards else
                    "возобновление выдачи по карточкам")
        bad(path, f"{selected} Запись {path} указывает {decision}. "
            f"Следующий шаг — {owner} сверяет запись перехода с выбранным источником.")
    if state == "reviewed" and editor is None:
        bad(path, f"{selected} Для решения в {path} не установлена действующая сессия общей области. "
            f"Следующий шаг — общему координатору указать её в {source}/scope.md.")
    if state != "paused" and editor and pause["actor"] != editor["session"]:
        decision = "о возобновлении выдачи" if state == "reviewed" else "об остановке перехода"
        bad(path, f"{selected} Решение {decision} в {path} записано от {pause['actor']}. "
            f"Следующий шаг — действующий общий редактор {owner} сверяет основания и оформляет решение.")

    step = pause["next_step"]
    if state == "paused" and not cards:
        step = "Координатору сверить прежние входы и кандидата перед сменой источника."
    if state == "stopped" and editor is None:
        step = "Общему координатору подтвердить остановку и допустимое продолжение по прежнему списку."
    return pause, editor, step
