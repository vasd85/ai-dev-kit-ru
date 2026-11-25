"""Exercise placement and recovery from the root of an installed project."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_kit import environment, fingerprint
from test_task_actions import (
    F_SCOPE, LOCAL, NORM, PROFILE, P_SCOPE, PRODUCT, SOURCE, Project, accept, address, decide, observation,
)


NUMBER = "P/T-001"
ACTOR = "product-chat"
REVIEW = "move-review.json"
OLD = SOURCE + "/open/T-001"
DONE = SOURCE + "/done/T-001"
CLOSED = SOURCE + "/closed/T-001"


def move_args(number=NUMBER, actor=None):
    args = ["--profile", PROFILE, "--task", number]
    return [*args, "--actor", actor, "--review", REVIEW] if actor else args


def read_review(project, number=NUMBER, owner=PRODUCT, links=()):
    result = project.run_cli("move", *move_args(number))
    result.check_returncode()
    review = json.loads(result.stdout)
    review["owners"] = {name: owner for name in review["files"] if name != "."}
    review["links"] = list(links)
    return review


def instrument(project, code, *args):
    return subprocess.run([sys.executable, "-B", "-c", code, *args], cwd=project.root,
                          env=environment(), capture_output=True, timeout=25)


RACE = """
import os
import sys
sys.path.insert(0, ".ai-kit/tools")
import kit
import task_moves
native = task_moves.rename_exclusive
def collide(source_fd, source, target_fd, target):
    os.mkdir(target, dir_fd=target_fd)
    before = os.stat(target, dir_fd=target_fd)
    try:
        native(source_fd, source, target_fd, target)
    finally:
        assert os.stat(target, dir_fd=target_fd).st_ino == before.st_ino
task_moves.rename_exclusive = collide
sys.exit(kit.main(sys.argv[1:]))
"""

CROSS_DEVICE = """
import errno
import sys
sys.path.insert(0, ".ai-kit/tools")
import kit
import task_moves
def cross_device(*args):
    raise OSError(errno.EXDEV, "controlled EXDEV")
