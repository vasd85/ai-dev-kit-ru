"""Exercise the four package 4 corrections through an installed kit."""

import json
from pathlib import Path
import tempfile
import unittest

from test_kit import fingerprint
from test_task_actions import PROFILE, Project, accept, address, decide, observation
from test_task_moves import ACTOR, DONE, NUMBER, OLD, REVIEW, move_args, read_review


CARD = "/task.json"


class TaskMoveFixTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="kit-move-fixes-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()

    def project(self, label):
        project = Project(self.scratch / label)
        card = project.task(NUMBER, "Проверить результат поиска.")
        return project, card

    def cli(self, project, command, *args, code=0, message=""):
        result = project.run_cli(command, *args)
        output = (result.stdout + result.stderr).decode()
        self.assertEqual(result.returncode, code, output)
        self.assertIn(message, output)
        return output

    def action(self, project, action, number=NUMBER, **expected):
        return self.cli(project, "check", *move_args(number), "--action", action, "--actor", ACTOR, **expected)

    def inspect(self, project, number=NUMBER):
        result = project.run_cli("move", *move_args(number))
        result.check_returncode()
        return json.loads(result.stdout)

    def move(self, project, **expected):
        return self.cli(project, "move", *move_args(actor=ACTOR), **expected)

    def prepare(self, project):
        project.save_json(REVIEW, read_review(project))

    def accept(self, project, card, key, basis):
        fact = observation(card, f"Наблюдение по проверке {key}.")
        accept(card, key, fact, "Критерий выполнен.", basis=address(basis, OLD + CARD))
        project.save_card(NUMBER, card)

    def save_paths(self, project, paths):
        project.git("--literal-pathspecs", "add", "-A", "--", *paths)
        project.git("--literal-pathspecs", "commit", "--only", "-m", "Согласованные пути", "--", *paths)

    def test_literal_selection_preserves_other_files_and_index_entries(self):
        for name in ("draft[1].md", "draft-one.md"):
            with self.subTest(name=name):
                f, card = self.project(name)
                neighbour = OLD + "/draft1.md"
                moved_neighbour = DONE + "/draft1.md"
                outside = "notes/other.txt"
                f.write(OLD + "/" + name, "Согласованный отчёт.\n")
                f.write(neighbour, "Запись из HEAD.\n")
                basis = f.commit("Исходный комплект")
                self.accept(f, card, "A-first", basis)
                for path in (neighbour, outside):
                    f.write(path, "Подготовленная версия.\n")
                    (f.root / path).chmod(0o755)
                    f.git("--literal-pathspecs", "add", "--", path)
                    f.write(path, "Отдельная рабочая версия.\n")
                    (f.root / path).chmod(0o644)
                foreign_paths = (neighbour, moved_neighbour, outside)
                foreign = f.git("--literal-pathspecs", "ls-files", "--stage", "--", *foreign_paths)
                work = {p: (f.root / p).read_bytes() for p in (neighbour, outside)}
                self.prepare(f)
                self.move(f)
                paths = [base + suffix for base in (OLD, DONE) for suffix in (CARD, "/" + name)]
                before = f.git("--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv",
                               "--name-only", "--", *paths)
                self.assertEqual(set(before.splitlines()), set(paths[:2]))
                index = f.git("--literal-pathspecs", "diff", "--cached", "--name-only", "--", *paths)
                self.assertEqual(index, "")
                f.git("--literal-pathspecs", "add", "-A", "--", *paths)
                prepared = f.git("--literal-pathspecs", "diff", "--cached", "--no-renames",
                                 "--name-only", "--", *paths)
                self.assertEqual(set(prepared.splitlines()), set(paths))
                self.assertEqual(f.git("--literal-pathspecs", "ls-files", "--stage", "--", *foreign_paths), foreign)
                for path in paths[2:]:
                    self.assertEqual(f.git("rev-parse", ":" + path), f.git("hash-object", "--no-filters", path))
                f.git("--literal-pathspecs", "commit", "--only", "-m", "Только карточка и отчёт", "--", *paths)
                changed = f.git("diff-tree", "-r", "--no-commit-id", "--no-renames", "--name-only", "HEAD")
                self.assertEqual(set(changed.splitlines()), set(paths))
                self.assertEqual(f.git("--literal-pathspecs", "ls-files", "--stage", "--", *foreign_paths), foreign)
                self.assertEqual({neighbour: (f.root / moved_neighbour).read_bytes(),
                                  outside: (f.root / outside).read_bytes()}, work)
                self.assertEqual(f.git("rev-parse", "HEAD:" + neighbour), f.git("rev-parse", basis + ":" + neighbour))
                for path in paths[2:]:
                    self.assertEqual(f.git("rev-parse", "HEAD:" + path), f.git("hash-object", "--no-filters", path))

    def test_saved_withdrawal_in_either_layer_blocks_stale_move_and_handoff(self):
        for layer in ("HEAD", "индексе"):
            with self.subTest(layer=layer):
                f, card = self.project("withdraw-" + layer)
                path = f.root / (OLD + CARD)
                checked = f.commit("Проверенная редакция")
                self.accept(f, card, "A-first", checked)
                old = path.read_bytes()
                accepted = f.commit("Первая приёмка")
                decide(card, "revoked", "Владелец отозвал приёмку.", revokes="A-first", revision=1)
                f.save_card(NUMBER, card)
                if layer == "HEAD":
                    self.save_paths(f, [OLD + CARD])
                    f.git("--literal-pathspecs", "restore", "--staged", "--source=" + accepted, "--", OLD + CARD)
                else:
                    f.git("--literal-pathspecs", "add", "--", OLD + CARD)
                path.write_bytes(old)
                self.prepare(f)
                before = fingerprint(f.root)
                for error in (self.move(f, code=1, message="восстановите сохранённые решения"),
                              self.action(f, "handoff", code=1, message="история решений")):
                    for detail in (NUMBER, layer, OLD + CARD, "revoked"):
                        self.assertIn(detail, error)
                self.assertEqual(fingerprint(f.root), before)
                self.assertFalse((f.root / DONE).exists())

                path.write_text(json.dumps({k: v for k, v in card.items() if k not in ("decisions", "observations")}))
                self.action(f, "review", code=1, message="история решений")
                decide(card, "reopened", "Повторная проверка разрешена.", revision=1)
                f.save_card(NUMBER, card)
                self.action(f, "review")
                self.accept(f, card, "A-recheck", checked)
                # Formatting and a new observation do not rewrite earlier decisions.
                path.write_text(json.dumps(card, ensure_ascii=False, sort_keys=True))
                self.action(f, "handoff")
                self.prepare(f)
                self.move(f)
                self.assertEqual(json.loads((f.root / (DONE + CARD)).read_bytes())["decisions"], card["decisions"])

    def test_first_working_acceptance_can_extend_an_undecided_card(self):
        f, card = self.project("first-decision")
        basis = f.commit("Поручение без решения")
        self.action(f, "review")
        self.accept(f, card, "A-first", basis)
        self.action(f, "handoff")
        self.prepare(f)
        self.move(f)
        self.assertEqual(self.inspect(f)["card_paths"]["worktree"], DONE + CARD)

    def test_product_waiting_survives_pending_partial_and_saved_return(self):
        f, card = self.project("return-with-waiting")
        waiting = {"remaining": "Проверить версию X.", "reason": "Новая версия недоступна.",
                   "next_step": "Дождаться версии X от владельца продукта."}
        f.write(OLD + "/report.md", "Отчёт о прежней версии.\n")
        card["waiting"] = waiting
        f.save_card(NUMBER, card)
        basis = f.commit("Обычное ожидание до решения")
        self.assertEqual(self.inspect(f)["phase"], "waiting")
        card.pop("waiting")
        self.accept(f, card, "A-first", basis)
        self.prepare(f)
        self.move(f)
        for suffix in (CARD, "/report.md"):
            self.save_paths(f, [OLD + suffix, DONE + suffix])
        decide(card, "revoked", "Требуется новая версия.", revokes="A-first", revision=1)
        card["waiting"] = waiting
        f.save_json(DONE + CARD, card)
        self.prepare(f)
        review = json.loads((f.root / REVIEW).read_bytes())
        review.pop("waiting")  # Earlier saved inspections did not have this display field.
        f.save_json(REVIEW, review)
        self.move(f)

        expected = {"worktree": OLD + CARD, "head": [DONE + CARD], "index": [DONE + CARD]}
        self.assertEqual(self.inspect(f)["card_paths"], expected)
        self.assertEqual(self.inspect(f)["waiting"], waiting)
        self.assertEqual(self.inspect(f)["phase"], "git")
        self.assertIn("Владельцу Git", self.inspect(f)["next_step"])
        self.save_paths(f, [DONE + CARD, OLD + CARD])
        partial = self.inspect(f)
        self.assertEqual(partial["card_paths"], {"worktree": OLD + CARD, "head": [OLD + CARD], "index": [OLD + CARD]})
        self.assertEqual(partial["files"]["report.md"]["index"][0]["path"], DONE + "/report.md")
        self.assertEqual(partial["phase"], "git")
        self.assertEqual(partial["waiting"], waiting)
        self.assertIn("прежние пути", partial["next_step"])
        self.save_paths(f, [DONE + "/report.md", OLD + "/report.md"])
        finished = self.inspect(f)
        self.assertEqual((finished["phase"], finished["next_step"], finished["waiting"]),
                         ("waiting", waiting["next_step"], waiting))

    def test_changed_report_names_the_real_path_in_each_layer(self):
        f, supplier = self.project("dependent-report")
        consumer = "P/T-002"
        old, live = OLD + "/report.md", DONE + "/report.md"
        contents = b"Verified report.\n"
        f.write(old, contents)
        original = f.commit("Проверенный отчёт в open")
        attempt = f.attempt(NUMBER, supplier, "study", "reader", original)
        f.launch(NUMBER, supplier, attempt)
        f.submitted(NUMBER, supplier, attempt, "Заключение", artifacts=[old])
        fact = observation(supplier, "Отчёт проверен.", attempt="study")
        accept(supplier, "A-study", fact, "Критерий выполнен.", attempt="study")
        f.save_card(NUMBER, supplier)
        accepted = f.commit("Приёмка исследования")
        card = f.task(consumer, "Использовать результат исследования.")
        card["dependencies"] = [{"task": NUMBER, "criterion": "C-1", "acceptance": "A-study",
                                 "basis": address(accepted, OLD + CARD)}]
        f.save_card(consumer, card)
        f.commit("Сохранить зависимость")
        self.prepare(f)
        self.move(f)
        self.action(f, "review", consumer)
        f.write(live, b"Changed working report.\n")
        error = self.action(f, "review", consumer, code=1, message="рабочие байты")
        self.assertIn(live, error)
        self.assertNotIn(old, error)
        bad_blob = f.git("hash-object", "-w", "--no-filters", live)
        f.write(live, contents)
        self.action(f, "review", consumer)
        oid = f.git("rev-parse", "HEAD:" + old)
        f.git("update-index", "--cacheinfo", f"100644,{bad_blob},{old}")
        error = self.action(f, "review", consumer, code=1, message="индексе")
        self.assertIn(old, error)
        self.assertNotIn(live, error)
        f.git("update-index", "--cacheinfo", f"100644,{oid},{old}")
        self.action(f, "review", consumer)

        # Save the card's new location in the index ahead of its report.
        f.git("--literal-pathspecs", "add", "-A", "--", OLD + CARD, DONE + CARD)
        self.assertEqual(self.inspect(f)["card_paths"]["index"], [DONE + CARD])
        self.action(f, "review", consumer)
        f.git("update-index", "--cacheinfo", f"100644,{bad_blob},{old}")
        error = self.action(f, "review", consumer, code=1, message="отличается в индексе")
        self.assertIn(old, error)
        self.assertNotIn(live, error)
        f.git("update-index", "--cacheinfo", f"100644,{oid},{old}")
        self.action(f, "review", consumer)
        f.git("--literal-pathspecs", "add", "--", live)
        error = self.action(f, "review", consumer, code=1, message="несколько адресов материала в индексе")
        self.assertIn(old, error)
        self.assertIn(live, error)
        f.git("--literal-pathspecs", "add", "-A", "--", old, live)
        self.action(f, "review", consumer)
        result = f.run_cli("read", "--commit", original, "--path", old)
        result.check_returncode()
        self.assertEqual(result.stdout, contents)
