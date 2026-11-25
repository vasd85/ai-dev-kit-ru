"""Build and verify inactive task cards, then explicitly select them."""

from datetime import date, datetime
from fcntl import LOCK_EX, LOCK_NB, flock
import os
from pathlib import Path, PurePosixPath
import re
from stat import S_IMODE
import tempfile

from migration_sources import GitLayers, LegacySources, changes, digest, encoded, inventory, read_json, read_review
from read_support import (
    BaseChecker, Problem, bad, canonical, emit, equal, json_record, obj, parse, relative_path, sequence,
    string, strings,
)
from records import contains
from task_actions import TaskActions, transition_status
from task_moves import open_directory, rename_exclusive
from task_reader import LIVE_PHASES, SCOPE_ID, full_id, profile_selection, read_history, switch_pause
from versions import CURRENT_FORMAT, LEGACY_FORMAT, TASK_FORMAT, record_format


BINDINGS = ("decisions", "mandates", "dependencies", "proposals")


def json_text(value):
    return encoded(value).decode("utf-8").rstrip()


def marker(name, value):
    return "<!-- ai-kit:" + name + " " + json_record(value) + " -->\n"


def historical(sources, work, key):
    string(key, work + "/history")
    entry = sources.history[work].get(key)
    if entry is None:
        bad(work, "нет исторического адреса " + key)
    address = entry["address"]
    return address, read_history(sources.base, address)


def read_time(value, where):
    string(value, where)
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            timestamp = None
        if timestamp is None or timestamp.utcoffset() is None:
            bad(where, "нужна дата ISO или время ISO с часовым поясом")
        return timestamp


def after(first, second):
    if isinstance(first, datetime) and isinstance(second, datetime):
        return first > second
    day = lambda value: value.date() if isinstance(value, datetime) else value
    return day(first) > day(second)


def read_bindings(path):
    data = obj(read_json(path), "bindings", [], BINDINGS) if path else {}
    for key, values in data.items():
        if not isinstance(values, dict):
            bad("bindings/" + key, "ожидается объект «номер → полный коммит»")
    return data


