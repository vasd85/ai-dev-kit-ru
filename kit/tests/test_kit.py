"""Behavior tests and reproducible synthetic walkthrough; no third-party packages."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
TOOL = KIT / "tools/kit.py"
STATE = "docs/ai-work/state.json"
sys.path.insert(0, str(KIT / "tools"))
spec = importlib.util.spec_from_file_location("kit_tool", TOOL)
kit_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit_tool)


# Keep the package-1 fixtures independent of later public template revisions.
P1_STATE = {'format_version': '0.1-p1',
 'scope': 'single-task',
 'profile': 'docs/ai-work/project.md',
 'coordinator': 'local-agent',
 'iteration': {'id': 'iteration-1', 'goal': 'Нормализовать пробелы в одной строке'},
 'task': {'id': 'task-1',
          'status': 'ready',
          'assignment': {'id': 'assignment-1',
                         'revision': 1,
                         'goal': 'Убрать крайние пробелы и свести внутренние пробельные группы к '
                                 'одному пробелу',
                         'boundaries': 'Изменить только функцию normalize; без новых зависимостей '
                                       'и выпуска',
                         'inputs': ['docs/ai-work/project.md', 'source.py', 'test_source.py'],
                         'write_files': ['source.py'],
                         'criteria': [{'id': 'criterion-spaces',
                                       'expectation': "normalize('  a   b  ') == 'a b'; пустая "
                                                      'строка остаётся пустой'}],
                         'plan': ['Прочитать функцию и тест',
                                  'Исправить нормализацию',
                                  'Сохранить входы в Git',
                                  'Проверить коммит и представить результат'],
                         'executor': 'local-agent',
                         'acceptor': 'local-agent',
                         'authorized_action': 'implement-and-test',
                         'authority': {'by': 'user', 'source': 'docs/ai-work/project.md'}},
          'attempt': {'id': 'attempt-1',
                      'assignment_revision': 1,
                      'executor': 'local-agent',
                      'phase': 'assigned'}},
 'continuation': {'owner': 'local-agent',
                  'last_completed': 'Поручение подготовлено, прежних исполнителей нет',
                  'unverified': ['Исправление ещё не выполнено'],
                  'processes': 'none',
                  'writer': None,
                  'next_action': 'execute',
                  'next_step': 'Исправить normalize по поручению, затем сохранить и проверить '
                               'входы'}}

P1_RESULT = {'id': 'result-1',
 'revision': 1,
 'assignment_id': 'assignment-1',
 'assignment_revision': 1,
 'attempt_id': 'attempt-1',
 'executor': 'local-agent',
 'summary': 'Заполнить фактически сделанным изменением',
 'commit': 'Заменить полным идентификатором уже проверенного коммита',
 'artifacts': ['source.py', 'test_source.py'],
 'checks': [{'id': 'check-1',
             'criteria': ['criterion-spaces'],
             'command': 'python3 -B -m unittest -v test_source',
             'cwd': '.',
             'expected': '2 теста проходят',
             'observed': 'Заменить фактическим результатом запуска; форма сама не доказывает успех',
             'outcome': 'passed',
             'environment': 'Заполнить фактическими версиями Python и ОС'}],
 'unverified': [],
 'limitations': ['Проверен только заданный сценарий функции'],
 'next_step': 'Координатору сопоставить результат с поручением и записать приёмку'}


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE="1")
    return env


def fingerprint(root):
    """Include Git metadata and symlink targets, without following symlinks."""
    entries = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink():
                entries[str(path.relative_to(root))] = ("link", os.readlink(path))
            elif path.is_file():
                entries[str(path.relative_to(root))] = (
                    path.stat().st_mode, path.stat().st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest())
    return entries


class Fixture:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / STATE
        self.state_path.parent.mkdir(parents=True)
        self.data = copy.deepcopy(P1_STATE)
        (self.root / "docs/ai-work/project.md").write_text(
            "# Учебные данные\n\nПотребность: нормализация пробелов. "
            "Пользователь разрешил подготовленный тест комплекта; это не продукт.\n"
            "Одна локальная ветка Git, без сервера, публикации и платных средств.\n"
            "Один агент совмещает координатора, исполнителя и принимающего.\n"
            "Проверка: python3 -B -m unittest -v test_source из корня.\n"
            "Новые расходы и выпуск не разрешены. Полнота трёх входов установлена "
            "для этой двухстрочной функции и её теста.\n", encoding="utf-8")
        (self.root / "source.py").write_text("def normalize(value):\n    return value.strip()\n")
        (self.root / "test_source.py").write_text(
            "import unittest\nfrom source import normalize\n\n"
            "class NormalizeTest(unittest.TestCase):\n"
            "    def test_groups(self):\n"
            "        self.assertEqual(normalize('  a   b  '), 'a b')\n"
            "    def test_empty(self):\n"
            "        self.assertEqual(normalize(''), '')\n")
        self.save()
        self.git("init", "--quiet", "--template=", "--initial-branch=main")
        self.commit("Исходное поручение и пример")

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "-c", "user.name=Kit Test", "-c", "user.email=kit-test@example.invalid", *args],
            cwd=self.root, env=environment(), capture_output=True, check=True)
        return result.stdout.decode().strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def save(self):
        self.state_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")

    def check(self):
        return subprocess.run([sys.executable, "-B", str(TOOL), "check", "--root", str(self.root)],
                              env=environment(), capture_output=True, text=True)

    def progress(self):
        task, continuation = self.data["task"], self.data["continuation"]
        task["status"] = "in_progress"
        task["attempt"]["phase"] = "running"
        continuation.update(last_completed="Исходник изменён; окончательный тест ещё не запущен",
                            unverified=["Изменённый source.py ещё не проверен"],
                            processes="running", writer="local-agent")
        (self.root / "source.py").write_text("def normalize(value):\n    return ' '.join(value.split())\n")
        self.save()

    def stop_before_test(self):
        # A real, bounded writer exits before the coordinator saves the handoff.
        self.data["task"]["status"] = "in_progress"
        self.data["task"]["attempt"]["phase"] = "running"
        self.save()
        command = [sys.executable, "-B", "-c",
                   "from pathlib import Path; "
                   "Path('source.py').write_text(\"def normalize(value):\\n    return ' '.join(value.split())\\n\")"]
        process = subprocess.Popen(command, cwd=self.root, env=environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        if process.returncode != 0:
            raise AssertionError(repr((stdout, stderr, process.returncode)))
        task = self.data["task"]
        task["status"] = "paused"
        task["attempt"]["phase"] = "stopped"
        self.data["continuation"].update(
            owner=self.data["coordinator"],
            last_completed=(f"Исполнитель {task['attempt']['executor']}, попытка {task['attempt']['id']}: "
                            f"отдельный процесс PID {process.pid} изменил только source.py "
                            "на split/join и завершился с кодом 0; родитель дождался завершения. "
                            "Дочерних команд не запускал, тест не выполнял. "
                            "Изменение осталось в рабочем дереве после исходного коммита."),
            unverified=["Изменённый source.py не проверен: test_source ещё не запускался; "
                        "результат и приёмка отсутствуют"],
            processes="none", writer=None, next_action="inspect",
            next_step="Ответственный координатор читает поручение, source.py, test_source.py и Git, "
                      "сверяет сохранённую правку и подтверждённое завершение прежнего процесса. "
                      "Разрешены чтение и разбор; новый писатель и запуск теста этой передачей не назначены. "
                      "Для исполнения требуется отдельное назначение и поддержка возобновления в пакете 2.")
        self.save()

    def submit(self):
        self.progress()
        self.checked_commit = self.commit("Исправление и поручение перед проверкой")
        command = [sys.executable, "-B", "-m", "unittest", "-v", "test_source"]
        run = subprocess.run(command, cwd=self.root, env=environment(), capture_output=True, text=True)
        if run.returncode:
            raise AssertionError(run.stdout + run.stderr)
        self.test_output = run.stdout + run.stderr
        result = copy.deepcopy(P1_RESULT)
        result.update(commit=self.checked_commit, summary="Внутренние пробелы нормализуются через split/join")
        result["checks"][0].update(command=" ".join(command), observed=self.test_output.strip(),
                                  environment=f"Python {platform.python_version()}, {platform.system()} {platform.release()}")
        task = self.data["task"]
        task.update(status="submitted", result=result)
        task["attempt"]["phase"] = "finished"
        self.data["continuation"].update(last_completed="Коммит проверен: два теста прошли; результат представлен",
                                         unverified=[], processes="none", writer=None,
                                         next_action="review", next_step="Сопоставить результат с поручением и принять")
        self.save()

    def accept(self):
        self.data["task"].update(status="accepted", acceptance={
            "id": "acceptance-1", "result_id": "result-1", "result_revision": 1,
            "assignment_revision": 1, "by": "local-agent", "decision": "accepted",
            "evidence": ["check-1"], "unresolved": [],
            "applicability": "Только записанное поручение и неизменённые три входа проверенного коммита"})
        self.data["continuation"].update(last_completed="Результат сопоставлен с критерием и принят",
                                         next_action="handoff", next_step="Передать учебный результат; новую задачу не начинать")
        self.save()


class KitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-p1-test-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(Path(self.tmp.name) / "repo")

    def assertCheck(self, code=0, text=""):
        run = self.fixture.check()
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        self.assertIn(text, run.stdout)
        self.assertNotIn("Traceback", run.stderr)
        return run

    def accepted(self):
        self.fixture.submit()
        self.fixture.accept()

    def test_complete_path_and_read_only(self):
        for action, status in ((None, "ready"), (self.fixture.progress, "in_progress"),
                               (self.fixture.submit, "submitted"), (self.fixture.accept, "accepted")):
            if action:
                action()
            before = fingerprint(self.fixture.root)
            self.assertCheck(0, status)
            self.assertEqual(before, fingerprint(self.fixture.root))
        self.assertIn("Ran 2 tests", self.fixture.test_output)
        self.assertIn("OK", self.fixture.test_output)

    def test_report_only_commit_does_not_invalidate(self):
        self.accepted()
        checked = self.fixture.checked_commit
        report = self.fixture.commit("Только результат и приёмка")
        self.assertNotEqual(checked, report)
        self.assertCheck()

    def test_missing_metrics_and_exceeded_forecast(self):
        self.accepted()
        self.assertCheck()
        self.fixture.data["forecast"] = {"estimate": 1, "observed": 9}
        self.fixture.save()
        self.assertCheck(0, "Прогноз превышен")
        self.fixture.data["forecast"] = {"estimate": 10**400, "observed": None}
        self.fixture.save()
        self.assertCheck()

    def test_malformed_and_duplicate_json(self):
        for raw, message in (("{", "нечитаемый JSON"),
                             ('{"scope":1,"scope":2}', "повтор ключа"),
                             ('{"forecast": NaN}', "недопустимое число")):
            with self.subTest(raw=raw):
                self.fixture.state_path.write_text(raw)
                self.assertCheck(1, message)

    def test_missing_state(self):
        self.fixture.state_path.unlink()
        self.assertCheck(1, "отсутствует источник")

    def test_duplicate_identifiers(self):
        self.fixture.data["task"]["id"] = "iteration-1"
        self.fixture.save()
        self.assertCheck(1, "повтор идентификатора")

    def test_unknown_version_and_constructs(self):
        original = copy.deepcopy(self.fixture.data)
        for field, value in (("format_version", "0.2"), ("scope", "feature"),
                             ("dependencies", ["task-0"]), ("real_limits", [{"remaining": None}]),
                             ("features", []), ("history", [])):
            with self.subTest(field=field):
                self.fixture.data = copy.deepcopy(original)
                self.fixture.data[field] = value
                self.fixture.save()
                self.assertCheck(2, "UNSUPPORTED")

    def test_unknown_nested_field(self):
        self.fixture.data["task"]["assignment"]["compatibility"] = "совместимо"
        self.fixture.save()
        self.assertCheck(2, "неизвестные поля")

    def test_missing_profile_and_required_field(self):
        (self.fixture.root / "docs/ai-work/project.md").unlink()
        self.assertCheck(1, "отсутствует источник")
        self.fixture.data.pop("continuation")
        self.fixture.save()
        self.assertCheck(1, "continuation")

    def test_missing_repository(self):
        (self.fixture.root / ".git").rename(self.fixture.root / "saved-git")
        self.assertCheck(1, "отсутствует локальный Git")

    def test_nonexistent_commit(self):
        self.fixture.submit()
        self.fixture.data["task"]["result"]["commit"] = "0" * 40
        self.fixture.save()
        self.assertCheck(1, "cat-file")

    def test_wrong_attempt_or_revision(self):
        self.accepted()
        original = copy.deepcopy(self.fixture.data)
        changes = [("result", "attempt_id", "attempt-other"),
                   ("result", "assignment_revision", 2), ("result", "assignment_id", "assignment-other"),
                   ("acceptance", "result_revision", 2), ("acceptance", "by", "stranger"),
                   ("attempt", "executor", "stranger")]
        for record, field, value in changes:
            with self.subTest(record=record, field=field):
                self.fixture.data = copy.deepcopy(original)
                self.fixture.data["task"][record][field] = value
                self.fixture.save()
                self.assertCheck(1, field)

    def test_revision_links_require_integer(self):
        self.accepted()
        original = copy.deepcopy(self.fixture.data)
        for record, field in (("attempt", "assignment_revision"), ("result", "assignment_revision"),
                              ("acceptance", "assignment_revision"), ("acceptance", "result_revision")):
            for value in (True, 1.0):
                with self.subTest(record=record, field=field, value=value):
                    self.fixture.data = copy.deepcopy(original)
                    self.fixture.data["task"][record][field] = value
                    self.fixture.save()
                    self.assertCheck(1, field)

    def test_status_cannot_replace_result_or_acceptance(self):
        self.accepted()
        original = copy.deepcopy(self.fixture.data)
        for record in ("result", "acceptance"):
            with self.subTest(record=record):
                self.fixture.data = copy.deepcopy(original)
                self.fixture.data["task"].pop(record)
                self.fixture.save()
                self.assertCheck(1, record)

    def test_required_checks_and_acceptance_evidence(self):
        self.accepted()
        original = copy.deepcopy(self.fixture.data)
        for kind in ("absent", "failed", "criterion", "acceptance"):
            with self.subTest(kind=kind):
                self.fixture.data = copy.deepcopy(original)
                task = self.fixture.data["task"]
                if kind == "absent":
                    task["result"]["checks"] = []
                elif kind == "failed":
                    task["result"]["checks"][0]["outcome"] = "failed"
                elif kind == "criterion":
                    task["result"]["checks"][0]["criteria"] = ["missing"]
                else:
                    task["acceptance"]["evidence"] = ["missing"]
                self.fixture.save()
                self.assertCheck(1)

    def test_required_criterion_cannot_be_silently_omitted(self):
        self.fixture.data["task"]["assignment"]["criteria"].append(
            {"id": "criterion-second", "expectation": "Второе обязательное поведение"})
        self.fixture.save()
        self.fixture.submit()
        self.assertCheck(1, "не все обязательные критерии")

    def test_acceptance_evidence_must_cover_every_criterion(self):
        self.fixture.data["task"]["assignment"]["criteria"].append(
            {"id": "criterion-second", "expectation": "Второе обязательное поведение"})
        self.fixture.save()
        self.fixture.submit()
        second = copy.deepcopy(self.fixture.data["task"]["result"]["checks"][0])
        second.update(id="check-second", criteria=["criterion-second"])
        self.fixture.data["task"]["result"]["checks"].append(second)
        self.fixture.accept()
        self.assertCheck(1, "приёмка не покрывает все критерии")

    def test_changed_assignment_without_new_revision(self):
        self.fixture.submit()
        self.fixture.data["task"]["assignment"]["goal"] += " и ещё одно требование"
        self.fixture.save()
        self.assertCheck(1, "содержание поручения")

    def test_replaced_attempt_not_in_checked_commit(self):
        self.fixture.submit()
        self.fixture.data["task"]["attempt"]["id"] = "attempt-new"
        self.fixture.data["task"]["result"]["attempt_id"] = "attempt-new"
        self.fixture.save()
        self.assertCheck(1, "назначение попытки")

    def hidden_assignment(self, commit):
        self.fixture.submit()
        tested = copy.deepcopy(self.fixture.data)
        self.fixture.data["task"]["assignment"]["goal"] = "Изменённое скрытое требование"
        self.fixture.save()
        if commit:
            self.fixture.commit("Изменённое поручение")
        else:
            self.fixture.git("add", "--", STATE)
        self.fixture.data = tested
        self.fixture.save()
        self.assertCheck(1, "HEAD" if commit else "индекс")

    def test_assignment_in_head_not_hidden_by_working_state(self):
        self.hidden_assignment(commit=True)

    def test_assignment_in_index_not_hidden_by_working_state(self):
        self.hidden_assignment(commit=False)

    def test_unsaved_changed_input(self):
        self.accepted()
        (self.fixture.root / "source.py").write_text("changed\n")
        before = fingerprint(self.fixture.root)
        self.assertCheck(1, "текущий вход отличается")
        self.assertEqual(before, fingerprint(self.fixture.root))

    def test_committed_changed_input(self):
        self.accepted()
        (self.fixture.root / "test_source.py").write_text("changed\n")
        self.fixture.commit("Изменённый тест после проверки")
        self.assertCheck(1, "текущий вход отличается")

    def test_staged_change_hidden_by_restored_working_file(self):
        self.fixture.submit()
        source = self.fixture.root / "source.py"
        tested = source.read_bytes()
        source.write_text("changed and staged\n")
        self.fixture.git("add", "--", "source.py")
        source.write_bytes(tested)
        before = fingerprint(self.fixture.root)
        self.assertCheck(1, "индекс")
        self.assertEqual(before, fingerprint(self.fixture.root))

    def test_changed_head_hidden_by_restored_working_file(self):
        self.fixture.submit()
        source = self.fixture.root / "source.py"
        tested = source.read_bytes()
        source.write_text("changed and committed\n")
        self.fixture.commit("Изменённый вход при восстановленной рабочей копии")
        source.write_bytes(tested)
        self.assertCheck(1, "HEAD")

    def test_deleted_input(self):
        self.accepted()
        (self.fixture.root / "source.py").unlink()
        self.assertCheck(1, "отсутствует источник")

    def test_new_declared_source_outside_checked_commit(self):
        self.fixture.progress()
        # Save a declared input in the committed assignment, but omit the file from Git.
        (self.fixture.root / "new.py").write_text("new source\n")
        self.fixture.data["task"]["assignment"]["inputs"].append("new.py")
        self.fixture.save()
        self.fixture.git("add", "--", STATE, "source.py")
        checked = self.fixture.git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Без нового входа")
        del checked
        self.fixture.submit()
        # submit normally adds all; point at preceding commit with exactly the same assignment.
        self.fixture.data["task"]["result"]["commit"] = self.fixture.git("rev-parse", "HEAD^")
        self.fixture.save()
        self.assertCheck(1, "исходник отсутствует в проверенном коммите")

    def test_artifact_must_be_registered(self):
        self.fixture.submit()
        (self.fixture.root / "extra.py").write_text("extra\n")
        self.fixture.data["task"]["result"]["artifacts"].append("extra.py")
        self.fixture.save()
        self.assertCheck(1, "не включён в зарегистрированные входы")

    def test_directories_unsupported(self):
        self.fixture.data["task"]["assignment"]["inputs"].append("docs")
        self.fixture.save()
        self.assertCheck(2, "без каталогов")

    def test_executable_mode_is_an_input(self):
        self.fixture.submit()
        path = self.fixture.root / "source.py"
        path.chmod(path.stat().st_mode | 0o100)
        self.assertCheck(1, "права исполнения")

    def test_paths_outside_root_not_read(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text("PRIVATE_SENTINEL_DO_NOT_READ")
        for name in ("../outside.json", str(outside), "escape.json"):
            with self.subTest(name=name):
                link = self.fixture.root / "escape.json"
                if not link.exists():
                    link.symlink_to(outside)
                original_read = Path.read_bytes
                def guarded_read(path):
                    self.assertNotEqual(path.resolve(), outside.resolve(), "прочитан чужой файл")
                    return original_read(path)
                checker = kit_tool.Checker(self.fixture.root, name)
                with patch.object(Path, "read_bytes", guarded_read):
                    with self.assertRaises(kit_tool.Problem):
                        checker.check()
                run = subprocess.run([sys.executable, "-B", str(TOOL), "check", "--root",
                                      str(self.fixture.root), "--state", name], capture_output=True, text=True)
                self.assertEqual(run.returncode, 1)
                self.assertNotIn("PRIVATE_SENTINEL", run.stdout + run.stderr)

    def test_input_symlink_and_git_symlink(self):
        self.fixture.submit()
        source = self.fixture.root / "source.py"
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text("private\n")
        source.unlink()
        source.symlink_to(outside)
        self.assertCheck(1, "символические ссылки")
        source.unlink()
        source.write_text("def normalize(value):\n    return ' '.join(value.split())\n")
        meta = self.fixture.root / ".git"
        meta.rename(Path(self.tmp.name) / "git-outside")
        meta.symlink_to(Path(self.tmp.name) / "git-outside", target_is_directory=True)
        self.assertCheck(1, "символическая ссылка")

    def test_git_config_include_rejected_before_git(self):
        config = self.fixture.root / ".git/config"
        with config.open("a") as stream:
            stream.write('\n[include]\n\tpath = /outside/private-config\n')
        checker = kit_tool.Checker(self.fixture.root, STATE)
        with patch.object(checker, "git", side_effect=AssertionError("Git репозитория не должен запускаться")):
            with self.assertRaises(kit_tool.Problem) as caught:
                checker.check()
            self.assertEqual(caught.exception.code, 2)
        self.assertCheck(2, "include")

    def test_git_config_harmless_values_comments_and_syntax(self):
        self.accepted()
        config = self.fixture.root / ".git/config"
        original = config.read_bytes()
        additions = [
            b'\n[remote "origin"]\nurl = https://example.invalid/team/extensions.git\n',
            b'\n[user]\nname = Worktree Reviewer\n# includes extensions partialclone\n',
            b'\n ; includeIf worktree\n[USER] # extensions\n Name = "Include; # Worktree" ; include\n',
            b'\n[remote "include.extensions.worktree"]\nurl = "https://example.invalid/include"\n',
            b'\n[user]\nname = harmless\\\n[include]\n',
            b'\n[user]\nname = "quoted \\" include"\n',
            b'\n[user]\nname = caf\xe9 extensions\n',
        ]
        for addition in additions:
            with self.subTest(addition=addition):
                config.write_bytes(original + addition)
                before = fingerprint(self.fixture.root)
                self.assertCheck()
                self.assertEqual(before, fingerprint(self.fixture.root))

    def test_git_config_real_constructs_rejected_without_repository_setup(self):
        config = self.fixture.root / ".git/config"
        original = config.read_bytes()
        outside = Path(self.tmp.name) / "private-config"
        outside.write_text("INVALID PRIVATE CONFIG; must not be parsed\n")
        additions = [
            f'\n[InClUdE]\n PaTh = "{outside}" # comment\n',
            f'\n[includeIf "gitdir:{self.fixture.root}/"]\npath = {outside}\n',
            '\n# comment\\\n[include]\npath = /outside\n',
            '\n[CoRe]\n WoRkTrEe = /outside\n',
            '\n[extensions]\nworktreeConfig = true\n',
            '\n[extensions]\npartialClone = origin\n',
            '\n[remote "origin"]\npromisor = true\n',
            '\n[remote "origin"]\npartialCloneFilter = blob:none\n',
        ]
        for addition in additions:
            with self.subTest(addition=addition):
                config.write_bytes(original + addition.encode())
                checker = kit_tool.Checker(self.fixture.root, STATE)
                with patch.object(checker, "git", side_effect=AssertionError("Небезопасный setup")):
                    with self.assertRaises(kit_tool.Problem) as caught:
                        checker.check()
                    self.assertEqual(caught.exception.code, 2)
                before = fingerprint(self.fixture.root)
                self.assertCheck(2, ".git/config")
                self.assertEqual(before, fingerprint(self.fixture.root))
                self.assertEqual(outside.read_text(), "INVALID PRIVATE CONFIG; must not be parsed\n")

    def test_git_config_malformed_stops_before_repository_commands(self):
        config = self.fixture.root / ".git/config"
        original = config.read_bytes()
        for addition in (b'\n[user\nname=x\n', b'\n[user]\nname="unterminated\n',
                         b'\n[user]\nname=bad\\q\n'):
            with self.subTest(addition=addition):
                config.write_bytes(original + addition)
                checker = kit_tool.Checker(self.fixture.root, STATE)
                with patch.object(checker, "git", side_effect=AssertionError("Небезопасный setup")):
                    with self.assertRaises(kit_tool.Problem) as caught:
                        checker.check()
                    self.assertEqual(caught.exception.code, 1)
                before = fingerprint(self.fixture.root)
                self.assertCheck(1, "неверная конфигурация")
                self.assertEqual(before, fingerprint(self.fixture.root))

    def test_git_environment_and_external_storage_guards(self):
        self.fixture.submit()
        outside = Path(self.tmp.name) / "external-config"
        outside.write_text("invalid outside config\n")
        injected = dict(GIT_CONFIG_GLOBAL=str(outside), GIT_CONFIG_SYSTEM=str(outside),
                        GIT_CONFIG=str(outside), GIT_DIR=str(outside),
                        GIT_OBJECT_DIRECTORY=str(outside), GIT_ALTERNATE_OBJECT_DIRECTORIES=str(outside),
                        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="include.path", GIT_CONFIG_VALUE_0=str(outside),
                        GIT_TRACE=str(self.fixture.root / "TRACE_WAS_WRITTEN"))
        before = fingerprint(self.fixture.root)
        # fixture.check resets its own environment, so invoke the actual CLI here.
        env = environment()
        env.update(injected)
        run = subprocess.run([sys.executable, "-B", str(TOOL), "check", "--root", str(self.fixture.root)],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(before, fingerprint(self.fixture.root))
        for name in ("commondir", "objects/info/alternates", "info/grafts"):
            with self.subTest(name=name):
                path = self.fixture.root / ".git" / name
                path.parent.mkdir(exist_ok=True, parents=True)
                path.write_text(str(outside) + "\n")
                before = fingerprint(self.fixture.root)
                self.assertCheck(2, "хранилище Git")
                self.assertEqual(before, fingerprint(self.fixture.root))
                path.unlink()

    def test_unicode_cli_strict_utf8_outputs(self):
        original = copy.deepcopy(self.fixture.data)
        cases = [("unknown-surrogate", 2, "UNSUPPORTED: state: неизвестные поля: \\ud800"),
                 ("duplicate-surrogate", 1, "повтор ключа JSON: \\ud800"),
                 ("ordinary-error", 1, "нет обязательных полей: continuation"),
                 ("unicode-field", 2, "неизвестные поля: дополнительное"),
                 ("valid-unicode", 0, "OK:")]
        for name, code, message in cases:
            with self.subTest(name=name):
                self.fixture.data = copy.deepcopy(original)
                if name == "unknown-surrogate":
                    self.fixture.data["\ud800"] = "unknown"
                elif name == "ordinary-error":
                    self.fixture.data.pop("continuation")
                elif name == "unicode-field":
                    self.fixture.data["дополнительное"] = True
                self.fixture.state_path.write_text(json.dumps(self.fixture.data, ensure_ascii=True))
                if name == "duplicate-surrogate":
                    self.fixture.state_path.write_text('{"\\ud800":1,"\\ud800":2}')
                env = environment()
                env["PYTHONIOENCODING"] = "utf-8:strict"
                before = fingerprint(self.fixture.root)
                run = subprocess.run([sys.executable, "-B", str(TOOL), "check", "--root", str(self.fixture.root)],
                                     env=env, capture_output=True)
                stdout, stderr = run.stdout.decode("utf-8"), run.stderr.decode("utf-8")
                self.assertEqual(run.returncode, code, stdout + stderr)
                self.assertIn(message, stdout)
                self.assertIn("Это не проверка качества продукта", stdout)
                self.assertNotIn("Traceback", stdout + stderr)
                self.assertEqual(before, fingerprint(self.fixture.root))

    def test_external_git_handlers_not_executed(self):
        self.fixture.submit()
        marker = self.fixture.root / "HANDLER_WAS_EXECUTED"
        config = self.fixture.root / ".git/config"
        with config.open("a") as stream:
            stream.write('\n[diff]\n\texternal = touch HANDLER_WAS_EXECUTED\n'
                         '[diff "test"]\n\ttextconv = touch HANDLER_WAS_EXECUTED\n'
                         '[core]\n\tfsmonitor = touch HANDLER_WAS_EXECUTED\n'
                         '\tsshCommand = touch HANDLER_WAS_EXECUTED\n'
                         '[filter "test"]\n\tprocess = touch HANDLER_WAS_EXECUTED\n'
                         '[credential]\n\thelper = !touch HANDLER_WAS_EXECUTED\n')
        (self.fixture.root / ".gitattributes").write_text("*.py diff=test\n")
        for malformed in (False, True):
            if malformed:
                with config.open("a") as stream:
                    stream.write('\n[broken\n')
            before = fingerprint(self.fixture.root)
            self.assertCheck(1 if malformed else 0)
            self.assertEqual(before, fingerprint(self.fixture.root))
            self.assertFalse(marker.exists())

    def test_git_metadata_links_and_gitfile_rejected_before_config(self):
        meta = self.fixture.root / ".git"
        outside = Path(self.tmp.name) / "outside-config"
        outside.write_text("PRIVATE OUTSIDE DATA\n")
        link = meta / "config.worktree"
        link.symlink_to(outside)
        before = fingerprint(self.fixture.root)
        self.assertCheck(1, "ссылка в служебных данных")
        self.assertEqual(before, fingerprint(self.fixture.root))
        link.unlink()
        meta.rename(self.fixture.root / "saved-git")
        meta.write_text("gitdir: " + str(outside) + "\n")
        before = fingerprint(self.fixture.root)
        self.assertCheck(2, "gitfile")
        self.assertEqual(before, fingerprint(self.fixture.root))

    def test_unsupported_diagnostic_is_read_only(self):
        self.fixture.data["real_limits"] = [{"required_remaining": "unknown"}]
        self.fixture.save()
        before = fingerprint(self.fixture.root)
        self.assertCheck(2, "real_limits")
        self.assertEqual(before, fingerprint(self.fixture.root))

    def test_unknown_process_does_not_release_writer(self):
        self.fixture.data["continuation"]["processes"] = "unknown"
        self.fixture.save()
        self.assertCheck(1, "processes")

    def test_recorded_command_is_not_executed(self):
        self.fixture.submit()
        self.fixture.data["task"]["result"]["checks"][0]["command"] = "touch COMMAND_WAS_EXECUTED"
        self.fixture.save()
        self.assertCheck()
        self.assertFalse((self.fixture.root / "COMMAND_WAS_EXECUTED").exists())

    def test_continuation_can_be_read_without_chat(self):
        self.accepted()
        saved = json.loads(self.fixture.state_path.read_text())
        self.assertEqual(saved["task"]["status"], "accepted")
        self.assertEqual(saved["continuation"]["processes"], "none")
        self.assertIsNone(saved["continuation"]["writer"])
        self.assertEqual(saved["continuation"]["owner"], saved["coordinator"])
        self.assertEqual(saved["continuation"]["unverified"], [])
        self.assertIn("новую задачу не начинать", saved["continuation"]["next_step"])
        self.assertTrue(saved["task"]["result"]["limitations"])

    def test_stopped_handoff_before_test_is_honest_and_unsupported(self):
        original_source = self.fixture.git("show", "HEAD:source.py")
        self.fixture.stop_before_test()
        # Read the saved files, rather than relying on the live Fixture.data.
        saved = json.loads(self.fixture.state_path.read_bytes())
        task, continuation = saved["task"], saved["continuation"]
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["attempt"], {"id": "attempt-1", "assignment_revision": 1,
                                         "executor": "local-agent", "phase": "stopped"})
        self.assertNotIn("result", task)
        self.assertNotIn("acceptance", task)
        self.assertFalse(hasattr(self.fixture, "test_output"))
        source = (self.fixture.root / "source.py").read_text()
        self.assertIn("' '.join(value.split())", source)
        self.assertNotEqual(source.strip(), original_source)
        self.assertEqual(continuation["processes"], "none")
        self.assertIsNone(continuation["writer"])
        self.assertEqual(continuation["owner"], saved["coordinator"])
        self.assertIn("завершился с кодом 0", continuation["last_completed"])
        self.assertIn("тест не выполнял", continuation["last_completed"])
        self.assertIn("source.py не проверен", continuation["unverified"][0])
        self.assertEqual(continuation["next_action"], "inspect")
        self.assertIn("Разрешены чтение и разбор", continuation["next_step"])
        self.assertIn("запуск теста этой передачей не назначены", continuation["next_step"])
        self.assertIn("пакете 2", continuation["next_step"])
        before = fingerprint(self.fixture.root)
        run = self.assertCheck(2, "UNSUPPORTED: task/status")
        self.assertNotIn("OK:", run.stdout)
        self.assertEqual(before, fingerprint(self.fixture.root))


def example(destination):
    root = Path(destination).absolute()
    if root.exists():
        raise SystemExit("Для примера нужен новый, ещё не существующий каталог")
    fixture = Fixture(root)
    for action in (None, fixture.progress, fixture.submit, fixture.accept):
        if action:
            action()
        run = fixture.check()
        print(run.stdout, end="")
        if run.returncode:
            raise SystemExit(run.returncode)
    fixture.commit("Отчёт и приёмка; проверенные входы не изменены")
    final = fixture.check()
    print(final.stdout, end="")
    print("Фактический тест сохранённого коммита:\n" + fixture.test_output)
    print("Состояние: " + str(fixture.state_path))
    print("Проверенный коммит: " + fixture.checked_commit)
    print("Пример сохранён. Он не применяется к реальному продукту.")
    return final.returncode


def stopped_example(destination):
    root = Path(destination).absolute()
    if root.exists():
        raise SystemExit("Для примера нужен новый, ещё не существующий каталог")
    fixture = Fixture(root)
    fixture.stop_before_test()
    run = fixture.check()
    print(run.stdout, end="")
    print("Состояние: " + str(fixture.state_path))
    print("Исходник изменён, тест не выполнялся, прежний процесс завершён.")
    print("Ожидаемый код диагностики — 2; разрешены только чтение и разбор передачи.")
    return run.returncode


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--example":
        sys.exit(example(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == "--stopped-example":
        sys.exit(stopped_example(sys.argv[2]))
    unittest.main()
