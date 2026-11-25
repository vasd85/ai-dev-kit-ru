"""Package 1 fixtures exercise the copied kit from the product root."""

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_kit import Fixture as GitFixture, KIT, P1_RESULT, P1_STATE, STATE, environment, fingerprint

from read_support import Problem
from task_reader import TaskReader


PROFILE = "ops/project.md"
SOURCE = "planning/new work"
WAITING = f"{SOURCE}/open/T-008-empty-search/task.json"
OLD_REPORT = f"{SOURCE}/open/T-009-copy/report.md"
NEW_REPORT = f"{SOURCE}/done/T-009-copy/report.md"
OLD_BYTES = "Первая редакция отчёта.\r\nБез завершающего перевода".encode()
AR_ASSIGN = "archive/order.json"
AR_RESULT = "archive/result.json"
AR_OTHER = "archive/feature/state.json"


class CardProject(GitFixture):
    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        installed = self.root / ".ai-kit"
        shutil.copytree(KIT, installed, ignore=shutil.ignore_patterns("tests", "examples", "__pycache__"))
        self.write("AGENTS.md", f"# Учебный проект\n\nЗаполненный профиль — `{PROFILE}`. "
                                "Маршрут карточек — `.ai-kit/README.md#task-cards`.\n")
        self.marker = f'<!-- ai-kit:task-source {json.dumps({"format_version": "0.3.0", "path": SOURCE})} -->'
        self.write(PROFILE, "# Профиль учебного проекта\n\n" + self.marker + "\n\n"
                            "Владелец разрешил подготовить проверку пустого поиска только в учебной копии.\n")
        self.save_json(STATE, P1_STATE)
        self.write("docs/ai-work/project.md", "# Прежний учебный источник\n")
        self.write("source.py", "def normalize(value):\n    return value.strip()\n")
        self.write("test_source.py", "import unittest\n")
        for area, role in (("", "Координатор продукта"), ("/features/F-search", "Координатор поиска")):
            self.write(f"{SOURCE}{area}/scope.md", f"# {role}\n\nЕдинственный редактор — учебный чат.\n"
                       "Роль и полномочия — по учебному профилю.\n"
                       f"Точка продолжения — {'F-search' if area else 'P'}/T-008.\n"
                       f"Общая выдача — `{SOURCE}/resources.md`.\n")
        self.write(f"{SOURCE}/resources.md", "# Общая выдача\n\nВедёт координатор продукта. Выдач нет.\n")

        self.wait = self.card("T-008", "Сообщение при пустом поиске", "Объяснить отсутствие результатов",
                              "При отсутствии результатов виден текст «Ничего не найдено».")
        self.wait["assignment"]["description"] = {"path": "description.md"}
        self.wait["dependencies"] = [{"task": "F-search/T-008", "criterion": "C-1", "acceptance": "A-search"}]
        self.wait["waiting"] = {"remaining": "Проверить текст на экране при запросе без совпадений.",
                                "reason": "Результат ручной проверки экрана ещё не записан.",
                                "next_step": "Открыть поиск, ввести «zz-no-results» и сверить видимый текст."}
        self.save_json(WAITING, self.wait)
        self.write(WAITING.removesuffix("task.json") + "description.md",
                   "При запросе без совпадений поиск сохраняет введённый текст и показывает подсказку.\n")
        self.supplier_path = f"{SOURCE}/features/F-search/done/T-008-empty-result/task.json"
        self.supplier = self.card("T-008", "Пустой ответ поиска", "Вернуть пустой список",
                                  "Запрос без совпадений возвращает [].")
        self.supplier["parent"] = "P/T-008"
        self.accept_card(self.supplier, "A-search", "Учебное входное наблюдение: ответ [].")
        self.save_json(self.supplier_path, self.supplier)
        self.moving = self.card("T-009", "Заголовок пустого экрана", "Уточнить заголовок",
                                "Согласован текст заголовка пустого экрана.")
        self.moving["parent"] = "F-search/T-008"
        self.save_json(OLD_REPORT.removesuffix("report.md") + "task.json", self.moving)
        self.write(OLD_REPORT, OLD_BYTES)

        restored = {"format_version": "0.3.0", "id": "T-010", "title": "Проверка старого экспорта",
                    "goal": "Установить, сохранился ли файл открытия экспорта",
                    "origin": {"status": "Done", "source": "Учебный старый список, строка 4"},
                    "waiting": {"remaining": "Проверить открытие файла экспорта.",
                                "reason": "Сохранилась только отметка Done.",
                                "next_step": "Найти проверяемый файл и установить критерий проверки."}}
        self.save_json(f"{SOURCE}/open/T-010-restored/task.json", restored)
        closed = {"format_version": "0.3.0", "id": "T-011", "title": "Вторая подсказка",
                  "goal": "Обсудить дополнительную подсказку", "parent": "P/T-008",
                  "decisions": [{"outcome": "rejected", "by": "Владелец продукта",
                                 "role": "Принимающий", "date": "2026-11-21",
                                 "reason": "Одной подсказки достаточно."}]}
        self.save_json(f"{SOURCE}/features/F-search/closed/T-011-extra/task.json", closed)

        self.assignment = {key: value for key, value in P1_STATE["task"]["assignment"].items()
                           if key not in ("executor", "authorized_action", "write_files")}
        self.assignment.update(id="B-order", revision=2, goal="Проверить новый экран",
                               stage="implement", write_scope=["source.py"], dependencies=[])
        check = {key: value for key, value in P1_RESULT["checks"][0].items() if key != "criteria"}
        check["covers"] = [{"work": "A", "assignment_revision": 4, "criterion": "C-api"}]
        self.result = dict(P1_RESULT, id="B-result", revision=3, assignment_id="B-order",
                           assignment_revision=2, attempt_id="B-run", checks=[check])
        self.criterion = {"id": "C-api", "expectation": "Пустой поиск возвращает пустой массив"}
        self.save_json(AR_ASSIGN, self.assignment)
        self.save_json(AR_RESULT, self.result)
        self.save_json(AR_OTHER, {"format_version": "0.1-p2", "works": [{
            "id": "A", "assignment": {"path": "archive/feature/order.json"},
            "acceptances": [{"id": "A-accepted", "assignment_revision": 4}],
        }, {
            "id": "B", "assignment": {"path": AR_ASSIGN},
            "attempts": [{"id": "B-run", "assignment_revision": 2, "result": {"path": AR_RESULT}}],
        }]})
        self.save_json("archive/feature/order.json", {"id": "A-order", "revision": 4,
                                                     "criteria": [self.criterion]})
        (self.root / "archive/link").symlink_to("order.json")
        self.git("init", "--quiet", "--template=", "--initial-branch=main")
        self.first = self.commit("Первый снимок учебных данных")
        (self.root / NEW_REPORT).parent.parent.mkdir()
        (self.root / OLD_REPORT).parent.rename((self.root / NEW_REPORT).parent)
        self.accept_card(self.moving, "A-copy", "Учебное наблюдение: согласован текст заголовка.")
        self.save_json(NEW_REPORT.removesuffix("report.md") + "task.json", self.moving)
        self.write(NEW_REPORT, "Текущая редакция отчёта\n")
        (self.root / "archive").rename(self.root / "old-archive")
        self.second = self.commit("Второй снимок: карточка и отчёт в done")
        self.write("scratch.txt", "Содержимое индекса\n")
        self.git("add", "--", "scratch.txt")
        self.write("scratch.txt", "Отличающиеся рабочие байты\n")

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode() if isinstance(value, str) else value)

    def save_json(self, path, value):
        self.write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def card(number, title, goal, criterion):
        card = json.loads((KIT / "templates/task.json").read_text())
        card.update(id=number, title=title, goal=goal)
        card["assignment"].update(description=goal, boundaries="Только проверка учебного примера",
                                  acceptor="Координатор", authority={"by": "Владелец проекта",
                                                                   "source": PROFILE})
        card["assignment"]["criteria"][0]["expectation"] = criterion
        return card

    @staticmethod
    def accept_card(card, key, fact):
        card["observations"] = [{"id": "O-1", "by": "Автор учебных данных", "date": "2026-11-21",
                                 "revision": 1, "criteria": ["C-1"], "fact": fact,
                                 "source": "Подготовленное наблюдение в учебных данных"}]
        card["decisions"] = [{"id": key, "outcome": "accepted", "role": "Координатор",
                              "by": "Автор учебных данных", "date": "2026-11-21", "revision": 1,
                              "reason": "Учебные данные покрывают условие", "observations": ["O-1"]}]

    def run_cli(self, command, *args):
        env = environment()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        return subprocess.run([sys.executable, ".ai-kit/tools/kit.py", command, "--root", ".", *args],
                              cwd=self.root, env=env, capture_output=True)

    def history(self, path, selector=None):
        select = [] if selector is None else ["--select", json.dumps(selector)]
        return self.run_cli("read", "--commit", self.first, "--path", path, *select)


class TaskCardsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="kit-cards-")
        self.addCleanup(temp.cleanup)
        self.project = CardProject(Path(temp.name).resolve() / "project")

    def output(self, result, code=0, message=""):
        self.assertEqual(result.returncode, code, (result.stdout + result.stderr).decode())
        self.assertIn(message, (result.stdout + result.stderr).decode())
        return result.stdout

    def test_selected_profile_lookup_and_listing_from_product_root(self):
        f = self.project
        before = fingerprint(f.root)
        self.output(f.run_cli("check", "--profile", PROFILE), message="0.3.0; 5 карточек")
        self.output(f.run_cli("check", "--state", STATE), message="ограниченный")
        selected = json.loads(self.output(f.run_cli("tasks", "--profile", PROFILE, "P/T-008")))
        self.assertIn("подсказку", selected["description_text"])
        self.assertEqual(selected["card"]["waiting"], f.wait["waiting"])
        entries = json.loads(self.output(f.run_cli("tasks", "--profile", PROFILE)))
        self.assertEqual([entry["id"] for entry in entries],
                         ["F-search/T-008", "F-search/T-011", "P/T-008", "P/T-009", "P/T-010"])
        self.assertEqual({entry["location"] for entry in entries}, {"open", "done", "closed"})
        self.assertEqual(selected["card"], entries[2]["card"])
        self.assertEqual([entries[i]["card"]["parent"] for i in (0, 3)], ["P/T-008", "F-search/T-008"])
        supplier = json.loads(self.output(f.run_cli("tasks", "--profile", PROFILE, "F-search/T-008")))
        self.assertEqual(supplier["location"], "done")
        self.assertEqual(supplier["card"]["decisions"][-1]["id"], selected["card"]["dependencies"][0]["acceptance"])
        self.assertEqual(fingerprint(f.root), before)

    def test_history_is_exact_and_independent_of_the_current_tree(self):
        f = self.project
        self.assertFalse((f.root / OLD_REPORT).exists())
        before = fingerprint(f.root)
        self.assertEqual(self.output(f.history(OLD_REPORT)), OLD_BYTES)
        self.assertEqual(self.output(f.run_cli("read", "--profile", PROFILE, "--task", "P/T-009",
                                              "--path", "report.md")).decode(), "Текущая редакция отчёта\n")
        self.output(f.history("archive/link"), 1, "символических ссылок")
        self.output(f.history("archive/feature"), 1, "обычный файл")
        self.output(f.history("../" + OLD_REPORT), 1, "нормализованный")
        self.output(f.run_cli("read", "--commit", "HEAD", "--path", NEW_REPORT), 1, "полный идентификатор")
        self.assertEqual(fingerprint(f.root), before)
        # A symlink at the obsolete live location must not affect Git reads.
        (f.root / OLD_REPORT).parent.symlink_to("../done/T-009-copy")
        self.output(f.run_cli("check", "--profile", PROFILE), 1, "символические ссылки")
        self.assertEqual(self.output(f.history(OLD_REPORT)), OLD_BYTES)

    def test_independent_historical_record_locators(self):
        f = self.project
        before = fingerprint(f.root)
        assignment = {"kind": "assignment", "key": "B-order", "revision": 2}
        result = {"kind": "result", "key": "B-result", "revision": 3}
        criterion = {"work": "A", "kind": "criterion", "key": "C-api", "revision": 4}
        self.assertEqual(json.loads(self.output(f.history(AR_ASSIGN, assignment))), f.assignment)
        self.assertEqual(json.loads(self.output(f.history(AR_RESULT, result))), f.result)
        self.assertEqual(json.loads(self.output(f.history(AR_OTHER, criterion))), f.criterion)
        self.assertEqual(json.loads(self.output(f.history(AR_OTHER, {**result, "work": "B", "attempt": "B-run"}))),
                         f.result)
        self.assertEqual(json.loads(self.output(f.history(AR_OTHER, {**assignment, "work": "B"}))), f.assignment)
        self.output(f.history(AR_OTHER, {"work": "A", "kind": "acceptance", "key": "A-accepted", "revision": 4}))
        self.assertEqual(json.loads(self.output(f.history(
            STATE, {"work": "task-1", "kind": "assignment", "key": "assignment-1", "revision": 1}))),
            P1_STATE["task"]["assignment"])
        self.output(f.history(AR_OTHER, {**criterion, "key": "not-here"}), 1, "нет единственной записи")
        self.output(f.history(AR_OTHER, {**criterion, "revision": 1}), 1, "ожидалось 1, записано 4")
        self.assertEqual(fingerprint(f.root), before)

    def test_standalone_historical_selection_requires_record_kind(self):
        f = self.project
        key, rev = "Z-neutral", 7
        order = dict(f.assignment, id=key, revision=rev)
        result = dict(f.result, id=key, revision=rev, commit=f.first)
        limited = dict(P1_STATE["task"]["assignment"], id=key, revision=rev)
        ledger = {"format_version": "0.2.0", "id": key, "revision": rev, "works": []}
        for path, data in (("result.json", order), ("assignment.json", result),
                           ("old-assignment.json", limited), ("ledger.json", ledger)):
            f.save_json(path, data)
        commit = f.commit("Historical record types with unrelated names")

        def read(path, kind=None):
            args = [] if kind is None else ["--select", json.dumps({"kind": kind, "key": key,
                                                                   "revision": rev})]
            return f.run_cli("read", "--commit", commit, "--path", path, *args)

        for path, kind, data in (("result.json", "assignment", order),
                                 ("assignment.json", "result", result),
                                 ("old-assignment.json", "assignment", limited)):
            with self.subTest(path=path, kind=kind):
                self.assertEqual(json.loads(self.output(read(path, kind))), data)
        for path, kind in (("result.json", "result"), ("assignment.json", "assignment"),
                           ("ledger.json", "result")):
            with self.subTest(path=path, kind=kind):
                response = read(path, kind)
                self.assertEqual(self.output(response, 1, f"отдельной записи {kind}"), b"")
                self.assertIn(path, response.stderr.decode())
        self.assertEqual(self.output(read("result.json")), (f.root / "result.json").read_bytes())

    def test_open_card_observation_must_reference_its_declared_criterion(self):
        f = self.project
        observation = dict(f.supplier["observations"][0], criteria=["K-ghost"])
        f.save_json(WAITING, {**f.wait, "observations": [observation]})
        response = f.run_cli("check", "--profile", PROFILE)
        self.output(response, 1, "в редакции поручения 1 отсутствует критерий 'K-ghost'")
        self.assertIn(WAITING, response.stdout.decode())
        observation["criteria"] = ["C-1"]
        f.save_json(WAITING, {**f.wait, "observations": [observation]})
        self.output(f.run_cli("check", "--profile", PROFILE))

    def test_later_acceptance_cannot_hide_unknown_criterion_in_same_revision(self):
        f = self.project
        observation = dict(f.supplier["observations"][0], id="O-selected", criteria=["K-ghost"])
        earlier = dict(f.supplier["decisions"][0], id="A-selected", observations=["O-selected"])
        link = {"task": "F-search/T-008", "criterion": "K-ghost", "acceptance": "A-selected"}
        f.save_json(WAITING, {**f.wait, "dependencies": [link]})
        supplier = {**f.supplier, "observations": [observation, *f.supplier["observations"]],
                    "decisions": [earlier, *f.supplier["decisions"]]}
        f.save_json(f.supplier_path, supplier)
        response = f.run_cli("check", "--profile", PROFILE)
        self.output(response, 1, "в редакции поручения 1 отсутствует критерий 'K-ghost'")
        self.assertIn(f.supplier_path, response.stdout.decode())
        observation["criteria"] = ["C-1"]
        link["criterion"] = "C-1"
        f.save_json(f.supplier_path, supplier)
        f.save_json(WAITING, {**f.wait, "dependencies": [link]})
        self.output(f.run_cli("check", "--profile", PROFILE), message="приёмка A-selected")

    def test_dependency_can_keep_evidence_of_an_earlier_assignment_revision(self):
        f = self.project
        assignment = dict(f.supplier["assignment"], revision=2,
                          criteria=[{"id": "C-new", "expectation": "Изменённый критерий"}])
        reopen = {"outcome": "reopened", "by": "Редактор", "role": "Координатор",
                  "date": "2026-11-22", "reason": "Изменилась задача"}
        f.save_json(f.supplier_path, {**f.supplier, "assignment": assignment,
                                     "decisions": [*f.supplier["decisions"], reopen]})
        result = f.run_cli("check", "--profile", PROFILE)
        self.output(result, message="приёмка A-search у F-search/T-008 доступна как история")
        self.assertIn("проверьте её применимость", result.stdout.decode())

    def test_invalid_external_description_fails_only_when_opening_details(self):
        f = self.project
        path = (f.root / WAITING).with_name("description.md")
        for content, reason in ((b"", "непустая строка"), (" \t\r\n\u2003".encode(), "непустая строка"),
                                (b"\xff", "должно быть в кодировке UTF-8")):
            with self.subTest(content=content):
                path.write_bytes(content)
                reader = TaskReader.from_profile(f.root, PROFILE)
                self.assertNotIn("description_text", reader.find("P/T-008"))
                with self.assertRaises(Problem) as caught:
                    reader.show("P/T-008")
                self.assertEqual(caught.exception.where, "P/T-008/description.md")
                self.assertIn(reason, caught.exception.message)
                self.output(f.run_cli("tasks", "--profile", PROFILE))
                response = f.run_cli("tasks", "--profile", PROFILE, "P/T-008")
                self.assertEqual(self.output(response, 1, reason), b"")
                self.assertIn("P/T-008/description.md", response.stderr.decode())

    def test_external_description_is_preserved_alongside_inline_and_idea_details(self):
        f = self.project
        content = "  Текст с отступом \t\r\n\r\n  вторая строка  \n"
        (f.root / WAITING).with_name("description.md").write_bytes(content.encode())
        listed = json.loads(self.output(f.run_cli("tasks", "--profile", PROFILE)))
        self.assertTrue(all("description_text" not in entry for entry in listed))
        reader = TaskReader.from_profile(f.root, PROFILE)
        for number in ("P/T-008", "P/T-009", "P/T-010"):
            with self.subTest(number=number):
                detail = json.loads(self.output(f.run_cli("tasks", "--profile", PROFILE, number)))
                self.assertEqual(detail, reader.show(number))
                if number == "P/T-008":
                    self.assertEqual(detail["description_text"], content)
                else:
                    self.assertNotIn("description_text", detail)
        self.assertEqual(reader.show("P/T-009")["card"]["assignment"]["description"],
                         f.moving["assignment"]["description"])
        self.assertNotIn("assignment", reader.show("P/T-010")["card"])

    def test_bad_or_ambiguous_profile_does_not_fall_back_to_valid_state(self):
        f = self.project
        for text, code, message in (
                (f.marker + "\n" + f.marker, 1, "найдено 2"),
                (f.marker.replace(SOURCE, "../forbidden"), 1, "нормализованный"),
                (f.marker.replace(SOURCE, "missing"), 1, "отсутствует источник"),
                (f.marker.replace("0.3.0", "9.9.9"), 2, "неизвестный формат"),
        ):
            with self.subTest(message=message):
                f.write(PROFILE, text + "\n")
                self.output(f.run_cli("check", "--profile", PROFILE), code, message)
        f.write(PROFILE, f'<!-- ai-kit:task-source {json.dumps({"format_version": "0.1.0", "path": STATE})} -->\n')
        self.output(f.run_cli("check", "--profile", PROFILE), message="состояние ready")

    def test_bad_tree_and_links_report_the_exact_conflict(self):
        f = self.project
        variations = (
            ({"parent": ["F-search/T-008", "P/T-010"]}, "один родитель"),
            ({"parent": "P/T-123"}, "отсутствует родитель P/T-123"),
            ({"parent": "F-search/T-008"}, "цикл вложенности"),
            ({"dependencies": [{"task": "P/T-123", "criterion": "C-1", "acceptance": None}]},
             "отсутствует цель P/T-123"),
            ({"dependencies": [{"task": "F-search/T-008", "criterion": "missing", "acceptance": "A-search"}]},
             "нет обязательства missing"),
            ({"dependencies": [{"task": "F-search/T-008", "criterion": "C-1", "acceptance": "missing"}]},
             "нет выбранной приёмки missing"),
        )
        for replacement, message in variations:
            with self.subTest(message=message):
                f.save_json(WAITING, {**f.wait, **replacement})
                self.output(f.run_cli("check", "--profile", PROFILE), 1, message)
        f.save_json(WAITING, f.wait)
        link = {"task": "P/T-008", "criterion": "C-1", "acceptance": None}
        f.save_json(f.supplier_path, {**f.supplier, "dependencies": [link]})
        self.output(f.run_cli("check", "--profile", PROFILE), 1, "цикл зависимостей")
        f.save_json(f.supplier_path, f.supplier)
        duplicate = WAITING.replace("/open/", "/done/")
        f.save_json(duplicate, f.wait)
        for _ in range(2):
            self.output(f.run_cli("tasks", "--profile", PROFILE, "P/T-008"), 1, "дубль полного номера")
        (f.root / duplicate).unlink()
        (f.root / duplicate).parent.rmdir()
        f.save_json(WAITING, {**f.wait, "format_version": "0.3-p1"})
        self.output(f.run_cli("check", "--profile", PROFILE), 2, "format_version")

    def test_decision_placement_and_reopened_dependency(self):
        f = self.project
        new_path = f.supplier_path.replace("/done/", "/open/")
        (f.root / new_path).parent.parent.mkdir()
        (f.root / f.supplier_path).parent.rename((f.root / new_path).parent)
        self.output(f.run_cli("check", "--profile", PROFILE), message="решение требует done")
        reopened = {"outcome": "reopened", "role": "Координатор", "by": "Учебный редактор",
                    "date": "2026-11-21", "reason": "Повторно проверяем изменённый источник"}
        f.save_json(new_path, {**f.supplier, "decisions": [*f.supplier["decisions"], reopened]})
        self.output(f.run_cli("check", "--profile", PROFILE), message="доступна как история")
        f.save_json(new_path, {**f.supplier, "observations": []})
        self.output(f.run_cli("check", "--profile", PROFILE), 1, "нет наблюдения O-1")


