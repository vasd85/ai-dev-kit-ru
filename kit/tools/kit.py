#!/usr/bin/env python3
"""Kit CLI. Only explicit move, migration prepare and migration switch write."""

import argparse
import json
import os
import re
import sys

# A diagnostic run from a project's installed copy must not populate that copy.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

from versions import CURRENT_FORMAT, LEGACY_FORMAT, STATE_FORMATS, TASK_FORMAT, record_format

SCOPE = (f"Охват: {TASK_FORMAT} — карточки задач; {CURRENT_FORMAT} — несколько работ и текущие действия; "
         f"{LEGACY_FORMAT} — прежний ограниченный путь. Прежние обозначения форматов поддерживаются.")
LIMIT = "Это не проверка качества продукта и не готовность полного комплекта."


from read_support import (
    BaseChecker, Problem, bad, unsupported, emit, obj, string, strings,
    revision, equal, parse, number, canonical, json_record,
)
from records import LEGACY_ASSIGNMENT_FIELDS, RESULT_FIELDS, RecordsChecker


class Checker(BaseChecker):
    def basis(self, state, where):
        try:
            version = record_format(state["format_version"])
            if version != LEGACY_FORMAT:
                unsupported(where + "/format_version", "ожидается " + LEGACY_FORMAT)
            return (version, state["scope"], state["profile"], state["coordinator"],
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
        if record_format(s["format_version"]) != LEGACY_FORMAT:
            unsupported("state/format_version", f"ожидается {LEGACY_FORMAT} или его прежнее обозначение")
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
        a = obj(task["assignment"], "assignment", LEGACY_ASSIGNMENT_FIELDS)
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
        r = obj(task["result"], "result", RESULT_FIELDS)
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


def select_checker(args):
    state = args.state if args.state is not None else "docs/ai-work/state.json"
    if args.profile is not None:
        from task_reader import profile_selection
        base = BaseChecker(args.root, None)
        profile = base.file(args.profile, "profile")
        selection = profile_selection(profile.read_text(encoding="utf-8"), args.profile)
        expected, state = selection["format_version"], selection["path"]
        if expected == TASK_FORMAT:
            from task_actions import TaskActions
            return TaskActions(args.root, state), selection
        return state_checker(args.root, state, expected), selection
    return state_checker(args.root, state), None


def state_checker(root, state, expected=None):
    base = BaseChecker(root, state)
    initial = parse(base.file(state, "state").read_bytes(), state)
    version = record_format(initial.get("format_version")) if isinstance(initial, dict) else None
    if isinstance(initial, dict) and "format_version" in initial and version not in (
            LEGACY_FORMAT, CURRENT_FORMAT):
        unsupported("state/format_version", "для файла состояния поддерживаются только " + STATE_FORMATS)
    if expected is not None and version != expected:
        bad("state/format_version", f"источник {state} не соответствует формату {expected} из профиля")
    if version == CURRENT_FORMAT:
        return RecordsChecker(root, state)
    return Checker(root, state)


def show_tasks(args):
    from task_reader import TaskReader
    reader = TaskReader.from_profile(args.root, args.profile)
    data = reader.show(args.number) if args.number is not None else reader.list_tasks()
    emit(json.dumps(data, ensure_ascii=False, indent=2))
    for info in reader.infos:
        print("INFO: " + info, file=sys.stderr)


def show_material(args):
    from task_reader import TaskReader, read_history
    address = {"path": args.path}
    if args.task is not None:
        if args.profile is None or args.select is not None:
            bad("read", "для --task нужен --profile; выбор записи относится только к --commit")
        address["task"] = args.task
        material = TaskReader.from_profile(args.root, args.profile).read_material(address)
    else:
        if args.profile is not None:
            bad("read", "--profile относится к текущему адресу --task")
        address["commit"] = args.commit
        if args.select is not None:
            address["select"] = parse(args.select, "--select")
        material = read_history(BaseChecker(args.root, None), address)
    if isinstance(material, bytes):
        sys.stdout.buffer.write(material)
    else:
        # Legacy payloads may contain exact decimal values. Keep their lexical
        # value in JSON without converting to binary floats.
        sys.stdout.write(json_record(material) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=SCOPE + " " + LIMIT)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="только чтение состояния и Git")
    check.add_argument("--root", required=True, help="допустимый корень, он же корень Git")
    source = check.add_mutually_exclusive_group()
    source.add_argument("--state", help="явный путь прежнего файла состояния от корня")
    source.add_argument("--profile", help="путь заполненного профиля от корня")
    check.add_argument("--action", choices=("assign", "execute", "review", "handoff"),
                       help="сверить заявленный переход по карточке")
    check.add_argument("--task", help="полный номер задачи для --action")
    check.add_argument("--actor", help="сессия редактора или исполнитель для --action")
    check.add_argument("--attempt", help="ключ попытки, если действие относится к запуску")
    tasks = sub.add_parser("tasks", help="перечислить карточки или открыть задачу по полному номеру")
    tasks.add_argument("--root", required=True)
    tasks.add_argument("--profile", required=True)
    tasks.add_argument("number", nargs="?", help="полный номер задачи, например P/T-008")
    move = sub.add_parser("move", help="сверить размещение или перенести папку по действующему решению")
    move.add_argument("--root", required=True)
    move.add_argument("--profile", required=True)
    move.add_argument("--task", required=True, help="полный номер задачи")
    move.add_argument("--actor", help="сессия редактора области для переноса")
    move.add_argument("--review", help="JSON осмотра с владельцами и ссылками; запускает перенос")
    overview = sub.add_parser("overview", help="открыть локальный обзор задач в браузере")
    overview.add_argument("--root", default=".", help="корень проекта, по умолчанию текущий каталог")
    overview.add_argument("--profile", required=True, help="путь заполненного профиля от корня")
    read = sub.add_parser("read", help="открыть текущий материал задачи или байты из Git")
    read.add_argument("--root", required=True)
    read.add_argument("--profile", help="путь заполненного профиля для --task")
    read.add_argument("--path", required=True, help="путь в папке задачи или в дереве коммита")
    read.add_argument("--select", help="JSON-ключ записи внутри прежнего файла в Git")
    address = read.add_mutually_exclusive_group(required=True)
    address.add_argument("--task", help="полный номер живой задачи")
    address.add_argument("--commit", help="полный идентификатор Git-коммита")
    migration = sub.add_parser("migration", help="подготовить, сверить и выбрать карточки")
    phases = migration.add_subparsers(dest="phase", required=True)
    for phase in ("inventory", "prepare", "verify", "switch"):
        command = phases.add_parser(phase)
        command.add_argument("--root", required=True,
                             help="проверяемый корень; для switch — установка, которую переключают")
        command.add_argument("--review", required=True, help="JSON сопоставления, составленный редактором")
        if phase != "inventory":
            command.add_argument("--origin", required=True, help="отдельный корень старой установки")
            command.add_argument("--snapshot", required=True, help="снимок команды inventory")
            command.add_argument("--target", required=True, help="выделенная папка от корня копии")
            command.add_argument("--bindings", help="JSON адресов снимков карточек в истории копии")
        if phase == "prepare":
            command.add_argument("--previous-bindings", help="JSON адресов предыдущей проверенной сборки")
        if phase == "switch":
            command.add_argument("--actor", required=True, help="сессия общего редактора")
            command.add_argument("--pause", required=True, help="путь записи остановки от корня")
    args = parser.parse_args(argv)
    try:
        if args.command == "migration":
            from task_migration import run
            return run(args, state_checker)
        if args.command == "tasks":
            show_tasks(args)
            return 0
        if args.command == "move":
            from task_moves import TaskMove
            if bool(args.actor) != bool(args.review):
                bad("move", "для переноса нужны одновременно --actor и --review")
            move = TaskMove.from_profile(args.root, args.profile)
            result = (move.apply_move(args.profile, args.task, args.actor, args.review)
                      if args.review else move.inspect_move(args.task))
            emit(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "overview":
            from overview import serve
            serve(args.root, args.profile)
            return 0
        if args.command == "read":
            show_material(args)
            return 0
        emit(SCOPE)
        if args.action is None and any(v is not None for v in (args.task, args.actor, args.attempt)):
            bad("check", "--task, --actor и --attempt относятся к --action")
        if args.action is not None and (args.profile is None or not args.task or not args.actor):
            bad("check", "для --action нужны --profile, --task и --actor")
        checker, selection = select_checker(args)
        if selection is not None:
            emit(f"Источник из {args.profile}: {selection['path']} ({selection['format_version']}).")
            if path := selection.get("pause"):
                from task_actions import transition_status
                pause, editor, step = transition_status(checker, args.profile, selection, path)
                notice = {"paused": "Выдача приостановлена",
                          "reviewed": "Выдача проверенных работ возобновлена",
                          "stopped": "Переход остановлен"}[pause["state"]]
                emit(f"{notice} ({path}). Следующий шаг — {step}")
                if editor:
                    emit(f"Координатор общей выдачи — {editor['role']} ({editor['session']}).")
        if args.action is None:
            status = checker.check()
            if selection and selection["format_version"] == TASK_FORMAT:
                for continuation in checker.continuations():
                    emit("Продолжение: " + continuation)
        else:
            from task_actions import TaskActions
            if not isinstance(checker, TaskActions):
                unsupported("check", "--action применяется к карточкам " + TASK_FORMAT)
            status = checker.action(args.profile, args.task, args.action, args.actor, args.attempt)
        for info in checker.infos:
            emit("INFO: " + info)
        emit(f"OK: формальных противоречий не найдено; состояние {status}.")
        emit(LIMIT)
        return 0
    except Problem as error:
        prefix = "UNSUPPORTED" if error.code == 2 else "ERROR"
        if args.command == "check":
            emit(f"{prefix}: {error.where}: {error.message}")
            emit(LIMIT)
        else:
            print(f"{prefix}: {error.where}: {error.message}", file=sys.stderr)
        return error.code
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        message = f"ERROR: чтение: {type(error).__name__}; проверьте доступность и формат файлов."
        if args.command == "check":
            emit(message)
            emit(LIMIT)
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
