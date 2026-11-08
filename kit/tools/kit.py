#!/usr/bin/env python3
"""Read-only CLI for p1/p2; Checker retains the limited p1 contract."""

import argparse
import os
import re
import sys

VERSION = "0.1-p1"
SCOPE = "Охват: 0.1-p2 — несколько работ и текущие действия; 0.1-p1 — прежний ограниченный путь."
LIMIT = "Это не проверка качества продукта и не готовность полного комплекта."


from read_support import (
    BaseChecker, Problem, bad, unsupported, emit, git_environment, obj, string,
    strings, revision, equal, parse, number, canonical,
)


class Checker(BaseChecker):
    def basis(self, state, where):
        try:
            return (state["format_version"], state["scope"], state["profile"], state["coordinator"],
                    state["iteration"], state["task"]["id"], state["task"]["assignment"],
                    {k: state["task"]["attempt"][k]
                     for k in ("id", "executor", "assignment_revision")})
        except (KeyError, TypeError):
            bad(where, "нет исходного поручения и назначения попытки")

    def check(self):
        state_path = self.file(self.state_name, "state")
        if state_path == self.root:
            bad("state", "ожидается файл состояния")
        s = parse(state_path.read_bytes(), self.state_name)
        obj(s, "state", ["format_version", "scope", "profile", "coordinator", "iteration",
                         "task", "continuation"], ["forecast", "dependencies", "real_limits"])
        if s["format_version"] != VERSION:
            unsupported("state/format_version", f"поддерживается только {VERSION}")
        if s["scope"] != "single-task":
            unsupported("state/scope", "поддерживается только single-task")
        for key in ("dependencies", "real_limits"):
            if key in s and s[key] != []:
                unsupported("state/" + key, "эта конструкция требует пакета 2")
        self.file(s["profile"], "state/profile")
        string(s["coordinator"], "state/coordinator")
        it = obj(s["iteration"], "iteration", ["id", "goal"])
        self.ident(it["id"], "iteration/id")
        string(it["goal"], "iteration/goal")
        task = obj(s["task"], "task", ["id", "status", "assignment", "attempt"],
                   ["result", "acceptance"])
        self.ident(task["id"], "task/id")
        status = task["status"]
        if status not in ("ready", "in_progress", "submitted", "accepted"):
            unsupported("task/status", "пакет 1: ready, in_progress, submitted, accepted")
        a = obj(task["assignment"], "assignment",
                ["id", "revision", "goal", "boundaries", "inputs", "write_files", "criteria",
                 "plan", "executor", "acceptor", "authorized_action", "authority"])
        self.ident(a["id"], "assignment/id")
        revision(a["revision"], "assignment/revision")
        for key in ("goal", "boundaries", "executor", "acceptor"):
            string(a[key], "assignment/" + key)
        equal(a["acceptor"], s["coordinator"], "assignment/acceptor")
        if a["authorized_action"] != "implement-and-test":
            unsupported("assignment/authorized_action", "поддерживается implement-and-test")
        authority = obj(a["authority"], "assignment/authority", ["by", "source"])
        string(authority["by"], "assignment/authority/by")
        self.file(authority["source"], "assignment/authority/source")
        self.paths(a["inputs"], "assignment/inputs")
        self.paths(a["write_files"], "assignment/write_files")
        for path in [s["profile"], authority["source"], *a["write_files"]]:
            if path not in a["inputs"]:
                bad("assignment/inputs", f"обязательный вход не зарегистрирован: {path}")
        if self.state_name in a["inputs"]:
            bad("assignment/inputs", "состояние проверяется отдельно по содержанию поручения")
        strings(a["plan"], "assignment/plan", nonempty=True)
        criteria = a["criteria"]
        if not isinstance(criteria, list) or not criteria:
            bad("assignment/criteria", "нужен непустой список критериев")
        criterion_ids = set()
        for i, criterion in enumerate(criteria):
            where = f"assignment/criteria/{i}"
            obj(criterion, where, ["id", "expectation"])
            self.ident(criterion["id"], where + "/id")
            string(criterion["expectation"], where + "/expectation")
            criterion_ids.add(criterion["id"])
        attempt = obj(task["attempt"], "attempt", ["id", "assignment_revision", "executor", "phase"])
        self.ident(attempt["id"], "attempt/id")
        equal(attempt["assignment_revision"], a["revision"], "attempt/assignment_revision")
        equal(attempt["executor"], a["executor"], "attempt/executor")
        phase = {"ready": "assigned", "in_progress": "running",
                 "submitted": "finished", "accepted": "finished"}[status]
        equal(attempt["phase"], phase, "attempt/phase")
        continuation = obj(s["continuation"], "continuation",
                           ["owner", "last_completed", "unverified", "processes", "writer",
                            "next_action", "next_step"])
        for key in ("owner", "last_completed", "next_step"):
            string(continuation[key], "continuation/" + key)
        strings(continuation["unverified"], "continuation/unverified")
        running = status == "in_progress"
        equal(continuation["processes"], "running" if running else "none", "continuation/processes")
        equal(continuation["writer"], a["executor"] if running else None, "continuation/writer")
        equal(continuation["owner"], a["executor"] if status in ("ready", "in_progress")
              else a["acceptor"], "continuation/owner")
        equal(continuation["next_action"], {"ready": "execute", "in_progress": "execute",
              "submitted": "review", "accepted": "handoff"}[status], "continuation/next_action")
        if "forecast" in s:
            forecast = obj(s["forecast"], "forecast", ["estimate"], ["observed"])
            for key, value in forecast.items():
                if value is None and key == "observed":
                    continue
                number(value, "forecast/" + key)
            observed = forecast.get("observed")
            if observed is not None and observed > forecast["estimate"]:
                self.infos.append("Прогноз превышен: координатору нужен разбор прогресса; это не отказ.")
        self.repository()
        if status in ("ready", "in_progress"):
            if "result" in task or "acceptance" in task:
                bad("task", "результат/приёмка не соответствуют незавершённой попытке")
        else:
            self.result(s, criterion_ids)
        return status

    def result(self, s, criterion_ids):
        task, a = s["task"], s["task"]["assignment"]
        if "result" not in task:
            bad("task/result", "нет обязательного результата представленной попытки")
        r = obj(task["result"], "result",
                ["id", "revision", "assignment_id", "assignment_revision", "attempt_id", "executor",
                 "summary", "commit", "artifacts", "checks", "unverified", "limitations", "next_step"])
        self.ident(r["id"], "result/id")
        revision(r["revision"], "result/revision")
        for key, expected in (("assignment_id", a["id"]), ("assignment_revision", a["revision"]),
                              ("attempt_id", task["attempt"]["id"]), ("executor", a["executor"])):
            equal(r[key], expected, "result/" + key)
        for key in ("summary", "next_step"):
            string(r[key], "result/" + key)
        for key in ("unverified", "limitations"):
            strings(r[key], "result/" + key)
        self.paths(r["artifacts"], "result/artifacts")
        for path in r["artifacts"]:
            if path not in a["inputs"]:
                bad("result/artifacts", f"исходник не включён в зарегистрированные входы: {path}")
        checks = r["checks"]
        if not isinstance(checks, list) or not checks:
            bad("result/checks", "нет обязательного свидетельства проверки")
        coverage, check_map = set(), {}
        for i, c in enumerate(checks):
            where = f"result/checks/{i}"
            obj(c, where, ["id", "criteria", "command", "cwd", "expected", "observed",
                           "outcome", "environment"])
            self.ident(c["id"], where + "/id")
            strings(c["criteria"], where + "/criteria", nonempty=True)
            for key in ("command", "expected", "observed", "environment"):
                string(c[key], where + "/" + key)
            cwd = self.local(c["cwd"], where + "/cwd")
            if not cwd.is_dir():
                bad(where + "/cwd", "рабочая папка проверки не является каталогом")
            if not set(c["criteria"]) <= criterion_ids:
                bad(where + "/criteria", "ссылка на неизвестный критерий")
            equal(c["outcome"], "passed", where + "/outcome")
            coverage.update(c["criteria"])
            check_map[c["id"]] = c
        if coverage != criterion_ids:
            bad("result/checks", "не все обязательные критерии имеют успешное свидетельство")
        commit = r["commit"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            bad("result/commit", "нужен полный идентификатор Git-коммита")
        equal(self.git("cat-file", "-t", commit).strip(), b"commit", "result/commit")
        current = canonical(self.basis(s, "state"))
        for label, prefix in (("проверенный коммит", commit + ":"), ("HEAD", "HEAD:"), ("индекс", ":")):
            saved = parse(self.git("cat-file", "blob", prefix + self.state_name),
                          "Git:" + label + ":" + self.state_name)
            if canonical(self.basis(saved, "Git:assignment")) != current:
                bad("Git:assignment", "содержание поручения или назначение попытки отличается: " + label)
        tree = self.tree(commit)
        head = self.tree("HEAD")
        index = {}
        for entry in self.git("ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                index.setdefault(os.fsdecode(name), []).append(tuple(metadata.split()))
        for i, name in enumerate(a["inputs"]):
            where = f"Git:inputs/{i} ({name})"
            if name not in tree:
                bad(where, "заявленный исходник отсутствует в проверенном коммите")
            mode, kind, oid = tree[name]
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                unsupported(where, "ссылки, подмодули и необычные Git-объекты не поддерживаются")
            path = self.file(name, where)
            if path.read_bytes() != self.git("cat-file", "blob", oid.decode("ascii")):
                bad(where, "текущий вход отличается от проверенного коммита")
            if bool(path.stat().st_mode & 0o111) != (mode == b"100755"):
                bad(where, "изменились права исполнения входа")
            if head.get(name) != tree[name]:
                bad(where, "вход в HEAD отличается от проверенного коммита")
            if index.get(name) != [(mode, oid, b"0")]:
                bad(where, "индекс Git содержит другую версию входа или конфликт")
        if task["status"] == "submitted":
            if "acceptance" in task:
                bad("task/acceptance", "представленный кандидат ещё не принят")
            return
        if "acceptance" not in task:
            bad("task/acceptance", "статус accepted не заменяет запись приёмки")
        accept = obj(task["acceptance"], "acceptance",
                     ["id", "result_id", "result_revision", "assignment_revision", "by", "decision",
                      "evidence", "unresolved", "applicability"])
        self.ident(accept["id"], "acceptance/id")
        for key, expected in (("result_id", r["id"]), ("result_revision", r["revision"]),
                              ("assignment_revision", a["revision"]), ("by", a["acceptor"]),
                              ("decision", "accepted")):
            equal(accept[key], expected, "acceptance/" + key)
        strings(accept["evidence"], "acceptance/evidence", nonempty=True)
        strings(accept["unresolved"], "acceptance/unresolved")
        string(accept["applicability"], "acceptance/applicability")
        accepted_criteria = set()
        for evidence in accept["evidence"]:
            if evidence not in check_map:
                bad("acceptance/evidence", "неизвестное свидетельство: " + evidence)
            accepted_criteria.update(check_map[evidence]["criteria"])
        if accepted_criteria != criterion_ids:
            bad("acceptance/evidence", "приёмка не покрывает все критерии")


def main(argv=None):
    parser = argparse.ArgumentParser(description=SCOPE + " " + LIMIT)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="только чтение состояния и Git")
    check.add_argument("--root", required=True, help="допустимый корень, он же корень Git")
    check.add_argument("--state", default="docs/ai-work/state.json", help="путь относительно корня")
    args = parser.parse_args(argv)
    emit(SCOPE)
    try:
        checker = Checker(args.root, args.state)
        initial = parse(checker.file(args.state, "state").read_bytes(), args.state)
        if isinstance(initial, dict) and "format_version" in initial and initial["format_version"] not in ("0.1-p1", "0.1-p2"):
            unsupported("state/format_version", "поддерживаются только 0.1-p1 и 0.1-p2")
        if isinstance(initial, dict) and initial.get("format_version") == "0.1-p2":
            from records import RecordsChecker
            checker = RecordsChecker(args.root, args.state)
        status = checker.check()
        for info in checker.infos:
            emit("INFO: " + info)
        emit(f"OK: формальных противоречий не найдено; состояние {status}.")
        emit(LIMIT)
        return 0
    except Problem as error:
        prefix = "UNSUPPORTED" if error.code == 2 else "ERROR"
        emit(f"{prefix}: {error.where}: {error.message}")
        emit(LIMIT)
        return error.code
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        emit(f"ERROR: чтение: {type(error).__name__}; проверьте доступность и формат файлов.")
        emit(LIMIT)
        return 1


if __name__ == "__main__":
    sys.exit(main())
