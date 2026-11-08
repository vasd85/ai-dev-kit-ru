"""0.1-p2 records. Declarative checks only; no execution or state transitions."""

import os
from pathlib import PurePosixPath
import re
from fractions import Fraction

from read_support import BaseChecker, bad, canonical, equal, number, obj, parse, revision, string, strings, unsupported

VERSION = "0.1-p2"


def sequence(value, where, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        bad(where, "ожидается " + ("непустой " if nonempty else "") + "список")
    return value


def quantity(value, where):
    number(value, where)
    # Exact arithmetic on the decimal values in these manually supplied records.
    return Fraction(str(value))


def contains(scope, name):
    return name == scope or name.startswith(scope + "/")


def allowed_effects(assignment, attempt, where):
    if assignment["stage"] not in ("prepare", "implement"):
        bad(where + "/stage", "неизвестный этап выбранного результата")
    if assignment["stage"] == "prepare" and set(attempt["effects"]) - {"read"}:
        bad(where + "/effects", "подготовка не разрешает реализацию, Git или внешнее действие")


class RecordsChecker(BaseChecker):
    def __init__(self, root, state):
        super().__init__(root, state)
        self.states, self.works, self.attempts = {}, {}, {}
        self.results, self.acceptances, self.sources = {}, {}, set()
        self.decisions, self.limits = {}, {}
        self.snapshots, self.trees = {}, {}

    def load(self, name, where):
        path = self.file(name, where)
        if path in self.sources:
            bad(where, "повтор действующего источника: " + name)
        self.sources.add(path)
        return parse(path.read_bytes(), name)

    def source(self, value, where, ref=None):
        if isinstance(value, dict) and "url" in value:
            obj(value, where, ["url"])
            string(value["url"], where + "/url")
            unsupported(where, "внешний URL не проверен; обязательный источник нужен локально")
        if isinstance(value, dict) and "path" in value:
            obj(value, where, ["path"])
            if ref is None:
                return self.load(value["path"], where + "/path")
            return self.saved_json(ref, value["path"])
        return value

    def scoped_paths(self, values, where, nonempty=True, must_exist=True):
        strings(values, where, nonempty=nonempty)
        for i, name in enumerate(values):
            if name == ".":
                bad(where, "весь корень не является областью входов по умолчанию")
            path = self.local(name, f"{where}/{i}", must_exist=must_exist)
            if not path.exists() and not must_exist:
                continue
            if path.is_dir():
                # Check every descendant before any later Git/content read.
                for parent, dirs, files in os.walk(path, followlinks=False):
                    for child in dirs + files:
                        p = self.local((os.path.relpath(parent, self.root) + "/" + child), where)
                        if not p.is_dir() and not p.is_file():
                            unsupported(where, "необычный файл внутри области")
            elif not path.is_file():
                unsupported(where, "необычный вход")
        return values

    def check(self):
        self.read_state(self.state_name, None)
        self.product = self.states[self.state_name]
        equal(self.product["scope"], "product", self.state_name + "/scope")
        self.repository()
        self.read_results()
        self.read_acceptances()
        self.dependencies()
        self.coordination()
        self.resources()
        self.actions()
        return f"{VERSION}; {len(self.works)} работ; только заявленные действия"

    def read_state(self, name, parent):
        s = self.load(name, name)
        obj(s, name, ["format_version", "id", "revision", "scope", "profile", "editor",
                      "iterations", "works", "continuation"],
            ["parent", "actions", "write_owner", "git_owner", "decisions", "revision_review",
             "forecast", "real_limits", "usage_views", "external_links"])
        if s["format_version"] != VERSION:
            unsupported(name + "/format_version", "поддерживается только " + VERSION)
        if s["scope"] not in ("product", "feature"):
            unsupported(name + "/scope", "поддержаны product и feature")
        self.ident(s["id"], name + "/id")
        revision(s["revision"], name + "/revision")
        self.file(s["profile"], name + "/profile")
        editor = obj(s["editor"], name + "/editor", ["role", "session", "status"])
        for k in ("role", "session"):
            string(editor[k], name + "/editor/" + k)
        if editor["status"] not in ("active", "stopped"):
            bad(name + "/editor/status", "нужны active или stopped")
        if parent is None:
            equal(s["scope"], "product", name + "/scope")
            if "parent" in s:
                bad(name + "/parent", "общая область не имеет родителя")
        else:
            equal(s["scope"], "feature", name + "/scope")
            p = obj(s.get("parent"), name + "/parent", ["work", "assignment_revision", "commit"])
            equal(p["work"], parent, name + "/parent/work")
            revision(p["assignment_revision"], name + "/parent/assignment_revision")
            string(p["commit"], name + "/parent/commit")
        c = obj(s["continuation"], name + "/continuation",
                ["owner", "last_completed", "unverified", "next_step"])
        for k in ("owner", "last_completed", "next_step"):
            string(c[k], name + "/continuation/" + k)
        strings(c["unverified"], name + "/continuation/unverified")
        self.states[name] = s
        for i, d in enumerate(sequence(s.get("decisions", []), name + "/decisions")):
            w = f"{name}/decisions/{i}"
            obj(d, w, ["id", "revision", "source"])
            if parent is not None:
                bad(w, "общие решения принадлежат общему источнику")
            self.ident(d["id"], w + "/id")
            revision(d["revision"], w + "/revision")
            self.file(d["source"], w + "/source")
            self.decisions[d["id"]] = d
        its = set()
        for i, it in enumerate(sequence(s["iterations"], name + "/iterations", True)):
            w = f"{name}/iterations/{i}"
            obj(it, w, ["id", "goal", "status"], ["integration", "release"])
            self.ident(it["id"], w + "/id")
            string(it["goal"], w + "/goal")
            if it["status"] not in ("planned", "active", "integrated", "released"):
                bad(w + "/status", "неизвестное состояние итерации")
            its.add(it["id"])
        locals_to_read = []
        for i, task in enumerate(sequence(s["works"], name + "/works")):
            w = f"{name}/works/{i}"
            obj(task, w, ["id", "kind", "status", "iteration"],
                ["idea", "assignment", "attempts", "active_attempt", "acceptances",
                 "current_acceptance", "local_state", "view", "blocker", "closure"])
            self.ident(task["id"], w + "/id")
            if task["kind"] not in ("task", "question", "feature", "integration"):
                unsupported(w + "/kind", "неизвестный вид работы")
            if task["status"] not in ("candidate", "ready", "in_progress", "submitted", "accepted",
                                      "blocked", "paused", "cancelled", "revise"):
                unsupported(w + "/status", "неизвестное состояние работы")
            string(task["iteration"], w + "/iteration")
            if task["iteration"] not in its:
                bad(w + "/iteration", "неизвестная итерация в этой области")
            self.works[task["id"]] = (task, s, name, w)
            if task["status"] == "candidate":
                string(task.get("idea"), w + "/idea")
                if set(task) - {"id", "kind", "status", "iteration", "idea"}:
                    bad(w, "идея не содержит назначения или результатов")
                continue
            a = self.source(task.get("assignment"), w + "/assignment")
            self.assignment(a, s, w + "/assignment", task["kind"])
            task["_assignment"] = a
            attempts = sequence(task.get("attempts", []), w + "/attempts")
            if task.get("active_attempt") is not None:
                string(task["active_attempt"], w + "/active_attempt")
            for j, attempt in enumerate(attempts):
                aw = f"{w}/attempts/{j}"
                obj(attempt, aw, ["id", "assignment_revision", "executor", "phase", "effects"],
                    ["termination", "result", "external_outcome"])
                self.ident(attempt["id"], aw + "/id")
                revision(attempt["assignment_revision"], aw + "/assignment_revision")
                string(attempt["executor"], aw + "/executor")
                if attempt["phase"] not in ("assigned", "running", "unknown", "stopped", "finished"):
                    bad(aw + "/phase", "неизвестная фаза исполнения")
                strings(attempt["effects"], aw + "/effects", True)
                if not set(attempt["effects"]) <= {"read", "write", "git", "external"}:
                    unsupported(aw + "/effects", "неизвестный эффект исполнения")
                if attempt["phase"] in ("stopped", "finished"):
                    t = obj(attempt.get("termination"), aw + "/termination", ["by", "fact"])
                    for k in t:
                        string(t[k], aw + "/termination/" + k)
                elif "termination" in attempt:
                    bad(aw, "прекращение ещё не установлено")
                if "external" in attempt["effects"]:
                    if attempt.get("external_outcome") not in ("unknown", "confirmed", "not-performed"):
                        bad(aw + "/external_outcome", "нужен установленный исход либо unknown")
                elif "external_outcome" in attempt:
                    bad(aw, "исход не относится к локальной попытке")
                self.attempts[attempt["id"]] = (attempt, task, aw)
            if task["status"] in ("blocked", "paused", "cancelled", "revise"):
                b = obj(task.get("blocker"), w + "/blocker", ["reason", "next_step"])
                for k in b:
                    string(b[k], w + "/blocker/" + k)
            if task["kind"] == "feature":
                if parent is not None:
                    unsupported(w, "вложенная область функции не входит в эту версию")
                local = task.get("local_state")
                self.file(local, w + "/local_state")
                locals_to_read.append((local, task["id"]))
            elif "local_state" in task or "view" in task or "closure" in task:
                bad(w, "местное состояние/сводка/закрытие принадлежат функции")
        for local, wid in locals_to_read:
            self.read_state(local, wid)
        if "forecast" in s:
            f = obj(s["forecast"], name + "/forecast", ["estimate"], ["observed", "unit", "analysis"])
            number(f["estimate"], name + "/forecast/estimate")
            if f.get("observed") is not None:
                number(f["observed"], name + "/forecast/observed")
                if f["observed"] > f["estimate"]:
                    self.infos.append(name + ": прогноз превышен; нужен разбор прогресса, это не отказ")
            for k in ("unit", "analysis"):
                if k in f:
                    string(f[k], name + "/forecast/" + k)
        for url in strings(s.get("external_links", []), name + "/external_links"):
            if not re.match(r"https?://[^\s]+$", url):
                bad(name + "/external_links", "ожидается URL HTTP(S)")
            self.infos.append(name + ": внешний URL не проверен и не загружен: " + url)

    def assignment(self, a, s, w, kind):
        obj(a, w, ["id", "revision", "goal", "boundaries", "inputs", "write_scope", "criteria",
                   "plan", "acceptor", "stage", "authority"], ["dependencies", "decisions", "mandate"])
        self.ident(a["id"], w + "/id")
        revision(a["revision"], w + "/revision")
        for k in ("goal", "boundaries", "acceptor"):
            string(a[k], w + "/" + k)
        equal(a["acceptor"], s["editor"]["role"], w + "/acceptor")
        if a["stage"] not in ("prepare", "implement"):
            bad(w + "/stage", "разрешены prepare или implement")
        self.scoped_paths(a["inputs"], w + "/inputs", must_exist=False)
        self.scoped_paths(a["write_scope"], w + "/write_scope", False, must_exist=False)
        if any(not any(contains(p, q) for p in a["inputs"]) for q in a["write_scope"]):
            bad(w + "/write_scope", "отрезок записи должен входить в область входов")
        au = obj(a["authority"], w + "/authority", ["by", "source"])
        string(au["by"], w + "/authority/by")
        # This record can be unselected history. Existence is checked at the
        # result's commit, or now when the assignment authorizes an action.
        self.local(au["source"], w + "/authority/source", must_exist=False)
        for p in (s["profile"], au["source"]):
            if not any(contains(q, p) for q in a["inputs"]):
                bad(w + "/inputs", "обязательный вход не зарегистрирован: " + p)
        strings(a["plan"], w + "/plan", True)
        for i, c in enumerate(sequence(a["criteria"], w + "/criteria", True)):
            cw = f"{w}/criteria/{i}"
            obj(c, cw, ["id", "expectation"])
            self.ident(c["id"], cw + "/id")
            string(c["expectation"], cw + "/expectation")
        seen = set()
        for i, d in enumerate(sequence(a.get("dependencies", []), w + "/dependencies")):
            dw = f"{w}/dependencies/{i}"
            obj(d, dw, ["work", "acceptance", "criterion"])
            for k in ("work", "criterion"):
                string(d[k], dw + "/" + k)
            if d["acceptance"] is not None:
                string(d["acceptance"], dw + "/acceptance")
            key = (d["work"], d["criterion"])
            if key in seen:
                bad(dw, "повтор зависимости")
            seen.add(key)
        seen = set()
        for d in sequence(a.get("decisions", []), w + "/decisions"):
            obj(d, w + "/decisions", ["id", "revision"])
            string(d["id"], w + "/decisions/id")
            revision(d["revision"], w + "/decisions/revision")
            if d["id"] in seen:
                bad(w + "/decisions", "повтор решения")
            seen.add(d["id"])
        if kind == "feature":
            m = obj(a.get("mandate"), w + "/mandate", ["role", "stages", "interfaces", "closing"])
            string(m["role"], w + "/mandate/role")
            strings(m["stages"], w + "/mandate/stages", True)
            if not set(m["stages"]) <= {"prepare", "implement"}:
                bad(w + "/mandate/stages", "неизвестный этап")
            if a["stage"] == "prepare" and "implement" in m["stages"]:
                bad(w + "/mandate/stages", "общее prepare не разрешает местный implement")
            for k in ("interfaces", "closing"):
                string(m[k], w + "/mandate/" + k)
        elif "mandate" in a:
            bad(w + "/mandate", "мандат относится только к функции")

    def saved_json(self, ref, name):
        # Check the local spelling/boundary even when content comes from Git.
        self.local(name, "Git:source", must_exist=False)
        return parse(self.git("cat-file", "blob", ref + ":" + name), "Git:" + ref + ":" + name)

    def snapshot(self, ref, wid):
        key = (ref, wid)
        if key not in self.snapshots:
            task, _, name, w = self.works[wid]
            saved = self.saved_json(ref, name)
            if not isinstance(saved, dict) or saved.get("format_version") != VERSION:
                bad(w, "нет поддержанного состояния в Git")
            matches = [x for x in sequence(saved.get("works"), "Git:works")
                       if isinstance(x, dict) and x.get("id") == wid]
            if len(matches) != 1:
                bad(w, "в Git нет единственной работы " + wid)
            old = matches[0]
            a = self.source(old.get("assignment"), "Git:assignment", ref)
            obj(a, w + "/Git:assignment",
                ["id", "revision", "goal", "boundaries", "inputs", "write_scope", "criteria",
                 "plan", "acceptor", "stage", "authority"], ["dependencies", "decisions", "mandate"])
            revision(a["revision"], w + "/Git:assignment/revision")
            string(a["id"], w + "/Git:assignment/id")
            strings(a["inputs"], w + "/Git:assignment/inputs", True)
            for criterion in sequence(a["criteria"], w + "/Git:assignment/criteria", True):
                obj(criterion, w + "/Git:assignment/criteria", ["id", "expectation"])
                string(criterion["id"], w + "/Git:assignment/criteria/id")
            self.snapshots[key] = (old, a)
        return self.snapshots[key]

    def commit(self, value, w):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            bad(w, "нужен полный идентификатор Git-коммита")
        equal(self.git("cat-file", "-t", value).strip(), b"commit", w)

    def read_results(self):
        for aid, (at, task, aw) in self.attempts.items():
            if "result" not in at:
                continue
            if at["phase"] not in ("finished", "stopped"):
                bad(aw + "/result", "результат требует установленного прекращения исполнения")
            r = self.source(at["result"], aw + "/result")
            w = aw + "/result"
            obj(r, w, ["id", "revision", "assignment_id", "assignment_revision", "attempt_id",
                       "executor", "summary", "commit", "artifacts", "checks", "unverified",
                       "limitations", "next_step"])
            self.ident(r["id"], w + "/id")
            revision(r["revision"], w + "/revision")
            equal(r["attempt_id"], aid, w + "/attempt_id")
            equal(r["executor"], at["executor"], w + "/executor")
            equal(r["assignment_revision"], at["assignment_revision"], w + "/assignment_revision")
            for k in ("summary", "next_step"):
                string(r[k], w + "/" + k)
            for k in ("unverified", "limitations"):
                strings(r[k], w + "/" + k)
            self.commit(r["commit"], w + "/commit")
            old, a = self.snapshot(r["commit"], task["id"])
            authority = obj(a["authority"], w + "/authority", ["by", "source"])
            string(authority["by"], w + "/authority/by")
            self.local(authority["source"], w + "/authority/source", must_exist=False)
            if not any(contains(p, authority["source"]) for p in a["inputs"]):
                bad(w + "/authority/source", "источник разрешения не включён в исторические входы")
            entry = self.git_tree(r["commit"]).get(authority["source"])
            if entry is None or entry[0] not in (b"100644", b"100755") or entry[1] != b"blob":
                bad(w + "/authority/source", "источник разрешения отсутствует в проверенном коммите или не является обычным файлом")
            equal(r["assignment_id"], a.get("id"), w + "/assignment_id")
            equal(r["assignment_revision"], a.get("revision"), w + "/assignment_revision")
            saved_at = [x for x in sequence(old.get("attempts", []), "Git:attempts")
                        if isinstance(x, dict) and x.get("id") == aid]
            if len(saved_at) != 1:
                bad(w, "назначенная попытка отсутствует в проверенном коммите")
            for k in ("id", "assignment_revision", "executor", "effects"):
                equal(at[k], saved_at[0].get(k), w + "/" + k)
            own = {(task["id"], a["revision"], c["id"]) for c in a["criteria"]}
            covered = set()
            checks = {}
            for i, check in enumerate(sequence(r["checks"], w + "/checks", True)):
                cw = f"{w}/checks/{i}"
                obj(check, cw, ["id", "covers", "command", "cwd", "expected", "observed",
                               "outcome", "environment"])
                self.ident(check["id"], cw + "/id")
                for k in ("command", "expected", "observed", "environment"):
                    string(check[k], cw + "/" + k)
                self.local(check["cwd"], cw + "/cwd", must_exist=False)
                if check["cwd"] != "." and not any(
                        n.startswith(check["cwd"] + "/") for n in self.git_tree(r["commit"])):
                    bad(cw + "/cwd", "исторический каталог отсутствует в проверенном коммите")
                if check["outcome"] not in ("passed", "failed"):
                    bad(cw + "/outcome", "нужны passed или failed")
                obligations = set()
                for co in sequence(check["covers"], cw + "/covers", True):
                    obj(co, cw + "/covers", ["work", "assignment_revision", "criterion"])
                    string(co["work"], cw + "/covers/work")
                    revision(co["assignment_revision"], cw + "/covers/assignment_revision")
                    if co["work"] not in self.works:
                        bad(cw + "/covers", "неизвестная работа")
                    _, ca = self.snapshot(r["commit"], co["work"])
                    equal(co["assignment_revision"], ca.get("revision"), cw + "/covers/assignment_revision")
                    if co["criterion"] not in [c["id"] for c in ca.get("criteria", [])]:
                        bad(cw + "/covers/criterion", "неизвестный критерий")
                    key = (co["work"], co["assignment_revision"], co["criterion"])
                    if key in obligations:
                        bad(cw + "/covers", "повтор обязательства")
                    obligations.add(key)
                if check["outcome"] == "passed":
                    covered |= obligations
                checks[check["id"]] = (check, obligations)
            strings(r["artifacts"], w + "/artifacts", True)
            for p in r["artifacts"]:
                self.local(p, w + "/artifacts", must_exist=False)
                if not any(contains(q, p) for q in a["inputs"]):
                    bad(w + "/artifacts", "артефакт не включён во входы поручения")
            self.input_tree(r["commit"], a["inputs"], w)
            tree = self.git_tree(r["commit"])
            if any(not any(contains(p, n) for n in tree) for p in r["artifacts"]):
                bad(w + "/artifacts", "артефакт отсутствует в проверенном коммите")
            self.results[r["id"]] = (r, task, a, checks, own, covered, w)

    def read_acceptances(self):
        for task, _, _, w in self.works.values():
            for i, ac in enumerate(sequence(task.get("acceptances", []), w + "/acceptances")):
                aw = f"{w}/acceptances/{i}"
                obj(ac, aw, ["id", "result", "result_revision", "assignment_revision", "by",
                             "evidence", "unresolved", "applicability"])
                self.ident(ac["id"], aw + "/id")
                string(ac["result"], aw + "/result")
                if ac["result"] not in self.results:
                    bad(aw + "/result", "отсутствует результат")
                r, rt, a, checks, own, _, _ = self.results[ac["result"]]
                equal(rt["id"], task["id"], aw + "/result")
                equal(ac["result_revision"], r["revision"], aw + "/result_revision")
                equal(ac["assignment_revision"], a["revision"], aw + "/assignment_revision")
                equal(ac["by"], a["acceptor"], aw + "/by")
                strings(ac["evidence"], aw + "/evidence", True)
                strings(ac["unresolved"], aw + "/unresolved")
                string(ac["applicability"], aw + "/applicability")
                covered = set()
                for cid in ac["evidence"]:
                    if cid not in checks or checks[cid][0]["outcome"] != "passed":
                        bad(aw + "/evidence", "нет успешной проверки результата")
                    covered |= checks[cid][1]
                if not own <= covered:
                    bad(aw + "/evidence", "приёмка не покрывает все обязательные критерии")
                self.acceptances[ac["id"]] = (ac, task, r, covered)
            current = task.get("current_acceptance")
            if current is not None:
                string(current, w + "/current_acceptance")
            if task["status"] == "accepted":
                if current not in self.acceptances:
                    bad(w + "/current_acceptance", "нет текущей приёмки")
                ac, _, r, _ = self.acceptances[current]
                equal(r["attempt_id"], task.get("active_attempt"), w + "/current_acceptance/attempt")
                equal(self.attempts[r["attempt_id"]][0]["phase"], "finished", w + "/current_acceptance/phase")
                equal(ac["assignment_revision"], task["_assignment"]["revision"], w + "/current_acceptance/revision")
                if canonical(self.results[r["id"]][2]) != canonical(task["_assignment"]):
                    bad(w + "/current_acceptance", "изменено содержание принятого поручения")
            elif current is not None:
                bad(w + "/current_acceptance", "история приёмок не является текущим принятым результатом")

    def dependencies(self):
        graph = {}
        for wid, (task, _, _, w) in self.works.items():
            graph[wid] = []
            for d in task.get("_assignment", {}).get("dependencies", []):
                if d["work"] not in self.works:
                    bad(w + "/dependencies", "неизвестная работа " + d["work"])
                graph[wid].append(d["work"])
                target = self.works[d["work"]][0]
                if d["criterion"] not in [c["id"] for c in target.get("_assignment", {}).get("criteria", [])]:
                    bad(w + "/dependencies/criterion", "неизвестный критерий зависимости")
                if d["acceptance"] is not None:
                    ac = self.acceptances.get(d["acceptance"])
                    if ac is None or ac[1]["id"] != d["work"]:
                        bad(w + "/dependencies/acceptance", "неизвестная приёмка зависимости")
            for d in task.get("_assignment", {}).get("decisions", []):
                if d["id"] not in self.decisions:
                    bad(w + "/decisions", "неизвестное общее решение")
        visited, active = set(), set()
        def visit(wid):
            if wid in active:
                bad("dependencies/" + wid, "цикл зависимостей")
            if wid in visited:
                return
            active.add(wid)
            for dependency in graph[wid]:
                visit(dependency)
            active.remove(wid)
            visited.add(wid)
        for wid in graph:
            visit(wid)

    def active_authority(self, task):
        _, s, name, w = self.works[task["id"]]
        a = task["_assignment"]
        self.file(a["authority"]["source"], w + "/authority/source")
        self.scoped_paths(a["inputs"], w + "/inputs")
        for d in a.get("decisions", []):
            if d["id"] not in self.decisions:
                bad(w + "/decisions", "неизвестное общее решение")
            equal(d["revision"], self.decisions[d["id"]]["revision"], w + "/decisions/revision")
            if not any(contains(p, self.decisions[d["id"]]["source"]) for p in a["inputs"]):
                bad(w + "/inputs", "источник общего решения не включён во входы")
        if s["scope"] == "feature":
            parent, _, _, pw = self.works[s["parent"]["work"]]
            pa = parent["_assignment"]
            equal(s["parent"]["assignment_revision"], pa["revision"], name + "/parent/assignment_revision")
            self.commit(s["parent"]["commit"], name + "/parent/commit")
            _, bound = self.snapshot(s["parent"]["commit"], parent["id"])
            if canonical(bound) != canonical(pa):
                bad(name + "/parent", "содержание общего поручения отличается от принятого")
            norms = [self.product["profile"], pa["authority"]["source"]]
            norms.extend(self.decisions[d["id"]]["source"] for d in pa.get("decisions", []))
            self.current_inputs(s["parent"]["commit"], list(dict.fromkeys(norms)), name + "/parent/norms")
            if parent["status"] not in ("ready", "in_progress"):
                bad(pw, "общее поручение функции не разрешает местное продолжение")
            self.active_authority(parent)
            if a["stage"] not in pa["mandate"]["stages"]:
                bad(w + "/stage", "этап не разрешён общим мандатом")
            if any(not any(contains(p, q) for p in pa["write_scope"]) for q in a["write_scope"]):
                bad(w + "/write_scope", "выход за общий отрезок функции")

    def coordination(self):
        for name, s in self.states.items():
            for task in s["works"]:
                w = self.works[task["id"]][3]
                if task["kind"] == "feature" and task["status"] != "candidate":
                    local = self.states[task["local_state"]]
                    equal(local["editor"]["role"], task["_assignment"]["mandate"]["role"], w + "/local/editor/role")
                    if "view" in task:
                        view = obj(task["view"], w + "/view", ["state", "revision", "summary"])
                        equal(view["state"], task["local_state"], w + "/view/state")
                        equal(view["revision"], local["revision"], w + "/view/revision")
                        string(view["summary"], w + "/view/summary")
                    if task["status"] == "accepted":
                        cl = obj(task.get("closure"), w + "/closure", ["knowledge", "obligations", "owner"])
                        for k in ("knowledge", "owner"):
                            string(cl[k], w + "/closure/" + k)
                        strings(cl["obligations"], w + "/closure/obligations")
                        if any(at[1]["id"] in {x["id"] for x in local["works"]}
                               and at[0]["phase"] in ("assigned", "running", "unknown")
                               for at in self.attempts.values()):
                            bad(w + "/closure", "местное исполнение ещё не прекращено")
                if task["status"] == "candidate":
                    continue
                for p in task["_assignment"]["inputs"]:
                    if any(contains(p, st) for st in self.states):
                        bad(w + "/inputs", "состояние проверяется по содержанию поручения отдельно от входов")
            for it in s["iterations"]:
                if it["status"] in ("integrated", "released"):
                    string(it.get("integration"), name + "/iterations/integration")
                    ac = self.acceptances.get(it.get("integration"))
                    if ac is None or ac[1]["kind"] != "integration" or ac[1]["iteration"] != it["id"]:
                        bad(name + "/iterations/integration", "нужна отдельная приёмка общей интеграции")
                    if self.works[ac[1]["id"]][1] is not s:
                        bad(name + "/iterations/integration", "местная приёмка не заменяет общую")
                    if it["status"] == "released":
                        string(it.get("release"), name + "/iterations/release")
            if "revision_review" in s:
                rr = obj(s["revision_review"], name + "/revision_review",
                         ["decision", "revision", "owner", "pending", "updated", "independent_actions"])
                string(rr["decision"], name + "/revision_review/decision")
                if rr["decision"] not in self.decisions:
                    bad(name + "/revision_review", "неизвестное решение")
                equal(rr["revision"], self.decisions[rr["decision"]]["revision"], name + "/revision_review/revision")
                equal(rr["owner"], s["editor"]["role"], name + "/revision_review/owner")
                for k in ("pending", "updated", "independent_actions"):
                    strings(rr[k], name + "/revision_review/" + k)
                stale, updated = set(), set()
                for wid, (task, _, _, _) in self.works.items():
                    if task["status"] == "accepted":
                        # Historical acceptances are not assignments to reissue.
                        continue
                    for d in task.get("_assignment", {}).get("decisions", []):
                        if d["id"] == rr["decision"]:
                            (updated if d["revision"] == rr["revision"] else stale).add(wid)
                equal(set(rr["pending"]), stale, name + "/revision_review/pending")
                equal(set(rr["updated"]), updated, name + "/revision_review/updated")
        # An assigned or unknown writer reserves the same shared resource as a running one.
        writers, git_owners = [], []
        for at, task, w in self.attempts.values():
            if at["phase"] in ("assigned", "running", "unknown"):
                equal(at["id"], task.get("active_attempt"), w + "/active_attempt")
                if task["status"] in ("submitted", "accepted"):
                    bad(w, "незавершённое исполнение не является представленным/принятым результатом")
                if "write" in at["effects"] or "git" in at["effects"]:
                    writers.append((at, task, w))
                if "git" in at["effects"]:
                    git_owners.append(at["id"])
            if at.get("external_outcome") == "unknown":
                for other in task.get("attempts", []):
                    if other["id"] != at["id"] and "external" in other["effects"] and other["phase"] in ("assigned", "running"):
                        bad(w, "неизвестный исход внешнего действия не разрешает повтор")
        if len(writers) > 1:
            bad("ownership", "несколько активных писателей/непрекращённых попыток")
        product = self.product
        owner = product.get("write_owner")
        git_owner = product.get("git_owner")
        if owner is not None:
            string(owner, "ownership/write_owner")
            if owner != product["id"]:
                scoped = self.works.get(owner)
                retained = any(self.works[t["id"]][1].get("parent", {}).get("work") == owner
                               for _, t, _ in writers)
                if (scoped is None or scoped[0]["kind"] != "feature" or
                        (scoped[0]["status"] not in ("ready", "in_progress") and not retained)):
                    bad("ownership/write_owner", "отрезок может принадлежать только продукту или активной функции")
        if git_owner is not None:
            equal(git_owner, owner, "ownership/git_owner")
        if writers:
            at, task, w = writers[0]
            _, s, _, _ = self.works[task["id"]]
            expected = s["parent"]["work"] if s["scope"] == "feature" else s["id"]
            equal(owner, expected, "ownership/write_owner")
            if git_owners:
                equal(git_owner, expected, "ownership/git_owner")
            if not task["_assignment"]["write_scope"]:
                bad(w, "пишущая попытка не имеет отрезка записи")
        # A function retains its delegated slot between local attempts. Its local
        # coordinator need not edit the product ledger to change the concrete writer.
        for name, s in self.states.items():
            if s is not product and ("write_owner" in s or "git_owner" in s):
                bad(name, "общий ресурс назначается только общей областью")

    def resources(self):
        for name, s in self.states.items():
            for i, limit in enumerate(sequence(s.get("real_limits", []), name + "/real_limits")):
                w = f"{name}/real_limits/{i}"
                if s is not self.product:
                    bad(w, "общие остатки хранятся только в общем источнике")
                obj(limit, w, ["id", "source", "scope", "unit", "cap", "remaining", "usage", "allocations"])
                self.ident(limit["id"], w + "/id")
                src = obj(limit["source"], w + "/source", ["by", "path"])
                string(src["by"], w + "/source/by")
                self.file(src["path"], w + "/source/path")
                string(limit["unit"], w + "/unit")
                strings(limit["scope"], w + "/scope", True)
                if not set(limit["scope"]) <= self.works.keys():
                    bad(w + "/scope", "неизвестная работа")
                cap = quantity(limit["cap"], w + "/cap")
                usage, known, seen = Fraction(0), True, set()
                for entry in sequence(limit["usage"], w + "/usage"):
                    obj(entry, w + "/usage", ["attempt", "amount"])
                    aid = entry["attempt"]
                    string(aid, w + "/usage/attempt")
                    if aid not in self.attempts or aid in seen:
                        bad(w + "/usage", "неизвестная или повторная расходная попытка")
                    seen.add(aid)
                    if not self.in_limit(self.attempts[aid][1]["id"], limit):
                        bad(w + "/usage", "попытка вне области ограничения")
                    if entry["amount"] is None:
                        known = False
                    else:
                        usage += quantity(entry["amount"], w + "/usage/amount")
                if usage > cap:
                    bad(w, "превышено действительное ограничение")
                if limit["remaining"] is not None:
                    remaining = quantity(limit["remaining"], w + "/remaining")
                    if not known:
                        bad(w + "/remaining", "неизвестный расход не равен нулю")
                    if remaining != cap - usage:
                        bad(w + "/remaining", "остаток не равен пределу минус первичный расход")
                allocated, allocations = Fraction(0), {}
                for entry in sequence(limit["allocations"], w + "/allocations"):
                    obj(entry, w + "/allocations", ["work", "amount"])
                    wid = entry["work"]
                    string(wid, w + "/allocations/work")
                    if wid not in self.works or not self.in_limit(wid, limit) or wid in allocations:
                        bad(w + "/allocations", "неизвестное/повторное назначение остатка")
                    amount = quantity(entry["amount"], w + "/allocations/amount")
                    allocated += amount
                    allocations[wid] = amount
                    task, scope, _, _ = self.works[wid]
                    parent = (self.works[scope["parent"]["work"]][0]
                              if scope["scope"] == "feature" else task)
                    if parent["kind"] == "feature" and parent["status"] == "accepted" and amount:
                        bad(w + "/allocations", "завершённая функция должна освободить назначение")
                if limit["remaining"] is not None and allocated > remaining:
                    bad(w + "/allocations", "повторно зарезервирован или превышен общий остаток")
                self.limits[limit["id"]] = (limit, allocations, w)
            for view in sequence(s.get("usage_views", []), name + "/usage_views"):
                obj(view, name + "/usage_views", ["limit", "attempts"])
                string(view["limit"], name + "/usage_views/limit")
                strings(view["attempts"], name + "/usage_views/attempts")
        for name, s in self.states.items():
            for view in s.get("usage_views", []):
                if view["limit"] not in self.limits:
                    bad(name + "/usage_views", "неизвестное ограничение")
                leaf = {x["attempt"] for x in self.limits[view["limit"]][0]["usage"]}
                if not set(view["attempts"]) <= leaf:
                    bad(name + "/usage_views", "сводка ссылается не на первичные расходы")

    def in_limit(self, wid, limit):
        s = self.works[wid][1]
        return wid in limit["scope"] or (s["scope"] == "feature" and s["parent"]["work"] in limit["scope"])

    def git_tree(self, ref):
        if ref not in self.trees:
            self.trees[ref] = self.tree(ref)
        return self.trees[ref]

    def input_tree(self, ref, scopes, w):
        tree = self.git_tree(ref)
        result = {}
        strings(scopes, w + "/inputs", True)
        for p in scopes:
            self.local(p, w + "/inputs", must_exist=False)
            if p == ".":
                bad(w + "/inputs", "не разрешён весь корень")
            matches = {n: v for n, v in tree.items() if contains(p, n)}
            if not matches:
                bad(w + "/inputs", "исходник/область отсутствует в проверенном коммите: " + p)
            result.update(matches)
        for n, (mode, kind, _) in result.items():
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                unsupported(w + "/inputs/" + n, "ссылки, подмодули и необычные объекты Git не поддерживаются")
        return result

    def fresh(self, rid, obligation=None, compatibility=None):
        r, task, a, _, _, _, w = self.results[rid]
        scopes = a["inputs"]
        old = self.input_tree(r["commit"], scopes, w)
        target = r["commit"]
        if compatibility is not None:
            c = obj(compatibility, w + "/compatibility",
                    ["from_commit", "to_commit", "work", "assignment_revision", "criterion",
                     "differences", "reason", "by"])
            revision(c["assignment_revision"], w + "/compatibility/assignment_revision")
            equal(c["from_commit"], r["commit"], w + "/compatibility/from_commit")
            self.commit(c["to_commit"], w + "/compatibility/to_commit")
            if obligation is None:
                bad(w, "совместимость требует конкретного обязательства")
            equal((c["work"], c["assignment_revision"], c["criterion"]), obligation, w + "/compatibility/obligation")
            strings(c["differences"], w + "/compatibility/differences", True)
            for k in ("reason", "by"):
                string(c[k], w + "/compatibility/" + k)
            target = c["to_commit"]
            new = self.input_tree(target, scopes, w)
            changed = {n for n in old.keys() | new.keys() if old.get(n) != new.get(n)}
            equal(set(c["differences"]), changed, w + "/compatibility/differences")
            if not changed:
                bad(w + "/compatibility", "нет различий, совместимость не требуется")
        self.current_inputs(target, scopes, w)

    def current_inputs(self, target, scopes, w):
        tested = self.input_tree(target, scopes, w)
        head = self.input_tree("HEAD", scopes, w)
        if tested != head:
            bad(w + "/inputs", "входы в HEAD отличаются от выбранного подтверждения")
        index = {}
        for entry in self.git("ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                n = os.fsdecode(name)
                if any(contains(p, n) for p in scopes):
                    index.setdefault(n, []).append(tuple(metadata.split()))
        expected = {n: [(m, oid, b"0")] for n, (m, _, oid) in tested.items()}
        if index != expected:
            bad(w + "/inputs", "индекс Git отличается от выбранного подтверждения")
        # Git chooses nonignored untracked names; no filters, diff drivers or shell.
        untracked = self.git("-c", "core.excludesFile=/dev/null", "ls-files", "--others",
                             "--exclude-standard", "-z").split(b"\0")
        for raw in untracked:
            if raw and any(contains(p, os.fsdecode(raw)) for p in scopes):
                bad(w + "/inputs", "новый неигнорируемый вход вне проверенного коммита: " + os.fsdecode(raw))
        for n, (mode, _, oid) in tested.items():
            path = self.file(n, w + "/inputs/" + n)
            if path.read_bytes() != self.git("cat-file", "blob", oid.decode("ascii")):
                bad(w + "/inputs/" + n, "рабочий вход отличается от выбранного подтверждения")
            if bool(path.stat().st_mode & 0o111) != (mode == b"100755"):
                bad(w + "/inputs/" + n, "изменились права исполнения")

    def current_assignment(self, wid, expected, w, selected_history=False):
        def compare(saved, label):
            # A separately accepted historical confirmer can have a new own
            # assignment. Each current version still claiming the selected
            # identity/revision must retain its exact content.
            if selected_history and (saved["id"], saved["revision"]) != (expected["id"], expected["revision"]):
                return
            if canonical(saved) != canonical(expected):
                bad(w + "/assignment", "содержание поручения отличается: " + label)

        compare(self.works[wid][0]["_assignment"], "рабочие данные")
        for ref in ("HEAD", ""):
            # An empty ref is the index's :path syntax. Compare the normative
            # assignment only, never progress, results or the complete ledger.
            _, saved = self.snapshot(ref, wid)
            compare(saved, ref or "индекс")

    def candidate(self, task, acceptance=False):
        _, _, _, w = self.works[task["id"]]
        aid = task.get("active_attempt")
        if aid not in self.attempts or self.attempts[aid][1] is not task:
            bad(w + "/active_attempt", "нет активной попытки этой работы")
        at = self.attempts[aid][0]
        if "result" not in at:
            bad(w + "/result", "нет представленного результата")
        results = [v for v in self.results.values() if v[0]["attempt_id"] == aid]
        if len(results) != 1:
            bad(w + "/result", "нет единственного результата попытки")
        r, _, a, _, own, covered, _ = results[0]
        allowed_effects(a, at, w)
        equal(at["phase"], "finished", w + "/phase")
        equal(r["assignment_revision"], task["_assignment"]["revision"], w + "/assignment_revision")
        if canonical(a) != canonical(task["_assignment"]):
            bad(w + "/assignment", "содержание поручения изменилось при прежней редакции")
        if not own <= covered:
            bad(w + "/checks", "не все обязательные критерии успешно проверены")
        for check, _ in results[0][3].values():
            if not self.local(check["cwd"], w + "/checks/cwd").is_dir():
                bad(w + "/checks/cwd", "для текущего кандидата нужен существующий каталог")
        self.current_assignment(task["id"], a, w)
        self.fresh(r["id"])
        if acceptance:
            if task.get("current_acceptance") not in self.acceptances:
                bad(w + "/current_acceptance", "нет текущей приёмки для передачи")

    def bases(self, task, bases, w, allow_candidate=False):
        deps = task["_assignment"].get("dependencies", [])
        sequence(bases, w)
        selected = set()
        expected = {(d["work"], d["acceptance"], d["criterion"]) for d in deps}
        for i, b in enumerate(bases):
            bw = f"{w}/{i}"
            obj(b, bw, ["work", "acceptance", "criterion", "result", "result_revision", "check"],
                ["replacements", "compatibility"])
            for k in ("work", "acceptance", "criterion", "result", "check"):
                string(b[k], bw + "/" + k)
            revision(b["result_revision"], bw + "/result_revision")
            key = (b["work"], b["acceptance"], b["criterion"])
            if key not in expected or key in selected:
                bad(bw, "основание не соответствует зависимости или повторяется")
            selected.add(key)
            ac = self.acceptances.get(b["acceptance"])
            if ac is None or ac[1]["id"] != b["work"]:
                bad(bw + "/acceptance", "нет требуемой принятой редакции зависимости")
            dep_task = self.works[b["work"]][0]
            if dep_task["status"] != "accepted" or dep_task.get("current_acceptance") != b["acceptance"]:
                bad(bw + "/acceptance", "отменённая/непринятая/заменённая работа не даёт разрешение")
            obligation = (b["work"], ac[0]["assignment_revision"], b["criterion"])
            if obligation not in ac[3]:
                bad(bw + "/criterion", "приёмка не подтверждает нужное обязательство")
            evidence = self.results.get(b["result"])
            if evidence is None:
                bad(bw + "/result", "нет источника результата")
            r, _, _, checks, _, _, _ = evidence
            allowed_effects(evidence[2], self.attempts[r["attempt_id"]][0], bw)
            accepted_assignment = self.results[ac[2]["id"]][2]
            _, confirmed_assignment = self.snapshot(r["commit"], b["work"])
            if canonical(confirmed_assignment) != canonical(accepted_assignment):
                bad(bw + "/check", "содержание подтверждаемого обязательства отличается от принятого поручения")
            self.current_assignment(b["work"], accepted_assignment, bw)
            self.current_assignment(evidence[1]["id"], evidence[2], bw, selected_history=True)
            equal(b["result_revision"], r["revision"], bw + "/result_revision")
            cid = b["check"]
            if cid not in checks or checks[cid][0]["outcome"] != "passed" or obligation not in checks[cid][1]:
                bad(bw + "/check", "проверка не покрывает нужное обязательство")
            accepted = [a for a in self.acceptances.values()
                        if a[2]["id"] == r["id"] and a[0]["result_revision"] == r["revision"]
                        and cid in a[0]["evidence"]]
            own_candidate = (allow_candidate and evidence[1] is task and
                             r["attempt_id"] == task.get("active_attempt"))
            if not accepted and not own_candidate:
                bad(bw + "/result", "текущее подтверждение ещё не принято")
            if r["id"] != ac[2]["id"]:
                replacements = sequence(b.get("replacements"), bw + "/replacements", True)
                first = replacements[0]
                for old in replacements:
                    obj(old, bw + "/replacements", ["result", "revision", "check", "reason", "by"])
                    string(old["result"], bw + "/replacements/result")
                    string(old["check"], bw + "/replacements/check")
                    if old["result"] not in self.results:
                        bad(bw + "/replacements", "утрачен прежний источник")
                    prior = self.results[old["result"]]
                    equal(old["revision"], prior[0]["revision"], bw + "/replacements/revision")
                    if old["check"] not in prior[3] or obligation not in prior[3][old["check"]][1]:
                        bad(bw + "/replacements/check", "прежнее основание не связано с обязательством")
                    for k in ("reason", "by"):
                        string(old[k], bw + "/replacements/" + k)
                equal(first["result"], ac[2]["id"], bw + "/replacements/first")
            self.fresh(r["id"], obligation, b.get("compatibility"))
        if selected != expected:
            bad(w, "пропущено обязательное основание текущего действия")

    def actions(self):
        required = {"ready": "assign", "in_progress": "execute", "submitted": "review"}
        for name, s in self.states.items():
            seen, spending = set(), {}
            for i, action in enumerate(sequence(s.get("actions", []), name + "/actions")):
                w = f"{name}/actions/{i}"
                obj(action, w, ["work", "kind"], ["bases", "limit", "amount"])
                wid, kind = action["work"], action["kind"]
                string(wid, w + "/work")
                string(kind, w + "/kind")
                if wid not in self.works or self.works[wid][1] is not s:
                    bad(w + "/work", "действие относится к неизвестной/чужой области")
                task = self.works[wid][0]
                if kind not in ("inspect", "assign", "execute", "review", "handoff", "spend"):
                    unsupported(w + "/kind", "неизвестное действие")
                if (wid, kind) in seen:
                    bad(w, "повтор действия")
                seen.add((wid, kind))
                if kind == "inspect":
                    if set(action) != {"work", "kind"}:
                        bad(w, "чтение не является расходом или разрешением продолжения")
                    self.infos.append(wid + ": только чтение/разбор; реализация этим действием не разрешена")
                    continue
                if task["status"] in ("candidate", "blocked", "paused", "cancelled", "revise"):
                    bad(w, "состояние работы допускает только inspect")
                self.active_authority(task)
                self.bases(task, action.get("bases"), w + "/bases", kind == "review")
                if kind in ("assign", "execute"):
                    equal(task["status"], "ready" if kind == "assign" else "in_progress", w + "/status")
                    aid = task.get("active_attempt")
                    if aid not in self.attempts or self.attempts[aid][1] is not task:
                        bad(w + "/active_attempt", "чужая/отсутствующая активная попытка")
                    at = self.attempts[aid][0]
                    equal(at["assignment_revision"], task["_assignment"]["revision"], w + "/assignment_revision")
                    equal(at["phase"], "assigned" if kind == "assign" else "running", w + "/phase")
                    allowed_effects(task["_assignment"], at, w)
                elif kind in ("review", "handoff"):
                    equal(task["status"], "submitted" if kind == "review" else "accepted", w + "/status")
                    self.candidate(task, kind == "handoff")
                else:
                    string(action.get("limit"), w + "/limit")
                    if action.get("limit") not in self.limits:
                        bad(w + "/limit", "нет действительного ограничения с источником разрешения")
                    limit, allocations, lw = self.limits[action["limit"]]
                    amount = quantity(action.get("amount"), w + "/amount")
                    if limit["remaining"] is None:
                        bad(w + "/amount", "обязательный остаток неизвестен; расход не разрешён")
                    if wid not in allocations:
                        bad(w + "/amount", "нет распределения остатка для этой работы")
                    spending[(action["limit"], wid)] = spending.get((action["limit"], wid), Fraction(0)) + amount
                    if spending[(action["limit"], wid)] > allocations[wid]:
                        bad(w + "/amount", "расход превышает выделенный остаток")
                if kind != "spend" and ("limit" in action or "amount" in action):
                    bad(w, "расход описывается отдельным действием spend")
            for task in s["works"]:
                if task["status"] in required:
                    if (task["id"], required[task["status"]]) not in seen:
                        bad(name + "/actions", "нет обязательного действия для " + task["id"])