class Candidate:
    def __init__(self, copy, target, plan, snapshot, sources, bindings):
        self.copy, self.sources, self.plan = copy, sources, plan
        self.target, self.snapshot, self.bindings = target, snapshot, bindings
        self.files, self.cards, self.rows, self.folders = {}, {}, {}, {}
        self.scopes, self.scope_sources, self.parents = {}, {}, {}
        self.used_bindings = {key: set() for key in BINDINGS}
        self.by_old = {old: full_id(item["new"], "review/works/" + old + "/new")
                       for old, item in plan["works"].items()}
        if len(set(self.by_old.values())) != len(self.by_old):
            bad("review/works", "несколько исходных работ получили один полный номер")
        self.exceptions = self.read_exceptions()
        self.read_scopes()
        for old, new in sorted(self.by_old.items()):
            self.build_card(old, new)
        self.complete_cards()
        self.write_files()
        for key, values in bindings.items():
            equal(set(values), self.used_bindings[key], "bindings/" + key)

    def read_exceptions(self):
        result = []
        for item in sequence(self.plan.get("exceptions", []), "review/exceptions"):
            obj(item, "review/exceptions", ["level", "target", "owner", "reason", "next_step"])
            for key, value in item.items():
                string(value, "review/exceptions/" + key)
            if item["level"] not in ("task", "switch"):
                bad("review/exceptions", "уровень должен быть task или switch")
            result.append({**item, "target": self.by_old.get(item["target"], item["target"])})
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply",
                     "rebase-merge", "sequencer", "index.lock"):
            if self.snapshot["git"]["git_meta"][name] is not None:
                result.append(self.hold("P", self.plan["editor"], "Git: " + name,
                                        "Владелец Git устанавливает исход и состояние индекса.", "switch"))
        if any(row[2] != "0" for file in self.snapshot["git"]["files"].values()
               for row in file["index"]):
            result.append(self.hold("P", self.plan["editor"], "В индексе есть конфликт.",
                                    "Владелец Git завершает разбор конфликта.", "switch"))
        if self.sources.stopped:
            continuation = self.sources.states[self.sources.name]["continuation"]
            if (continuation.get("processes") != "none" or continuation.get("writer", "unknown") is not None
                    or continuation.get("next_action") != "inspect"):
                result.append(self.hold("P", self.plan["editor"],
                                        "Остановка старой попытки и освобождение права записи расходятся в источнике.",
                                        "Проверьте исход процесса, писателя и точку продолжения.", "switch"))
        return result

    @staticmethod
    def hold(target, owner, reason, step, level="task"):
        return {"level": level, "target": target, "owner": owner, "reason": reason, "next_step": step}

    def read_scopes(self):
        for scope, choice in self.plan["scopes"].items():
            if not re.fullmatch(SCOPE_ID, scope):
                bad("review/scopes", "неверная область " + scope)
            w = "review/scopes/" + scope
            name = choice["source"]
            if name not in self.sources.states or name in self.scope_sources:
                bad(w, "нужна одна прежняя запись на область")
            state = self.sources.states[name]
            equal(scope == "P", name == self.sources.name, w + "/source")
            old = choice["continuation"]
            in_scope = {key for key, value in self.sources.works.items() if value[0] == name}
            if not ((old is None and not in_scope) or (isinstance(old, str) and old in in_scope)):
                bad(w + "/continuation", "точка продолжения должна принадлежать этой области")
            if old is not None and self.by_old[old].split("/")[0] != scope:
                bad(w + "/continuation", "полный номер должен принадлежать этой области")
            editor = state.get("editor")
            if editor is None and "editor" in choice:
                editor = obj(choice["editor"], w + "/editor", ["role", "session"])
                for key, value in editor.items():
                    string(value, w + "/editor/" + key)
                if "coordinator" in state:
                    equal(editor["role"], state["coordinator"], w + "/editor/role")
            if editor is None:
                editor = {"role": state.get("coordinator", self.plan["editor"])}
            self.scopes[scope] = {**choice, "editor": editor}
            string(choice["next_step"], w + "/next_step")
            self.scope_sources[name] = scope
            if scope != "P":
                self.parents[scope] = self.by_old[state["parent"]["work"]]
        if "P" not in self.scopes:
            bad("review/scopes", "нет общей области P")

    def assignment(self, old, scope, assignment, row):
        if self.sources.version is None:
            current_decision = self.sources.current_decisions.get(old)
            return {"revision": 1, "description": row["goal"], "stage": "prepare",
                    "boundaries": self.plan["works"][old]["meaning"], "write_scope": [],
                    "authority": {"by": row["by"], "source": self.sources.name},
                    "acceptor": self.scopes[scope]["editor"]["role"], "criteria": row["criteria"],
                    **({"inputs": [current_decision]} if current_decision else {})}
        if assignment is None:
            return None
        required = ["revision", "goal", "boundaries", "authority", "acceptor", "inputs", "criteria"]
        if any(key not in assignment for key in required):
            bad(old + "/assignment", "в прежней записи отсутствуют обязательные сведения поручения")
        converted = {key: assignment[key] for key in ("revision", "boundaries", "authority",
                                                        "acceptor", "criteria")}
        converted["description"] = assignment["goal"]
        converted["stage"] = assignment.get("stage", "implement")
        converted["write_scope"] = (assignment.get("write_scope", assignment.get("write_files", []))
                                    if converted["stage"] == "implement" else [])
        converted["inputs"] = [name for name in self.snapshot["git"]["files"]
                               if any(contains(path, name) for path in assignment["inputs"])]
        if assignment.get("plan"):
            converted["plan"] = "\n".join(strings(assignment["plan"], old + "/plan"))
        local = self.sources.works[old][1].get("local_state")
        if local:
            converted["mandate"] = {"scope": self.scope_sources[local],
                                    "role": assignment["mandate"]["role"],
                                    "stages": assignment["mandate"]["stages"]}
        return converted

    def build_card(self, old, new):
        scope, number = new.split("/")
        name, raw, assignment = self.sources.works[old]
        choice = self.plan["works"][old]
        equal(scope, self.scope_sources[name], new + "/scope")
        converted = self.assignment(old, scope, assignment, raw)
        goal = raw.get("goal", raw.get("idea")) if assignment is None else assignment["goal"]
        title = raw.get("title", goal)
        card = {"format_version": TASK_FORMAT, "id": number, "title": title, "goal": goal,
                "origin": {"status": raw["status"], "source": {"task": new, "path": "legacy.json"}}}
        if scope in self.parents:
            card["parent"] = self.parents[scope]
        if converted is not None:
            card["assignment"] = converted
        grounds = sequence(choice["grounds"], new + "/grounds", nonempty=True)
        for item in grounds:
            obj(item, new + "/grounds", ["basis", "status", "reason", "next_step"])
            for key, value in item.items():
                string(value, new + "/grounds/" + key)
            if item["status"] not in ("applicable", "recheck", "wait"):
                bad(new + "/grounds", "используйте applicable, recheck или wait")
        if "accept" in choice:
            if converted is None:
                bad(new, "для приёмки нужно прежнее поручение")
            if "waiting" in choice:
                bad(new, "приёмка и обязательный остаток требуют разных решений")
            card["observations"], decision = self.acceptance(old, choice["accept"], converted)
            card["decisions"] = [decision]
        if "waiting" in choice:
            card["waiting"] = choice["waiting"]
            obj(card["waiting"], new + "/waiting", ["remaining", "reason", "next_step"])
            for key, value in card["waiting"].items():
                string(value, new + "/waiting/" + key)
            equal(choice["next_step"], card["waiting"]["next_step"], new + "/next_step")
            self.exceptions.append(self.hold(new, choice["owner"], card["waiting"]["reason"],
                                             card["waiting"]["next_step"]))
        if self.sources.stopped and "waiting" not in card:
            bad(new, "остановленный путь допускает разбор с явно сохранённым ожиданием")
        if "proposal" in choice:
            self.check_proposal(old, new, choice, card, raw)
        if not any(key in choice for key in ("accept", "waiting", "proposal")) and new not in self.parents.values():
            bad(new, "зафиксируйте следующий шаг и остаток либо предъявите приёмку или допустимую подготовку")
        if not self.sources.stopped:
            self.check_holds(old, new, raw, choice)
        self.cards[new] = card
        bucket = "done" if "decisions" in card else "open"
        area = self.area(scope)
        self.folders[new] = f"{area}/{bucket}/{number}"
        state = self.sources.states[name]
        iteration = raw.get("iteration", state.get("iteration"))
        if isinstance(iteration, dict):
            iteration = iteration["id"]
        self.rows[new] = {"old_source": name, "old_id": old, "scope": scope,
                          "old_scope": state.get("id", state.get("scope")), "iteration": iteration,
                          "new": new, "path": self.folders[new] + "/task.json", "old_status": raw["status"],
                          "meaning": choice["meaning"], "owner": choice["owner"], "grounds": grounds,
                          "remaining": card.get("waiting", {}).get("remaining"), "next_step": choice["next_step"],
                          "history": self.sources.history[old]}

    def check_holds(self, old, new, raw, choice):
        attempts = raw.get("attempts", [raw["attempt"]] if "attempt" in raw else [])
        for attempt in attempts:
            effects = attempt.get("effects", ["write"] if self.sources.version == LEGACY_FORMAT else [])
            # The single-task ready form has not started, but the old in-progress
            # record or a reserved multi-work writer needs settled ownership.
            reserved = attempt["phase"] in LIVE_PHASES and bool(set(effects) & {"write", "git", "external"})
            if self.sources.version == LEGACY_FORMAT and attempt["phase"] == "assigned":
                reserved = False
            if reserved or attempt.get("external_outcome") == "unknown":
                self.exceptions.append(self.hold(
                    new, choice["owner"], f"Не выяснено удержание или внешний исход попытки {attempt['id']}.",
                    "Установите состояние процесса, владельца ресурсов и внешний исход до переключения.",
                    "switch"))
        if self.sources.version == CURRENT_FORMAT:
            _, state, _, _ = self.sources.checker.works[old]
            if old in state.get("revision_review", {}).get("pending", []) and "waiting" not in choice:
                bad(new, "пересмотр существенного решения ещё не завершён; укажите ожидание")

    def check_proposal(self, old, new, choice, card, raw):
        proposal = obj(choice["proposal"], new + "/proposal", ["id", "executor"])
        for key, value in proposal.items():
            string(value, new + "/proposal/" + key)
        if (self.sources.version != CURRENT_FORMAT or "waiting" in card or "decisions" in card or
                card.get("assignment", {}).get("stage") != "prepare" or card["assignment"]["write_scope"] or
                not all(item["status"] == "applicable" for item in choice["grounds"])):
            bad(new, "допустима только подтверждённая подготовка без записи и ожиданий")
        checker = self.sources.checker
        _, state, _, _ = checker.works[old]
        selected = [action for action in state.get("actions", [])
                    if action["work"] == old and action["kind"] == "assign"]
        attempt = checker.attempts.get(raw.get("active_attempt"))
        if not selected or attempt is None or attempt[0]["effects"] != ["read"]:
            bad(new, "в прежней записи нет действующего назначения только для чтения")
        equal(proposal["executor"], attempt[0]["executor"], new + "/proposal/executor")

    def acceptance(self, old, spec, assignment):
        w = old + "/accept"
        decision_address, saved_decision = historical(self.sources, old, spec["decision"])
        evidence_address, evidence = historical(self.sources, old, spec["evidence"])
        observed_at, decided_at = (read_time(spec[key], w + "/" + key)
                                   for key in ("observed_at", "decided_at"))
        if after(observed_at, decided_at) or after(decided_at, read_time(self.snapshot["captured_at"], "snapshot")):
            bad(w, "даты наблюдения, решения и подготовки должны идти в этом порядке")
        if decision_address == evidence_address:
            bad(w, "нужны раздельные адреса решения и его свидетельства")
        if self.sources.version is None:
            accept = parse(saved_decision, w) if isinstance(saved_decision, bytes) else saved_decision
            obj(accept, w, ["id", "work", "by", "role", "observed_at", "decided_at", "criteria",
                            "fact", "evidence"])
            if current := self.sources.current_decisions.get(old):
                live = parse(self.sources.base.file(current, w).read_bytes(), current)
                if canonical(live) != canonical(accept):
                    bad(current, "действующее решение отличается от исторической приёмки. "
                        "Координатор разбирает изменение или отзыв и уточняет основание либо ожидание.")
            equal(accept["work"], old, w + "/work")
            for key in ("observed_at", "decided_at"):
                equal(accept[key], spec[key], w + "/" + key)
            equal(canonical(accept["evidence"]), canonical(evidence_address), w + "/evidence")
            equal(accept["role"], assignment["acceptor"], w + "/role")
            equal(set(strings(accept["criteria"], w + "/criteria", nonempty=True)),
                  {criterion["id"] for criterion in assignment["criteria"]}, w + "/criteria")
            observations = [{"id": "legacy-observation", "by": accept["by"], "criteria": accept["criteria"],
                             "fact": accept["fact"]}]
            reason = "Сохранено решение из прежнего списка и связанное с ним наблюдение."
        else:
            _, raw, _ = self.sources.works[old]
            equal(raw["status"], "accepted", w + "/old-status")
            if self.sources.version == CURRENT_FORMAT:
                checker = self.sources.checker
                accept, _, result, _ = checker.acceptances[raw["current_acceptance"]]
                checker.fresh(result["id"])
                checker.current_assignment(old, checker.results[result["id"]][2], w)
            else:
                accept, result = raw["acceptance"], raw["result"]
            equal(canonical(saved_decision), canonical(accept), w + "/decision")
            equal(canonical(evidence), canonical(result), w + "/evidence")
            if accept["unresolved"]:
                bad(w, "у прежней приёмки есть обязательный остаток; разберите его применимость")
            _, dates = historical(self.sources, old, spec.get("dates"))
            dates = parse(dates, w + "/dates") if isinstance(dates, bytes) else dates
            for key, expected in (("work", old), ("acceptance", accept["id"]),
                                  ("observed_at", spec["observed_at"]), ("decided_at", spec["decided_at"])):
                if not isinstance(dates, dict):
                    bad(w + "/dates", "нужна сохранённая запись дат для прежнего решения")
                equal(dates.get(key), expected, w + "/dates/" + key)
            observations = []
            for check in result["checks"]:
                if check["id"] not in accept["evidence"]:
                    continue
                covered = check.get("criteria")
                if covered is None:
                    covered = [item["criterion"] for item in check["covers"]
                               if item["work"] == old and item["assignment_revision"] == assignment["revision"]]
                if covered:
                    observations.append({
                        "id": check["id"], "by": result["executor"], "criteria": covered,
                        "fact": f"{check['observed']} Условия: {check['environment']}. Ожидание: {check['expected']}.",
                    })
            reason = accept["applicability"]
        for observation in observations:
            observation.update(date=spec["observed_at"], revision=assignment["revision"], source=evidence_address)
        return observations, {
            "id": accept["id"], "outcome": "accepted", "by": accept["by"], "role": assignment["acceptor"],
            "date": spec["decided_at"], "reason": reason, "revision": assignment["revision"],
            "observations": [value["id"] for value in observations], "source": decision_address,
        }

    def area(self, scope):
        area = self.target + "/tasks"
        return area if scope == "P" else area + "/features/" + scope

    def binding(self, kind, key, card):
        commit = self.bindings.get(kind, {}).get(key)
        if commit is None:
            return None
        self.copy.commit(commit, f"bindings/{kind}/{key}")
        path = self.folders[card] + "/task.json"
        self.copy.saved_file(commit, path)
        self.used_bindings[kind].add(key)
        return {"commit": commit, "path": path}

    def selected_bases(self, old, state, choice):
        if self.sources.version != CURRENT_FORMAT or "waiting" in choice:
            return {}
        kind = "assign" if "proposal" in choice else "handoff" if "accept" in choice else None
        return {(basis["work"], basis["criterion"]): basis
                for action in state.get("actions", []) if action["work"] == old and action["kind"] == kind
                for basis in action["bases"]}

    def confirmation(self, new, base, target_decision):
        checker = self.sources.checker
        result, raw, order, *_ = checker.results[base["result"]]
        confirmer = self.by_old[raw["id"]]
        accepted = (self.cards[confirmer].get("decisions") or [{}])[-1]
        proof = checker.acceptances.get(raw.get("current_acceptance"))
        if (not proof or proof[0]["id"] != accepted.get("id") or proof[0]["result"] != result["id"]
                or base["check"] not in proof[0]["evidence"]):
            bad(new + "/dependencies",
                f"результат {base['result']} и проверка {base['check']} требуют отдельного подтверждения. "
                "Сохраните выбранное основание или укажите ожидание.")
        choice = self.plan["works"][raw["id"]]["accept"]
        result_address = self.sources.history[raw["id"]][choice["evidence"]]["address"]
        assignment_address = {
            "commit": result["commit"], "path": self.sources.works[raw["id"]][0],
            "select": {"kind": "assignment", "work": raw["id"], "key": order["id"], "revision": order["revision"]},
        }
        criterion_address = {
            "commit": result["commit"], "path": self.sources.works[base["work"]][0],
            "select": {"kind": "criterion", "work": base["work"], "key": base["criterion"],
                       "revision": target_decision["revision"]},
        }
        return {"task": confirmer, "assignment": assignment_address, "result": result_address,
                "acceptance": accepted["source"], "criterion": criterion_address, "check": base["check"],
                **({"compatibility": base["compatibility"]} if "compatibility" in base else {})}

    def complete_cards(self):
        known = self.cards.keys() | self.scopes.keys()
        for issue in self.exceptions:
            if issue["target"] not in known:
                bad("review/exceptions", "неизвестная задача или область " + str(issue["target"]))
            if issue["level"] == "task":
                if issue["target"] not in self.cards or "waiting" not in self.cards[issue["target"]]:
                    bad("review/exceptions", "ограничение задачи должно быть видно в её waiting")
        for old, new in self.by_old.items():
            name, raw, assignment = self.sources.works[old]
            card, choice = self.cards[new], self.plan["works"][old]
            selected = self.selected_bases(old, self.sources.states[name], choice)
            for link in sequence((assignment or raw).get("dependencies", []), new + "/dependencies"):
                obj(link, new + "/dependencies", ["work", "criterion", "acceptance"])
                string(link["work"], new + "/dependencies/work")
                if link["work"] not in self.by_old:
                    bad(new, "зависимость от неизвестной прежней работы " + str(link["work"]))
                target = self.by_old[link["work"]]
                decision = (self.cards[target].get("decisions") or [{}])[-1]
                selection = link["acceptance"] if link["acceptance"] == decision.get("id") else None
                dependency = {"task": target, "criterion": link["criterion"], "acceptance": selection}
                if selection is not None and (basis := self.binding("dependencies", target, target)):
                    dependency["basis"] = basis
                if chosen := selected.get((link["work"], link["criterion"])):
                    if selection is None:
                        bad(new + "/dependencies", "сохраните выбранную приёмку обязательства или укажите ожидание")
                    dependency["confirmation"] = self.confirmation(new, chosen, decision)
                card.setdefault("dependencies", []).append(dependency)
            scope = self.scope_sources[name]
            if "decisions" in card:
                decision = card["decisions"][-1]
                if basis := self.binding("decisions", new, new):
                    decision["basis"] = basis
                if scope in self.parents and (mandate := self.scope_mandate(scope)):
                    decision["mandate"] = mandate
            if "proposal" in choice and (basis := self.binding("proposals", new, new)):
                card["attempts"] = [{**choice["proposal"], "revision": card["assignment"]["revision"],
                                     "phase": "proposed", "resources": [], "basis": basis}]

    def scope_mandate(self, scope):
        parent = self.parents[scope]
        basis = self.binding("mandates", scope, parent)
        return {"task": parent, "basis": basis} if basis else None

    def write_files(self):
        profile = self.target + "/profile.md"
        self.files[profile] = ("# Профиль для проверки на копии\n\n"
                               "Действующий вход указан в снимке подготовки.\n\n" +
                               marker("task-source", {"format_version": TASK_FORMAT,
                                                       "path": self.target + "/tasks"})).encode()
        for scope, choice in sorted(self.scopes.items()):
            area = self.area(scope)
            editor = choice["editor"]
            text = (f"# Область {scope}\n\nПрежний источник: {choice['source']}\n\n"
                    f"Ответственный: {editor['role']}\n\nСледующий шаг: {choice['next_step']}\n")
            if choice["continuation"] is not None:
                continuation = self.by_old[choice["continuation"]]
                text += f"\nТочка продолжения: {continuation}\n"
                if "session" in editor:
                    scope_data = {"editor": {k: editor[k] for k in ("role", "session")},
                                  "continuation": continuation}
                    if scope == "P":
                        scope_data["resources"] = area + "/resources.json"
                        self.files[scope_data["resources"]] = encoded({"editor": editor["session"], "grants": []})
                    elif mandate := self.scope_mandate(scope):
                        scope_data["mandate"] = mandate
                    if "resources" in scope_data or "mandate" in scope_data:
                        text += "\n" + marker("scope", scope_data)
            self.files[area + "/scope.md"] = text.encode("utf-8")
            self.files[area + "/legacy-scope.json"] = encoded({
                "source": choice["source"], "old": self.sources.states[choice["source"]],
                "review": choice, "old_file": self.snapshot["git"]["files"][choice["source"]],
            })
        for old, new in sorted(self.by_old.items()):
            card, row = self.cards[new], self.rows[new]
            name, raw, assignment = self.sources.works[old]
            choice = self.plan["works"][old]
            folder = self.folders[new]
            results = []
            if self.sources.version == CURRENT_FORMAT:
                results = [result for result, work, *_ in self.sources.checker.results.values()
                           if work["id"] == old]
            material = {}
            for path in choice.get("materials", []):
                content = self.copy.file(path, new + "/materials").read_bytes()
                equal(digest(content), self.snapshot["git"]["files"][path]["work"]["sha256"], path)
                local = "materials/" + path
                self.files[folder + "/" + local] = content
                material[path] = {"card_path": local, **self.snapshot["git"]["files"][path]}
            self.files[folder + "/legacy.json"] = encoded({
                "mapping": row, "old_work": raw, "assignment": assignment, "results": results,
                "source_layers": self.snapshot["git"]["files"][name], "materials": material,
            })
            self.files[folder + "/task.json"] = encoded(card)
        self.files[self.target + "/preparation.json"] = encoded({
            "snapshot": self.snapshot, "editor": self.plan["editor"], "mapping": list(self.rows.values()),
            "exceptions": self.exceptions,
        })

    def check_actions(self):
        reader = TaskActions(self.copy.root, self.target + "/tasks")
        status = reader.check()
        profile = self.target + "/profile.md"
        checked, problems = [], []
        expected = {new for new, card in self.cards.items() if "decisions" in card}
        for old, new in self.by_old.items():
            choice = self.plan["works"][old]
            action = "handoff" if new in expected else "assign" if "proposal" in choice else None
            if action is None:
                continue
            editor = self.scopes[new.split("/")[0]]["editor"]
            try:
                result = reader.action(profile, new, action, editor.get("session"),
                                       choice["proposal"]["id"] if action == "assign" else None)
            except Problem as error:
                problems.append(self.hold(new, choice["owner"], f"{error.where}: {error.message}",
                                          "Сверьте сохранённые снимки на копии или укажите ожидание.", "switch"))
            else:
                checked.append(result)
        return status, checked, problems