def demo():
    with tempfile.TemporaryDirectory(prefix="kit-cards-demo-") as temp:
        f = CardProject(Path(temp).resolve() / "project")
        before = fingerprint(f.root)
        entry = (f.root / "AGENTS.md").read_text()
        profile, route = entry.split("`")[1::2]
        readme, anchor = route.split("#")
        assert f'<a id="{anchor}"></a>' in (f.root / readme).read_text()
        print(entry)
        print((f.root / profile).read_text())
        print("Два учебных коммита:", f.git("log", "--format=%H %s"), sep="\n")
        print("Индекс и рабочее дерево перед чтением:", f.git("status", "--short"), sep="\n")
        print("Бывший путь отчёта отсутствует:", not (f.root / OLD_REPORT).exists())

        def step(command, *args, code=0, summarize=None):
            print("\n$", shlex.join(["python3", ".ai-kit/tools/kit.py", command, "--root", ".", *args]))
            process = f.run_cli(command, *args)
            assert process.returncode == code, (process.stdout, process.stderr)
            if summarize:
                print(json.dumps(summarize(json.loads(process.stdout)), ensure_ascii=False, indent=2))
            elif process.stdout:
                print(process.stdout.decode().rstrip())
            if process.stderr:
                print(process.stderr.decode().rstrip())
            return process.stdout

        step("check", "--profile", profile)
        item = json.loads(step("tasks", "--profile", profile, "P/T-008"))
        waiting = item["card"]["waiting"]
        print("\nОтвет преемника по данным карточки:")
        print("Критерий.", item["card"]["assignment"]["criteria"][0]["expectation"])
        print("Осталось.", waiting["remaining"])
        print("Причина ожидания.", waiting["reason"])
        print("Дальше.", waiting["next_step"])
        step("tasks", "--profile", profile,
             summarize=lambda entries: [{k: entry[k] for k in ("id", "location", "path")} for entry in entries])
        step("tasks", "--profile", profile, "F-search/T-008", summarize=lambda data: data["card"]["decisions"])
        step("tasks", "--profile", profile, "P/T-009",
             summarize=lambda data: {k: data[k] for k in ("id", "location", "path")})
        step("read", "--profile", profile, "--task", "P/T-009", "--path", "report.md")
        raw = step("read", "--commit", f.first, "--path", OLD_REPORT)
        assert raw == OLD_BYTES
        print("Точные байты:", repr(raw), "\nSHA-256:", hashlib.sha256(raw).hexdigest())
        for path, selector in (
                (AR_ASSIGN, {"kind": "assignment", "key": "B-order", "revision": 2}),
                (AR_RESULT, {"kind": "result", "key": "B-result", "revision": 3}),
                (AR_OTHER, {"work": "A", "kind": "criterion", "key": "C-api", "revision": 4})):
            step("read", "--commit", f.first, "--path", path, "--select", json.dumps(selector))
        step("read", "--commit", f.first, "--path", AR_OTHER, "--select",
             '{"work":"A","kind":"criterion","key":"unknown","revision":4}', code=1)
        step("read", "--commit", f.first, "--path", "../archive/order.json", code=1)
        step("read", "--commit", f.first, "--path", "archive/link", code=1)
        step("check", "--state", STATE)
        after = fingerprint(f.root)
        assert before == after
        for name, entries in (("до", before), ("после", after)):
            digest = hashlib.sha256(repr(sorted(entries.items())).encode()).hexdigest()
            print(f"Отпечаток {name}: {digest} ({len(entries)} путей)")
        print("Снимки файлов и служебных данных Git идентичны.")
        print(f.git("status", "--short"))


if __name__ == "__main__":
    if sys.argv[1:] == ["--demo"]:
        demo()
    else:
        unittest.main()