task_moves.rename_exclusive = cross_device
sys.exit(kit.main(sys.argv[1:]))
"""


class TaskMoveTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="kit-move-")
        self.addCleanup(scratch.cleanup)
        self.project = Project(Path(scratch.name).resolve() / "project")
        self.card = self.project.task(NUMBER, "Различить пустой запрос и отсутствие совпадений.")

    def cli(self, command, *args, code=0, message=""):
        result = self.project.run_cli(command, *args)
        return self.result(result, code, message)

    def result(self, result, code=0, message=""):
        output = (result.stdout + result.stderr).decode()
        self.assertEqual(result.returncode, code, output)
        self.assertIn(message, output)
        return result.stdout

    def apply(self, number=NUMBER, actor=ACTOR, **expected):
        return self.cli("move", *move_args(number, actor), **expected)

    def prepare(self, number=NUMBER, **kwargs):
        review = read_review(self.project, number, **kwargs)
        self.project.save_json(REVIEW, review)
        return review

    def accept(self, **kwargs):
        ref = self.project.commit("Проверенная редакция")
        key = observation(self.card, "Утверждение проверено на указанной версии.", **kwargs)
        accept(self.card, "A-1", key, "Проверка покрывает обязательство.",
               basis=address(ref, OLD + "/task.json"))
        self.project.save_card(NUMBER, self.card)
        return ref

    def test_accepted_bundle_move_git_and_repeat(self):
        f = self.project
        f.write(".gitignore", "/.ai-kit/\n**/private.dat\n")
        f.write(OLD + "/report.md", "[Заключение](notes/review.md)\n")
        f.write(OLD + "/notes/review.md", "Результат подтверждён.\n")
        self.card["assignment"]["description"] = {"path": "description.md"}
        f.save_card(NUMBER, self.card)
        f.write(OLD + "/description.md", "Точный текст поручения.\n")
        child = f.task("P/T-002", "Сохранить размещение дочерней работы.")
        child["parent"] = NUMBER
        f.save_card("P/T-002", child)
        ref = self.accept()
        f.write(OLD + "/.hidden", b"\x00\xff")
        f.write(OLD + "/notes/private.dat", b"ignore\0")
        (f.root / OLD / "empty").mkdir()
        f.write("notes/other.txt", "Чужая индексная версия.")
        f.git("add", "--", "notes/other.txt")
        f.write("notes/other.txt", "Чужая рабочая версия.")
        scope = (f.root / P_SCOPE).read_bytes()
        link = {"document": {"task": NUMBER, "path": "report.md"},
                "target": {"task": NUMBER, "path": "notes/review.md"},
                "relative": "notes/review.md", "owner": PRODUCT}
        review = self.prepare(links=[link])
        self.assertEqual(review["phase"], "rename")
        before = fingerprint(f.root)
        moved = json.loads(self.apply())
        self.assertEqual(moved["phase"], "git")
        self.assertEqual(moved["card_paths"], {
            "worktree": DONE + "/task.json", "index": [OLD + "/task.json"], "head": [OLD + "/task.json"],
        })
        actual = fingerprint(f.root)
        self.assertEqual({name.replace(OLD + "/", DONE + "/", 1) if name.startswith(OLD + "/") else name: info
                          for name, info in before.items()}, actual)
        self.assertEqual((f.root / P_SCOPE).read_bytes(), scope)
        self.assertTrue((f.root / DONE / "empty").is_dir())
        self.assertEqual(json.loads(self.cli("tasks", "--profile", PROFILE, "P/T-002"))["location"], "open")
        self.assertEqual(self.cli("read", "--profile", PROFILE, "--task", NUMBER, "--path", "report.md"),
                         self.cli("read", "--commit", ref, "--path", OLD + "/report.md"))
        self.cli("check", *move_args(), "--actor", ACTOR, "--action", "handoff")
        self.assertEqual(fingerprint(f.root), actual)
        self.assertEqual(json.loads(self.apply())["phase"], "git")
        self.assertEqual(fingerprint(f.root), actual)

        names = ("task.json", "description.md", "report.md", "notes/review.md")
        paths = [f"{folder}/{name}" for folder in (OLD, DONE) for name in names]
        unrelated = f.git("ls-files", "--stage", "--", "notes/other.txt")
        f.git("add", "-A", "--", *paths)
        f.git("commit", "--only", "-m", "Сохранить перенос", "--", *paths)
        self.assertEqual(f.git("ls-files", "--stage", "--", "notes/other.txt"), unrelated)
        self.assertEqual((f.root / "notes/other.txt").read_text(), "Чужая рабочая версия.")
        self.assertNotIn("notes/other.txt", f.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"))
        saved = fingerprint(f.root)
        self.assertEqual(json.loads(self.apply())["phase"], "placed")
        self.assertEqual(fingerprint(f.root), saved)

    def test_waits_for_decision_editor_and_verified_version(self):
        f = self.project
        f.write(OLD + "/result.md", "Готовый отчёт ещё ждёт решения.")
        f.commit("Готовые данные")
        self.assertEqual(self.prepare()["phase"], "decision")
        self.apply(code=1, message="записать решение")
        self.accept()
        self.prepare()
        baseline = fingerprint(f.root)
        self.apply(actor="executor", code=1, message="/actor")
        self.assertEqual(fingerprint(f.root), baseline)
        self.card["decisions"][-1].pop("basis")
        f.save_card(NUMBER, self.card)
        self.prepare()
        self.apply(code=1, message="снимок поручения")
        self.assertTrue((f.root / OLD).exists())
        self.assertFalse((f.root / DONE).exists())

    def test_local_move_requires_a_current_mandate_and_editor(self):
        f = self.project
        child = "F-search/T-002"
        self.card["assignment"].update(stage="implement", inputs=[NORM],
                                       mandate={"scope": "F-search", "role": LOCAL})
        f.save_card(NUMBER, self.card)
        basis = f.commit("Общий мандат")
        mandate = {"task": NUMBER, "basis": address(basis, OLD + "/task.json")}
        f.scope(F_SCOPE, LOCAL, "local-chat", child, mandate=mandate)
        card = f.task(child, "Проверить местное состояние.")
        ref = f.commit("Редакция местного поручения")
        key = observation(card, "Местное состояние соответствует критерию.")
        accept(card, "A-local", key, "Критерий выполнен.",
               basis=address(ref, f.card_path(child)), mandate=mandate)
        f.save_card(child, card)
        self.prepare(child)
        self.apply(child, code=1, message="/actor")
        rule = (f.root / NORM).read_bytes()
        f.write(NORM, "Изменённый вход общего мандата.")
        self.apply(child, "local-chat", code=1, message="существенного файла")
        f.write(NORM, rule)
        moved = json.loads(self.apply(child, "local-chat"))
        self.assertEqual(moved["card_paths"]["worktree"], SOURCE + "/features/F-search/done/T-002/task.json")
        self.assertTrue((f.root / OLD / "task.json").is_file())

    def test_unknown_writer_in_another_task_blocks_accepted_move(self):
        f = self.project
        self.accept()
        candidate = "P/T-002"
        card = f.task(candidate, "Выполнить соседнюю правку.", write_scope=["product"])
        basis = f.commit("Два поручения")
        attempt = f.attempt(candidate, card, "writer-run", "writer", basis, used=["write"])
        f.launch(candidate, card, attempt)
        f.grants("P", candidate, 1, ["write"], "writer-run")
        f.commit("Выдача записи")
        attempt["phase"] = "unknown"
        f.save_card(candidate, card)
        self.prepare()
        before = fingerprint(f.root)
        self.apply(code=1, message="ресурс удерживает P/T-002/writer-run")
        self.assertEqual(fingerprint(f.root), before)

    def test_release_cancel_replacement_reject_and_reopen(self):
        f = self.project
        self.card["assignment"]["write_scope"] = ["product"]
        f.save_card(NUMBER, self.card)
        base = f.commit("Уточнённое поручение")
        attempt = f.attempt(NUMBER, self.card, "run", "writer", base, used=["write"])
        f.launch(NUMBER, self.card, attempt)
        f.grants("P", NUMBER, 1, ["write"], "run")
        f.commit("Выдача прав и запуск")
        attempt["phase"] = "unknown"
        decide(self.card, "cancelled", "Остановка по решению владельца.", revision=1)
        f.save_card(NUMBER, self.card)
        self.assertEqual(self.prepare()["phase"], "waiting")
        self.apply(code=1, message="исход попыток")
        attempt.update(phase="stopped", termination={"by": PRODUCT, "source": "Процесс завершён."})
        f.save_card(NUMBER, self.card)
        self.prepare()
        self.apply()
        self.assertTrue((f.root / CLOSED / "task.json").exists())
        decide(self.card, "reopened", "Нужен отдельный проверочный прогон.", revision=1)
        f.save_json(CLOSED + "/task.json", self.card)
        self.prepare()
        self.apply()
        self.assertTrue((f.root / OLD / "task.json").exists())
        for candidate, outcome in (("P/T-002", "replaced"), ("P/T-003", "rejected")):
            card = f.task(candidate, "Уточнить результат.")
            card.pop("assignment")  # An idea can be closed before any assignment.
            decision = {"outcome": outcome, "by": "Координатор", "role": PRODUCT, "date": "2026-11-23",
                        "reason": "Выбрано отдельное решение."}
            if outcome == "replaced":
                decision["replacement"] = NUMBER
            card["decisions"] = [decision]
            f.save_card(candidate, card)
            self.prepare(candidate)
            self.apply(candidate)
            report = json.loads(self.cli("tasks", "--profile", PROFILE, candidate))
            self.assertEqual(report["card"]["decisions"][-1], decision)
            self.assertEqual(report["location"], "closed")

    def test_conflicts_empty_identical_missing_late_and_cross_device(self):
        f = self.project
        self.accept()
        self.prepare()
        target = f.root / DONE
        target.mkdir(parents=True)
        for duplicate in (False, True):
            if duplicate:
                shutil.copytree(f.root / OLD, target)
            before = fingerprint(f.root)
            stderr = f.run_cli("move", *move_args()).stderr.decode()
            for path in (OLD, DONE):
                self.assertIn(path, stderr)
            self.assertEqual(fingerprint(f.root), before)
            shutil.rmtree(target)
        self.cli("move", *move_args("P/T-009"), code=1, message="P/T-009")
        before = fingerprint(f.root)
        command = ("move", "--root", ".", *move_args(actor=ACTOR))
        self.result(instrument(f, CROSS_DEVICE, *command), 1, "разные файловые системы")
        self.assertEqual(fingerprint(f.root), before)
        self.result(instrument(f, RACE, *command), 1, "назначение занято")
        self.assertTrue(target.is_dir())
        self.assertFalse(list(target.iterdir()))
        self.assertEqual(fingerprint(f.root), before)

    def test_review_stale_bytes_unknown_owner_and_archived_link(self):
        f = self.project
        report = OLD + "/report.md"
        f.write(report, "Отчёт")
        archive = SOURCE + "/closed/T-003/reference.md"
        card = f.task("P/T-003", "Хранить ссылку из архива.")
        decide(card, "rejected", "Материалы остаются для чтения.")
        f.save_json(str(Path(archive).with_name("task.json")), card)
        shutil.rmtree(f.root / f.card_path("P/T-003").removesuffix("/task.json"))
        f.write(archive, "[Отчёт](../../open/T-001/report.md)\n")
        self.accept()
        f.write(OLD + "/borrowed.txt", "Чужая версия из индекса")
        f.git("add", "--", OLD + "/borrowed.txt")
        f.write(OLD + "/borrowed.txt", "Чужая рабочая версия")
        review = self.prepare()
        review["owners"].pop("borrowed.txt")
        f.save_json(REVIEW, review)
        self.apply(code=1, message="владелец материала")
        review["owners"]["borrowed.txt"] = {"index": "Чужой редактор", "worktree": "Чужой редактор"}
        link = {"document": archive, "target": {"task": NUMBER, "path": "report.md"},
                "relative": "../../open/T-001/report.md", "owner": "Владелец архива"}
        review["links"] = [link]
        f.save_json(REVIEW, review)
        self.apply(code=1, message="после переноса ссылка")
        link["next_step"] = "Заменить ссылку после согласования с редактором архива."
        f.save_json(REVIEW, review)
        self.apply(code=1, message="Владелец архива")
        del link["next_step"]
        link["relative"] = "../../done/T-001/report.md"
        f.write(archive, "[Отчёт](../../done/T-001/report.md)\n")
        f.save_json(REVIEW, review)
        f.write(report, "Авторы изменили байты после осмотра")
        self.apply(code=1, message="изменились")
        self.assertTrue((f.root / OLD).exists())
        review = self.prepare(links=[link])
        review["owners"]["borrowed.txt"] = {"index": "Чужой редактор", "worktree": "Чужой редактор"}
        f.save_json(REVIEW, review)
        before = (f.root / ".git/index").read_bytes()
        moved = json.loads(self.apply())
        version = moved["files"]["borrowed.txt"]
        self.assertNotEqual(version["index"][0]["blob"], version["worktree"]["blob"])
        self.assertEqual((f.root / ".git/index").read_bytes(), before)
        self.assertEqual((f.root / DONE / "borrowed.txt").read_text(), "Чужая рабочая версия")