def current_files(copy, target):
    directory = copy.local(target, "target", must_exist=False)
    if not directory.exists():
        return None
    if not directory.is_dir():
        bad(target, "путь занят, выберите свободную папку кандидата")
    result = {}
    for path in directory.rglob("*"):
        name = path.relative_to(copy.root).as_posix()
        path = copy.local(name, "target")
        if not path.is_dir():
            result[name] = copy.file(name, "target").read_bytes()
    return result


def prepare(copy, target, desired, previous):
    parent = copy.local(PurePosixPath(target).parent.as_posix(), "target/parent")
    if not parent.is_dir():
        bad(target, "выделите существующую родительскую папку на копии")
    with open_directory(parent) as destination:
        try:
            flock(destination, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            bad(target, "подготовка в этой папке уже идёт; дождитесь её завершения")
        current = current_files(copy, target)
        if current == desired:
            return "identical"
        if current is not None and (previous is None or current.keys() != desired.keys() or any(
                content not in (previous.get(name), desired[name]) for name, content in current.items())):
            bad(target, "кандидат отличается от ожидаемой редакции; сохраните его и проверьте расхождение")
        with tempfile.TemporaryDirectory(prefix=".migration-", dir=parent) as scratch:
            staged = Path(scratch) / "candidate"
            staged.mkdir()
            for name, content in desired.items():
                path = staged / PurePosixPath(name).relative_to(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            TaskActions(copy.root, (staged / "tasks").relative_to(copy.root).as_posix()).check()
            if current is None:
                with open_directory(Path(scratch)) as source:
                    rename_exclusive(source, "candidate", destination, PurePosixPath(target).name)
                return "created"
            for name, content in desired.items():
                if content != current[name]:
                    path = copy.file(name, "target")
                    with open_directory(path.parent) as folder:
                        os.replace(staged / PurePosixPath(name).relative_to(target), path.name, dst_dir_fd=folder)
            return "bound"


def drift(snapshot, origin, copy):
    return {
        "origin": changes(snapshot["git"], origin.snapshot(snapshot["paths"])),
        "copy": changes(snapshot["git"], copy.snapshot(snapshot["paths"]), copy=True),
    }


def report_drift(differences):
    emit(json_text({"status": "stale", "changes": differences,
                    "next_step": "Установите изменённое основание и повторите подготовку на свежей копии."}))
    return 1


def separate_roots(origin, copy):
    initial = BaseChecker(origin, None).root
    candidate = BaseChecker(copy, None).root
    if initial.samefile(candidate):
        bad("root", "исходная установка и копия указывают на один физический каталог; "
            "выберите отдельную копию")
    if (any(parent.samefile(initial) for parent in candidate.parents)
            or any(parent.samefile(candidate) for parent in initial.parents)):
        bad("root", "исходная установка и копия вложены друг в друга; выберите отдельный корень")
    return initial, candidate


def report_candidate_conflicts(git, editor):
    issues = []
    for path, entries in sorted(git.excluded_conflicts.items()):
        issue = Candidate.hold(
            "P", editor, f"Нерешённый конфликт индекса Git: {path}.",
            f"Владелец Git сверяет конфликтующие версии {path}, согласует файл и отмечает решение в Git.",
            "switch")
        issues.append({**issue, "path": path, "stages": [row[2] for row in entries]})
    emit(json_text({"status": "blocked", "exceptions": issues}))
    return 1


def switch_status(root, plan, target, args):
    reader = BaseChecker(root, None)
    profile = plan["profile"]
    profile_path = reader.file(profile, "profile")
    text = profile_path.read_bytes().decode("utf-8")
    selected = profile_selection(text, profile, optional=True)
    if selected is None:
        old = plan["source"]
        version = None
        if old["kind"] == "state":
            data = parse(reader.file(old["path"], "state").read_bytes(), old["path"])
            version = record_format(data.get("format_version")) if isinstance(data, dict) else None
        selected = {"format_version": version, "path": old["path"]}
    if selected.get("pause") is not None:
        equal(args.pause, selected["pause"], profile + "/pause")
    cards = selected["format_version"] == TASK_FORMAT
    if cards:
        equal(selected["path"], target + "/tasks", profile + "/path")
        equal(selected.get("pause"), args.pause, profile + "/pause")
    pause, editor, step = transition_status(reader, profile, selected, args.pause, target=target + "/tasks")
    if cards or pause["state"] == "stopped":
        if editor and args.actor != editor["session"]:
            bad(profile + "/actor", f"Повтор выполняет общий редактор «{editor['role']}» ({editor['session']}).")
        if editor is None:
            equal(args.actor, pause["actor"], args.pause + "/actor")
        if pause["state"] == "stopped":
            bad(args.pause, f"Профиль {profile} выбирает {selected['path']}. Переход остановлен. "
                f"Следующий шаг — {step}")
        emit(json_text({"status": "already-selected", "active_profile": profile,
                        "source": selected["path"], "issuance": pause["state"],
                        "editor": editor, "next_step": step}))
        return None
    equal(args.actor, pause["actor"], args.pause + "/actor")
    return text, pause


def switch_source(args, candidate, origin_root, before):
    copy, target, profile = candidate.copy, candidate.target, candidate.plan["profile"]
    text, pause = before
    editor = candidate.scopes["P"]["editor"]
    equal(editor.get("session"), args.actor, profile + "/actor")
    equal(editor["role"], candidate.plan["editor"], profile + "/editor")
    # A legacy profile can be a required byte-for-byte input. The product
    # can select its source in a separate profile while retaining that input.
    path = copy.file(profile, "profile")
    protected = {candidate.sources.name}
    protected.update(state["profile"] for state in candidate.sources.states.values()
                     if "profile" in state)
    for _, _, assignment in candidate.sources.works.values():
        if assignment:
            protected.update(assignment["inputs"])
    old_inputs = [copy.local(name, "legacy/input", must_exist=False) for name in protected]
    if any(old.exists() and (path.samefile(old) or
           old.is_dir() and any(parent.samefile(old) for parent in path.parents)) for old in old_inputs):
        bad(profile, "действующий профиль входит в прежние записи или обязательные входы. "
            "Сохраните его байты, настройте отдельный профиль выбора со старым источником и повторите подготовку.")
    if contains(target, args.pause) or args.pause == profile:
        bad(args.pause, "вынесите запись остановки за пределы профиля и папки кандидата")
    source = {"format_version": TASK_FORMAT, "path": target + "/tasks", "pause": args.pause,
              "prepared": {"profile": target + "/profile.md", "commits": sorted({
                  commit for key in ("decisions", "mandates", "proposals")
                  for commit in candidate.bindings.get(key, {}).values()
              })}}
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip().startswith("<!-- ai-kit:task-source"):
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = marker("task-source", source).rstrip("\n") + ending
            break
    else:
        if text and not text.endswith("\n"):
            lines.append("\n")
        lines.append(marker("task-source", source))
    with open_directory(path.parent) as directory:
        try:
            flock(directory, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            bad(profile, "изменение профиля уже идёт; дождитесь завершения и перечитайте источник")
        with tempfile.TemporaryDirectory(prefix=".migration-switch-", dir=path.parent) as scratch:
            staged = Path(scratch) / path.name
            with staged.open("wb") as output:
                output.write("".join(lines).encode("utf-8"))
                os.fchmod(output.fileno(), S_IMODE(path.stat().st_mode))
                output.flush()
                os.fsync(output.fileno())
            fresh = GitLayers(copy.root, exclude=target)
            if fresh.excluded_conflicts:
                return report_candidate_conflicts(fresh, candidate.plan["editor"])
            actual = current_files(fresh, target)
            if actual != candidate.files:
                return report_candidate_drift(candidate, actual)
            differences = drift(candidate.snapshot, GitLayers(origin_root), fresh)
            if any(differences.values()):
                return report_drift(differences)
            equal(switch_pause(fresh, profile, source["path"], args.pause), pause, args.pause)
            equal(path.read_bytes(), text.encode("utf-8"), profile)
            os.replace(staged, path.name, dst_dir_fd=directory)
    emit(json_text({"status": "selected", "active_profile": profile, "source": source["path"],
                    "previous_source": candidate.sources.name, "issuance": "paused",
                    "next_step": pause["next_step"]}))
    return 0


def report_candidate_drift(candidate, actual):
    paths = set(candidate.files) | (actual or {}).keys()
    emit(json_text({"status": "stale", "changes": sorted(
        name for name in paths if (actual or {}).get(name) != candidate.files.get(name)),
        "next_step": "Сопоставьте отличающиеся файлы с проверенной сборкой."}))
    return 1


def run(args, state_checker):
    if args.phase != "inventory":
        origin_root, copy_root = separate_roots(args.origin, args.root)
    plan, plan_hash = read_review(args.review)
    if args.phase == "inventory":
        origin = GitLayers(args.root)
        sources = LegacySources(origin, plan, state_checker)
        emit(json_text(inventory(origin, plan, plan_hash, sources)))
        return 0
    origin = GitLayers(origin_root)
    target = relative_path(args.target, "target").as_posix()
    if target == "." or origin.local(target, "target", must_exist=False).exists() or any(
            contains(target, name) for name in origin.tree_entries.keys() | origin.index.keys()):
        bad("target", "выберите папку кандидата, свободную в прежней установке")
    before = None
    if args.phase == "switch":
        before = switch_status(copy_root, plan, target, args)
        if before is None:
            return 0
    copy = GitLayers(copy_root, exclude=target)
    if copy.excluded_conflicts:
        return report_candidate_conflicts(copy, plan["editor"])
    snapshot = read_json(args.snapshot)
    obj(snapshot, "snapshot", ["captured_at", "review_sha256", "entry", "profile", "source", "selection",
                               "source_label", "format", "old_diagnostic", "paths", "history", "links", "git"])
    equal(plan_hash, snapshot["review_sha256"], "snapshot/review_sha256")
    strings(snapshot["paths"], "snapshot/paths", nonempty=True)
    obj(snapshot["git"], "snapshot/git", ["head", "tree_sha256", "index_entries_sha256",
                                        "index_file_sha256", "git_meta", "files"])
    if any(contains(target, name) or contains(name, target) for name in snapshot["paths"]):
        bad("target", "папка кандидата пересекает область прежних входов")
    differences = drift(snapshot, origin, copy)
    if any(differences.values()):
        return report_drift(differences)
    copy.git("merge-base", "--is-ancestor", snapshot["git"]["head"], copy.head)
    sources = LegacySources(origin, plan, state_checker)
    equal(canonical(sources.history), canonical(snapshot["history"]), "snapshot/history")
    copied = LegacySources(copy, plan, state_checker)
    equal(canonical(copied.history), canonical(snapshot["history"]), "copy/history")
    equal(canonical(sources.diagnostic), canonical(snapshot["old_diagnostic"]), "snapshot/old_diagnostic")
    equal((sources.selection, sources.label, sources.version, sorted(sources.files), sources.links),
          (snapshot["selection"], snapshot["source_label"], snapshot["format"], snapshot["paths"], snapshot["links"]),
          "snapshot/selected-source")
    candidate = Candidate(copy, target, plan, snapshot, sources, read_bindings(args.bindings))
    output = {"status": "assembled", "candidate_profile": target + "/profile.md",
              "active_profile": plan["profile"], "counts": {scope: 0 for scope in candidate.scopes},
              "exceptions": candidate.exceptions}
    for number in candidate.cards:
        output["counts"][number.split("/")[0]] += 1
    if args.phase == "prepare":
        previous = None
        if args.previous_bindings or args.bindings:
            previous = Candidate(copy, target, plan, snapshot, sources,
                                 read_bindings(args.previous_bindings)).files
        output["preparation"] = prepare(copy, target, candidate.files, previous)
        TaskActions(copy.root, target + "/tasks").check()
    else:
        actual = current_files(copy, target)
        if actual != candidate.files:
            return report_candidate_drift(candidate, actual)
        output["card_check"], output["actions"], problems = candidate.check_actions()
        checked_copy = GitLayers(copy_root, exclude=target)
        if checked_copy.excluded_conflicts:
            return report_candidate_conflicts(checked_copy, plan["editor"])
        differences = drift(snapshot, GitLayers(origin_root), checked_copy)
        if any(differences.values()):
            return report_drift(differences)
        output["exceptions"] = candidate.exceptions + problems
        output["status"] = "ready"
    if any(item["level"] == "switch" for item in output["exceptions"]):
        output["status"] = "blocked"
    elif args.phase == "switch":
        return switch_source(args, candidate, origin_root, before)
    emit(json_text(output))
    return 1 if output["status"] == "blocked" else 0
