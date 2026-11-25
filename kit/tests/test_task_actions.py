"""Exercise the card workflow through the installed command from the product root."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from test_kit import KIT, fingerprint
from test_task_cards import CardProject

PROFILE = "ops/project.md"
SOURCE = "docs/tasks"
BOOK = SOURCE + "/resources.json"
P_SCOPE = SOURCE + "/scope.md"
F_SCOPE = SOURCE + "/features/F-search/scope.md"
LOCAL = "Координатор поиска"
PRODUCT = "Координатор продукта"
DATE = "2026-11-22"
NORM = "docs/product/search.md"

EVIDENCE = None
OBSERVATIONS = []


class Project(CardProject):
    def __init__(self, root):
        self.root = root
        root.mkdir()
        shutil.copytree(KIT, root / ".ai-kit", ignore=shutil.ignore_patterns("tests", "examples", "__pycache__"))
        self.write(".gitignore", "/.ai-kit/\n")
        self.write("AGENTS.md", "# Учебный поиск\n\nПрофиль: `ops/project.md`. "
                                "Вход кита: `.ai-kit/README.md#task-cards`.\n")
        self.write(PROFILE, '# Профиль\n\n'
                   '<!-- ai-kit:task-source {"format_version":"0.3.0","path":"docs/tasks"} -->\n'
                   '\nДействующее правило поиска: `docs/product/search.md`. '
                   'Учебные проверки локальные, внешняя доставка запрещена.\n')
        self.write(NORM, "# Правило поиска\n\nЗапрос без совпадений возвращает пустой список.\n")
        self.write("product/search.py", 'import sys\n\n'
                   'if sys.argv[1] == "zz-no-results":\n    print("Ничего не найдено")\n')
        self.write("product/second.txt", "Учебный исходник второй работы.\n")
        self.write("notes/other.txt", "Файл соседней области.\n")
        self.save_json(BOOK, {"editor": PRODUCT, "grants": []})
        self.scope(P_SCOPE, PRODUCT, "product-chat", "P/T-001", resources=BOOK)
        self.git("init", "--quiet", "--template=", "--initial-branch=main")

    def scope(self, path, role, session, number, **values):
        data = {"editor": {"role": role, "session": session}, "continuation": number, **values}
        notes = ("Общий чат закрыт в учебном сценарии.\n\n" if session == "product-closed" else "")
        self.write(path, f"# {role}\n\n{notes}<!-- ai-kit:scope {json.dumps(data, ensure_ascii=False)} -->\n")

    def card_path(self, number):
        scope, task = number.split("/")
        return f"{SOURCE}{'' if scope == 'P' else '/features/' + scope}/open/{task}/task.json"

    def save_card(self, number, card):
        self.save_json(self.card_path(number), card)

    def task(self, number, expectation, **assignment):
        template = self.card(number.split("/")[1], expectation, expectation, expectation)
        role = PRODUCT if number.startswith("P/") else LOCAL
        template["assignment"].update(acceptor=role, **assignment)
        self.save_card(number, template)
        return template

    def grants(self, holder, number, rev, names, attempt=None):
        rows = [{"resource": resource, "holder": holder, "work": number, "revision": rev} for resource in names]
        if attempt:
            for row in rows:
                row["attempt"] = attempt
        self.save_json(BOOK, {"editor": PRODUCT, "grants": rows})

    def attempt(self, number, card, key, executor, ref, used=(), **extra):
        value = {"id": key, "executor": executor, "revision": card["assignment"]["revision"],
                 "basis": address(ref, self.card_path(number)), "phase": "proposed",
                 "resources": list(used), **extra}
        card.setdefault("attempts", []).append(value)
        self.save_card(number, card)
        return value

    def launch(self, number, card, attempt):
        ref = self.commit(f"Точное назначение {attempt['id']}")
        attempt.update(phase="assigned", launch=address(ref, self.card_path(number)))
        self.save_card(number, card)

    def submitted(self, number, card, attempt, fact, artifacts=(), outcome="submitted"):
        path = self.card_path(number).removesuffix("task.json") + attempt["id"] + ".md"
        self.write(path, f"# Отчёт {attempt['id']}\n\n{fact}\n")
        ref = self.commit(f"Отчёт {attempt['id']}")
        attempt.update(phase="finished", termination={
            "by": card["assignment"]["acceptor"],
            "source": "Учебный родитель дождался завершения своей команды без потомков.",
        }, result={"outcome": outcome, "source": address(ref, path), "artifacts": list(artifacts)})
        self.save_card(number, card)

    def card_json(self, number):
        return json.loads((self.root / self.card_path(number)).read_text())

    def local_search(self):
        return subprocess.run([sys.executable, "-B", "product/search.py", "zz-no-results"], cwd=self.root,
                              capture_output=True, check=True, text=True).stdout.strip()


def address(ref, path):
    return {"commit": ref, "path": path}


def observation(card, fact, author="Пользователь", source="Сообщение пользователя 22 ноября, учебный ввод", **extra):
    key = "O-" + str(len(card.get("observations", [])) + 1)
    card.setdefault("observations", []).append({"id": key, "by": author, "date": DATE,
                                                "revision": card["assignment"]["revision"], "criteria": ["C-1"],
                                                "fact": fact, "source": source, **extra})
    return key


def decide(card, outcome, reason, **extra):
    card.setdefault("decisions", []).append({
        "outcome": outcome, "by": card["assignment"]["acceptor"] + " (учебная роль)",
        "role": card["assignment"]["acceptor"], "date": DATE, "reason": reason, **extra})
    return card["decisions"][-1]


def accept(card, key, obs, reason, **extra):
    return decide(card, "accepted", reason, id=key, revision=card["assignment"]["revision"],
                  observations=[obs], **extra)


class TaskActionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="kit-card-actions-")
        self.addCleanup(temp.cleanup)
        self.f = Project(Path(temp.name).resolve() / "project")

    def observe(self, label, cmd, *args, code=0, message=""):
        before = fingerprint(self.f.root)
        result = self.f.run_cli(cmd, *args)
        self.assertEqual(fingerprint(self.f.root), before, label)
        stdout, stderr = result.stdout.decode(), result.stderr.decode()
        self.assertEqual(result.returncode, code, (label, stdout, stderr))
        self.assertIn(message, stdout + stderr, label)
        if EVIDENCE:
            OBSERVATIONS.append({"case": self._testMethodName, "label": label,
                                 "command": ["python3", ".ai-kit/tools/kit.py", cmd, "--root", ".", *args],
                                 "code": result.returncode, "stdout": stdout, "stderr": stderr,
                                 "files_and_git_unchanged": True})
        return stdout

    def action(self, label, action, number, actor, attempt=None, **expected):
        options = ["--attempt", attempt] if attempt else []
        return self.observe(label, "check", "--profile", PROFILE, "--task", number,
                            "--action", action, "--actor", actor, *options, **expected)

    def show(self, label, number):
        return json.loads(self.observe(label, "tasks", "--profile", PROFILE, number))

    def checkpoint(self, name):
        if EVIDENCE:
            archive = EVIDENCE / (name + ".tar.gz")
            with tarfile.open(archive, "w:gz") as handle:
                for path in sorted(self.f.root.iterdir()):
                    if path.name != ".ai-kit":
                        handle.add(path, arcname=path.name)

    def test_user_observation_waiting_and_authorized_scope_change(self):
        f = self.f
        number, other = "P/T-001", "P/T-002"
        criterion = 'На версии X при пустом поиске виден текст «Ничего не найдено».'
        good = f.task(number, criterion)
        partial = f.task(other, criterion)
        base = f.commit("Начальные критерии и локальная учебная копия")
        self.checkpoint("package4-user-before")
        self.action("Принимающий сверяет постановку", "review", number, "product-chat")
        obs = observation(good, "Открыл X, ввёл zz-no-results, увидел «Ничего не найдено».")
        accept(good, "A-empty-X", obs, "Сообщение называет требуемую версию, действие и видимый результат.",
               basis=address(base, f.card_path(number)))
        f.save_card(number, good)
        self.action("Наблюдение пользователя принято без запуска агента", "handoff", number, "product-chat")
        row = self.show("Приёмка ждёт переноса", number)
        self.assertEqual((row["location"], row["expected_location"]), ("open", "done"))
        self.assertNotIn("attempts", row["card"])
        self.checkpoint("package4-user-done-pending")

        self.action("Второе сообщение рассматривается тем же маршрутом", "review", other, "product-chat")
        # The changed string and this local execution do not establish what is on X.
        local = f.local_search()
        self.assertEqual(local, "Ничего не найдено")
        partial["waiting"] = {"remaining": "Увидеть текст после пустого поиска на X.",
                              "reason": "Пользователь сообщил «я поменял строку». "
                                        f"Локальная копия вывела «{local}». Экран X доступен только пользователю.",
                              "next_step": "Пользователю открыть X, выполнить пустой поиск и сообщить видимый текст."}
        partial["observations"] = [{
            "id": "O-local", "by": PRODUCT, "date": DATE, "revision": 1, "criteria": ["C-1"],
            "fact": f"Локальный запуск product/search.py zz-no-results вывел «{local}». Версия X не проверена.",
            "source": "Локальная учебная копия, команда запущена из корня проекта",
        }]
        f.write(f.card_path(other).removesuffix("task.json") + "user-message.md",
                "Пользователь, 22 ноября: «я поменял строку».\n")
        f.save_card(other, partial)
        self.action("Для неполного наблюдения передача закрыта", "handoff", other, "product-chat",
                    code=1, message="accepted")
        self.assertEqual(self.show("Точный остаток по X", other)["expected_location"], "open")
        self.checkpoint("package4-user-still-open")

        f.write("scope-change.md", "Пользователь разрешил учесть локальную версию отдельно.\n")
        scope_change = f.commit("Решение владельца: этот результат ограничен локальной версией")
        partial["revisions"] = [{"revision": 1, "basis": address(base, f.card_path(other)),
                                 "authorized_by": "Пользователь", "reason": "Экран X проверит владелец отдельной задачей.",
                                 "source": address(scope_change, "scope-change.md")}]
        partial["assignment"]["revision"] = 2
        partial["assignment"]["criteria"][0]["expectation"] = (
            "При локальном поиске без совпадений виден текст «Ничего не найдено».")
        partial.pop("waiting")
        f.save_card(other, partial)
        f.task("P/T-003", "Пользователь проверил экран на X после пустого поиска.")
        checked = f.commit("Новая редакция локальной проверки и отдельная проверка X")
        self.observe("Старая редакция сохранена", "read", "--commit", base, "--path", f.card_path(other),
                     message=criterion)
        self.action("Проверка разрешённой редакции 2", "review", other, "product-chat")
        obs = observation(partial, f"Локальная проверка дала «{f.local_search()}».", author=PRODUCT,
                          source="python3 -B product/search.py zz-no-results")
        accept(partial, "A-local", obs, "Применён явно разрешённый критерий локальной версии.",
               basis=address(checked, f.card_path(other)))
        f.save_card(other, partial)
        self.action("Приёмка пересмотренного объёма", "handoff", other, "product-chat")

    def test_description_change_in_worktree_index_and_head(self):
        f = self.f
        number = "P/T-001"
        card = f.task(number, "Проверить поведение по длинному описанию.", description={"path": "description.md"},
                      inputs=[NORM])
        desc = f.card_path(number).removesuffix("task.json") + "description.md"
        old, new = "Проверить только пустой поиск.\n", "Добавить отдельный API поиска.\n"
        f.write(desc, old)
        other = f.task("P/T-002", "Уточнить независимую задачу.")
        ref = f.commit("Исходное поручение с отдельным описанием")
        attempt = f.attempt(number, card, "run-1", "reader-one", ref)
        self.action("Поручение из сохранённого снимка", "assign", number, "product-chat", "run-1")
        f.launch(number, card, attempt)
        other["goal"] = "Исправлена цель соседней независимой задачи."
        f.save_card("P/T-002", other)
        self.action("Соседняя правка не останавливает запуск", "execute", number, "reader-one", "run-1")
        f.write(desc, new)
        self.checkpoint("review-description-same-reference")
        self.action("Рабочее описание изменено по прежнему адресу и редакции", "execute", number, "reader-one",
                    "run-1", code=1, message="рабочие байты")
        f.git("add", "--", desc)
        f.write(desc, old)
        self.action("Индекс расходится даже при прежних рабочих байтах", "execute", number, "reader-one",
                    "run-1", code=1, message="отличается в индексе")
        f.git("commit", "--quiet", "-m", "Подменённое описание в HEAD")
        f.git("add", "--", desc)
        self.action("HEAD расходится с исходным сохранённым поручением", "execute", number, "reader-one",
                    "run-1", code=1, message="отличается в HEAD")
        self.observe("Прежние байты извлекаются через исторический вход", "read",
                     "--commit", ref, "--path", desc, message=old)
        f.commit("Восстановлено первоначальное описание")
        attempt["executor"] = "other-reader"
        f.save_card(number, card)
        self.action("Исполнитель отличается от сохранённого запуска", "execute", number, "other-reader",
                    "run-1", code=1, message="назначение исполнителя")

    def feature(self, stage="implement"):
        f = self.f
        parent, first, second, outsider = "P/T-001", "F-search/T-011", "F-search/T-012", "P/T-009"
        root = f.task(parent, "Координировать улучшение поиска.", stage=stage,
                      mandate={"scope": "F-search", "role": LOCAL},
                      inputs=[NORM],
                      write_scope=["product"] if stage == "implement" else [],
                      resources=["git"] if stage == "implement" else [])
        a = f.task(first, "Пустой поиск выдаёт нужный текст.", write_scope=["product/search.py"],
                   inputs=[NORM], resources=["git"])
        b = f.task(second, "Дополнить учебный тест поиска.", write_scope=["product/second.txt"], inputs=[NORM])
        c = f.task(outsider, "Изменить заметки вне поиска.", write_scope=["notes/other.txt"])
        f.scope(P_SCOPE, PRODUCT, "product-closed", parent, resources=BOOK)
        if stage == "implement":
            f.grants("F-search", parent, 1, ["write", "git"])
        ref = f.commit("Действующая редакция общего мандата и местных задач")
        f.scope(F_SCOPE, LOCAL, "feature-chat", first, mandate={"task": parent, "basis": address(ref, f.card_path(parent))})
        return parent, first, second, outsider, root, a, b, c, ref

    def finish_first(self):
        f = self.f
        parent, first, second, outsider, root, a, b, c, ref = self.feature()
        run = f.attempt(first, a, "first", "implementer", ref, ["write", "git"])
        self.action("Местный координатор назначил первого", "assign", first, "feature-chat", "first")
        f.launch(first, a, run)
        self.action("Первый сверил местную запись и Git", "execute", first, "implementer", "first")
        result = f.local_search()
        self.assertEqual(result, "Ничего не найдено")
        f.submitted(first, a, run, f"Команда пустого поиска вывела «{result}».", ["product/search.py"])
        self.action("Местная приёмка при закрытом общем чате", "review", first, "feature-chat", "first")
        obs = observation(a, f"Локальный пустой поиск вывел «{result}».",
                          author="implementer", source=run["result"]["source"], attempt="first")
        accept(a, "A-first", obs, "Первая работа покрывает критерий функции.", attempt="first",
               mandate={"task": parent, "basis": address(ref, f.card_path(parent))})
        f.save_card(first, a)
        self.action("Местный вклад готов к передаче", "handoff", first, "feature-chat", "first")
        return parent, first, second, outsider, root, a, b, c, ref

    def test_two_sequential_writers_and_a_successor(self):
        f = self.f
        parent, first, second, outsider, root, a, b, c, ref = self.finish_first()
        issue = (f.root / BOOK).read_bytes()
        f.attempt(outsider, c, "outside", "notes-agent", ref, ["write"])
        self.action("Чужая область ждёт даже в промежутке", "assign", outsider, "product-closed", "outside",
                    code=1, message="закреплён за F-search")
        run = f.attempt(second, b, "second", "test-implementer", ref, ["write"])
        f.scope(F_SCOPE, LOCAL, "feature-successor", second,
                mandate={"task": parent, "basis": address(ref, f.card_path(parent))})
        self.checkpoint("review-lawful-second")
        self.assertIn(PROFILE, (f.root / "AGENTS.md").read_text())
        self.assertIn('"continuation": "F-search/T-012"', (f.root / F_SCOPE).read_text())
        self.assertTrue(self.show("Преемник нашёл конец первой попытки", first)["card"]["attempts"][0]["termination"])
        self.assertEqual(self.show("Преемник нашёл второе поручение", second)["card"]["attempts"][0]["id"], "second")
        self.action("Новая местная сессия правомерно назначает второго", "assign", second,
                    "feature-successor", "second")
        f.launch(second, b, run)
        self.action("Второй исполнитель вправе начать", "execute", second, "test-implementer", "second")
        self.assertEqual((f.root / BOOK).read_bytes(), issue)
        if EVIDENCE:
            OBSERVATIONS.append({"case": self._testMethodName, "resource_sha256_before": hashlib.sha256(issue).hexdigest(),
                                 "resource_sha256_after": hashlib.sha256((f.root / BOOK).read_bytes()).hexdigest()})

    def test_unknown_attempt_and_cancelled_work_retain_right(self):
        f = self.f
        parent, first, second, outsider, root, a, b, c, ref = self.feature()
        run = f.attempt(first, a, "first", "implementer", ref, ["write", "git"])
        self.action("Первое назначение разрешено", "assign", first, "feature-chat", "first")
        f.launch(first, a, run)
        run["phase"] = "unknown"
        a["waiting"] = {"remaining": "Установить, остановился ли первый исполнитель и его команды.",
                        "reason": "Чат закрыт после просьбы о паузе, прекращение не подтверждено.",
                        "next_step": "Установить исход запуска first и проверить запущенные им команды."}
        f.save_card(first, a)
        f.attempt(second, b, "second", "test-implementer", ref, ["write"])
        f.scope(F_SCOPE, LOCAL, "feature-successor", first,
                mandate={"task": parent, "basis": address(ref, f.card_path(parent))})
        self.checkpoint("review-unknown-first")
        self.action("Исход первого неизвестен, второе назначение ждёт", "assign", second,
                    "feature-successor", "second", code=1, message="F-search/T-011/first")
        decide(a, "cancelled", "Владелец отменил исправление отображения.")
        f.save_card(first, a)
        row = self.show("Преемник прочитал конкретный остаток по отмене", first)
        self.assertEqual(row["expected_location"], "open")
        self.assertIn("проверить запущенные", row["card"]["waiting"]["next_step"])
        self.action("Отмена не прекратила писателя", "assign", second, "feature-successor", "second",
                    code=1, message="F-search/T-011/first")
        run.update(phase="stopped", termination={"by": LOCAL, "source": "Учебный родитель подтвердил прекращение команды."})
        a.pop("waiting")
        f.save_card(first, a)
        self.assertEqual(self.show("После прекращения отменённая папка ожидается в closed", first)["expected_location"],
                         "closed")
        self.action("После установленного прекращения следующий запуск возможен", "assign", second,
                    "feature-successor", "second")

    def test_fix_01_next_writer_checks_saved_rights_and_termination_source(self):
        f = self.f
        _, first, second, _, _, old, new, _, order = self.feature()
        issue = (f.root / BOOK).read_bytes()
        read_task = "P/T-010"
        reader = f.task(read_task, "Прочитать учебную запись без изменений.", stage="prepare")
        reading = f.commit("Подготовлено независимое чтение")
        f.attempt(read_task, reader, "inspect", "observer", reading)
        previous = f.attempt(first, old, "first", "implementer", order, ["write", "git"])
        candidate = f.attempt(second, new, "second", "implementer-two", order, ["write"])
        self.action("1 · Первое назначение", "assign", first, "feature-chat", "first")
        f.launch(first, old, previous)
        previous.update(phase="unknown", resources=[])
        f.save_card(first, old)
        self.action("1 · Из рабочего списка исчезло право записи", "assign", second, "feature-chat", "second",
                    code=1, message=f"{first}/first: назначение исполнителя или ресурсов отличается")
        f.launch(second, new, candidate)  # Model an erroneous issue, with no second process started.
        self.action("1 · Повторная защита перед стартом", "execute", second, "implementer-two", "second",
                    code=1, message=f"{first}/first: назначение исполнителя или ресурсов отличается")
        self.action("1 · Независимое чтение при расхождении", "assign", read_task, "product-closed", "inspect")
        previous["resources"] = ["write", "git"]
        f.save_card(first, old)
        f.commit("Восстановлены сохранённые права прежней попытки")
        stale = f.git("show", previous["launch"]["commit"] + ":" + f.card_path(first)) + "\n"
        f.write(f.card_path(first), stale)
        for action, who in (("assign", "feature-chat"), ("execute", "implementer-two")):
            self.action(f"1 · {action} при старой рабочей версии с одним предложением", action, second, who, "second",
                        code=1, message=f"{first}/first: назначение сохранено в HEAD")
        missing = {key: value for key, value in old.items() if key != "attempts"}
        f.save_card(first, missing)
        for action, who in (("assign", "feature-chat"), ("execute", "implementer-two")):
            self.action(f"1 · {action} при исчезнувшей попытке", action, second, who, "second",
                        code=1, message=f"{first}/first: назначение сохранено в HEAD")
        self.action("1 · Независимое чтение при неполной рабочей карточке", "assign", read_task,
                    "product-closed", "inspect")
        f.save_card(first, old)
        previous["resources"] = []
        f.save_card(first, old)
        f.git("add", "--", f.card_path(first))
        previous["resources"] = ["write", "git"]
        f.save_card(first, old)
        self.action("1 · Исправленный рабочий список ещё расходится с индексом", "assign", second,
                    "feature-chat", "second", code=1, message=f"{first}/first: назначение исполнителя или ресурсов отличается в индексе")

        output = f.local_search()
        self.assertEqual(output, "Ничего не найдено")
        proof_path = "notes/termination.md"
        f.write(proof_path, f"Поиск завершился с кодом 0, вывод «{output}». Команда не запускала потомков.\n")
        proof = f.commit("Подтверждённое завершение первой команды")
        previous.update(phase="finished", termination={"by": LOCAL, "source": address(proof, "notes/missing.md")})
        f.save_card(first, old)
        self.action("1 · Адрес подтверждения окончания отсутствует", "assign", second, "feature-chat", "second",
                    code=1, message=f"{first}/first/termination: свидетельство {proof}:notes/missing.md")
        self.action("1 · Недоступное подтверждение останавливает и старт", "execute", second, "implementer-two",
                    "second", code=1, message=f"{first}/first/termination: свидетельство {proof}:notes/missing.md")
        self.action("1 · Независимое чтение при недоступном подтверждении", "assign", read_task, "product-closed",
                    "inspect")
        previous["termination"]["source"]["path"] = proof_path
        f.save_card(first, old)
        (f.root / proof_path).unlink()  # The reference names the historical file.
        self.action("1 · После подтверждения второе назначение возможно", "assign", second, "feature-chat", "second")
        self.action("1 · После подтверждения второй исполнитель может начать", "execute", second,
                    "implementer-two", "second")
        self.assertEqual((f.root / BOOK).read_bytes(), issue)

    def test_fix_02_direct_acceptance_respects_local_mandate_without_a_write_grant(self):
        f = self.f
        parent, own, outside = "P/T-001", "F-search/T-011", "F-search/T-012"
        common = f.task(parent, "Принимать работу поиска в product/.", stage="implement",
                        mandate={"scope": "F-search", "role": LOCAL}, write_scope=["product"],
                        boundaries="Приёмка изменений в payroll/ остаётся у координатора продукта.")
        rejected, admitted = "P/T-002", "P/T-003"
        consumer_wrong = f.task(rejected, "Использовать результат по зарплатам.")
        consumer_right = f.task(admitted, "Использовать результат поиска.")
        allowed_path, forbidden_path = "product/flag.txt", "payroll/flag.txt"
        f.write(allowed_path, "20\n")
        f.write(forbidden_path, "20\n")
        allowed = f.task(own, "Пользователь проверил значение 20 в product/.", write_scope=[allowed_path])
        forbidden = f.task(outside, "Пользователь проверил значение 20 в payroll/.", write_scope=[forbidden_path])
        ref = f.commit("Работа пользователя и отрезок полномочий местного редактора")
        mandate = {"task": parent, "basis": address(ref, f.card_path(parent))}
        f.scope(F_SCOPE, LOCAL, "feature-chat", own, mandate=mandate)
        self.assertEqual(json.loads((f.root / BOOK).read_text())["grants"], [])
        self.action("2 · В своём модуле рассмотрение доступно", "review", own, "feature-chat")
        self.action("2 · Принятие payroll не входит в общий мандат", "review", outside, "feature-chat",
                    code=1, message=f"{parent}/mandate: отрезок записи вне мандата местной роли: {forbidden_path}")
        for number, card, path in ((own, allowed, allowed_path), (outside, forbidden, forbidden_path)):
            self.assertEqual((f.root / path).read_text().strip(), "20")
            obs = observation(card, "В проверенной версии записано 20.", source=address(ref, path))
            accept(card, "A-user", obs, "Наблюдение пользователя о сохранённом значении.",
                   basis=address(ref, f.card_path(number)), mandate=mandate)
            f.save_card(number, card)
        self.action("2 · Сдача работы пользователя внутри мандата", "handoff", own, "feature-chat")
        self.action("2 · Записанное чужое решение не даёт права передачи", "handoff", outside, "feature-chat",
                    code=1, message=f"{parent}/mandate: отрезок записи вне мандата местной роли: {forbidden_path}")
        self.assertNotIn("attempts", self.show("2 · Допустимая приёмка без агентского запуска", own)["card"])
        signed = f.commit("Две учебные записи местного решения")
        for target, consuming, supplier in ((rejected, consumer_wrong, outside), (admitted, consumer_right, own)):
            consuming["dependencies"] = [{"task": supplier, "criterion": "C-1", "acceptance": "A-user",
                                           "basis": address(signed, f.card_path(supplier))}]
            f.save_card(target, consuming)
        prepared = f.commit("Потребители выбирают разные приёмки")
        bad_candidate = f.attempt(rejected, consumer_wrong, "use", "reader", prepared)
        good_candidate = f.attempt(admitted, consumer_right, "use", "reader", prepared)
        diagnostic = f"{rejected}/dependencies/{outside}/C-1/mandate/{parent}: отрезок записи вне мандата"
        self.action("2 · Потребитель чужой приёмки не может назначить работу", "assign", rejected, "product-chat",
                    "use", code=1, message=diagnostic)
        f.launch(rejected, consumer_wrong, bad_candidate)
        self.action("2 · Потребитель чужой приёмки не может исполнить работу", "execute", rejected, "reader", "use",
                    code=1, message=diagnostic)
        self.action("2 · Потребитель законного вклада может назначить работу", "assign", admitted, "product-chat",
                    "use")
        f.launch(admitted, consumer_right, good_candidate)
        self.action("2 · Потребитель законного вклада может начать", "execute", admitted, "reader", "use")

        common["assignment"].update(revision=2, write_scope=["product", "payroll"],
                                    boundaries="Роль расширена для будущих приёмок.")
        f.save_card(parent, common)
        expanded = f.commit("Новое право относится к дальнейшей работе")
        f.scope(F_SCOPE, LOCAL, "feature-chat", own,
                mandate={"task": parent, "basis": address(expanded, f.card_path(parent))})
        self.action("2 · Расширение полномочий не легализует прежнее решение", "execute", rejected, "reader", "use",
                    code=1, message=diagnostic)
        bad_candidate.update(phase="stopped", termination={"by": PRODUCT,
                             "source": "Учебная попытка не запускалась: проверка отказала."})
        consumer_wrong["dependencies"][0]["basis"] = address(expanded, f.card_path(outside))
        f.save_card(rejected, consumer_wrong)
        late = f.commit("Потребитель выбрал поздний снимок прежнего решения")
        f.attempt(rejected, consumer_wrong, "use-later", "reader", late)
        self.action("2 · Поздний снимок решения не меняет его мандат", "assign", rejected, "product-chat",
                    "use-later", code=1, message="общее поручение в снимке приёмки отличается от мандата решения")
        obs = observation(common, "Общий координатор проверил вклад и закрыл своё поручение.")
        accept(common, "A-close", obs, "Функция завершена.", basis=address(expanded, f.card_path(parent)))
        f.save_card(parent, common)
        f.commit("Общий мандат закрыт")
        self.action("2 · Закрытый мандат исключает новую местную передачу", "handoff", own, "feature-chat",
                    code=1, message="общее поручение функции закрыто")
        self.action("2 · После закрытия мандата законный вклад применим", "execute", admitted, "reader", "use")

    def test_fix_02_old_wide_mandate_cannot_validate_a_new_local_decision(self):
        f = self.f
        parent, supplier, consumer = "P/T-001", "F-search/T-011", "P/T-002"
        master = f.task(parent, "Принимать изменения в разрешённых модулях.", stage="implement",
                        mandate={"scope": "F-search", "role": LOCAL}, write_scope=["product", "payroll"])
        former = {"task": parent, "basis": address(f.commit("Прежний широкий мандат"), f.card_path(parent))}
        master["assignment"].update(revision=2, write_scope=["product"], boundaries="Изменения payroll принимает P.")
        f.save_card(parent, master)
        current = {"task": parent, "basis": address(f.commit("Право приёмки сужено"), f.card_path(parent))}
        payroll = "payroll/new.txt"
        card = f.task(supplier, "Проверить новую запись модуля payroll.", write_scope=[payroll], inputs=[payroll])
        dependent = f.task(consumer, "Опирается на новую запись payroll.")
        f.write(payroll, "Проверенное значение после сужения мандата.\n")
        f.scope(F_SCOPE, LOCAL, "feature-chat", supplier, mandate=current)
        verified = f.commit("Новые задача и файл после сужения мандата")
        self.assertFalse(f.git("ls-tree", former["basis"]["commit"], "--", payroll, f.card_path(supplier)))
        obs = observation(card, "Пользователь проверил новое значение.", source=address(verified, payroll))
        accept(card, "A-stale", obs, "В учебной записи ошибочно указан прежний мандат.",
               basis=address(verified, f.card_path(supplier)), mandate=former)
        f.save_card(supplier, card)
        signed = f.commit("Недействительное решение со старой ссылкой")
        dependent["dependencies"] = [{"task": supplier, "criterion": "C-1", "acceptance": "A-stale",
                                      "basis": address(signed, f.card_path(supplier))}]
        f.save_card(consumer, dependent)
        prepared = f.commit("Потребитель выбрал ошибочное решение")
        run = f.attempt(consumer, dependent, "wrong", "reader", prepared)
        self.action("2 · Устаревший широкий мандат не даёт права передать новую приёмку", "handoff", supplier,
                    "feature-chat", code=1, message="отрезок записи вне мандата")
        mismatch = "мандат решения отличается от записанного в scope.md на снимке приёмки"
        self.action("2 · Ссылка на широкое прежнее поручение не разрешает потребителя", "assign", consumer,
                    "product-chat", "wrong", code=1, message=mismatch)
        f.launch(consumer, dependent, run)
        self.action("2 · То же при начале зависимой работы", "execute", consumer, "reader", "wrong",
                    code=1, message=mismatch)

        f.scope(F_SCOPE, LOCAL, "feature-chat", supplier, mandate=former)
        stale_scope = f.commit("Даже записанный местный указатель остался устаревшим")
        run.update(phase="stopped", termination={"by": PRODUCT, "source": "В учебной пробе запуск не выполнялся."})
        dependent["dependencies"][0]["basis"] = address(stale_scope, f.card_path(supplier))
        f.save_card(consumer, dependent)
        order = f.commit("Снимок с одинаковой, но устаревшей ссылкой")
        f.attempt(consumer, dependent, "stale-scope", "reader", order)
        self.action("2 · Устаревший местный scope не расширяет общего поручения", "assign", consumer, "product-chat",
                    "stale-scope", code=1, message="общее поручение в снимке приёмки отличается от мандата решения")

        rightful = "P/T-003"
        approved = f.task(rightful, "Общий редактор проверяет действующее значение payroll.", inputs=[payroll])
        checked = f.commit("Поручение на приёмку в общей области")
        self.action("2 · Компетентный редактор может рассмотреть ту же работу пользователя", "review", rightful,
                    "product-chat")
        fact = observation(approved, "Значение подтверждено по файлу пользователя.", source=address(checked, payroll))
        accept(approved, "A-product", fact, "Решение принято уполномоченной общей ролью.",
               basis=address(checked, f.card_path(rightful)))
        f.save_card(rightful, approved)
        self.action("2 · Законная приёмка в общей области передаётся", "handoff", rightful, "product-chat")

    def test_fix_03_direct_handoff_keeps_the_checked_version_after_commits(self):
        f = self.f
        number = "P/T-001"
        scanner, description = "scanner.py", f.card_path(number).replace("task.json", "description.md")
        card = f.task(number, "На штрихкод B9 сканер выводит B9.", description={"path": "description.md"},
                      inputs=[scanner])
        f.write(scanner, 'print("B9")\n')
        f.write(description, "Проверить штрихкод B9, сохранив точные байты сканера.\n")

        def scan(expected):
            command = [sys.executable, "-B", scanner]
            actual = subprocess.check_output(command, cwd=f.root, text=True).strip()
            self.assertEqual(actual, expected)
            if EVIDENCE:
                OBSERVATIONS.append({"case": self._testMethodName, "product_command": ["python3", "-B", scanner],
                                     "observed": actual})

        checked = f.commit("Проверяемый сканер и точное поручение")
        scan("B9")
        self.action("3 · Рассмотрена проверенная версия", "review", number, "product-chat")
        obs = observation(card, "Сканер на B9 вывел B9.")
        old = accept(card, "A-scanner", obs, "Сканер проверен на исходной версии.",
                     basis=address(checked, f.card_path(number)))
        f.save_card(number, card)
        accepted = f.commit("Первоначальная приёмка B9")
        self.action("3 · Точная приёмка передаётся", "handoff", number, "product-chat")
        f.write("notes/other.txt", "Несвязанная правка.\n")
        f.commit("Независимая запись другого владельца")
        self.action("3 · Чужой коммит не влияет", "handoff", number, "product-chat")
        for path, replacement in ((scanner, 'print("ERROR")\n'),
                                  (description, "Проверить другое поведение сканера.\n")):
            with self.subTest(path=path):
                original = (f.root / path).read_text()
                f.write(path, replacement)
                if path == scanner:
                    scan("ERROR")
                self.action(f"3 · Рабочие байты {path}", "handoff", number, "product-chat", code=1,
                            message=f"рабочие байты существенного файла {path} отличаются")
                f.git("add", "--", path)
                f.write(path, original)
                self.action(f"3 · Только индекс {path}", "handoff", number, "product-chat", code=1,
                            message=f"существенный файл {path} отличается в индексе")
                f.write(path, replacement)
                f.git("commit", "--quiet", "-m", f"Изменён {path}")
                self.action(f"3 · После коммита {path}", "handoff", number, "product-chat", code=1,
                            message=f"существенный файл {path} отличается в HEAD")
                f.write(path, original)
                f.commit(f"Восстановлены байты {path}")
        self.action("3 · Точные прежние байты снова применимы", "handoff", number, "product-chat")
        decide(card, "revoked", "Сканер обновляется, нужна новая проверка.", revokes="A-scanner")
        decide(card, "reopened", "Повторить проверку на новой реализации.")
        f.save_card(number, card)
        f.write(scanner, 'print("B" + "9")\n')
        renewed = f.commit("Новая реализация при прежнем критерии")
        scan("B9")
        self.action("3 · Рассмотрение новой версии", "review", number, "product-chat")
        obs = observation(card, "Новый сканер успешно вывел B9.")
        accept(card, "A-rescan", obs, "Новая проверка на изменившихся байтах.",
               basis=address(renewed, f.card_path(number)))
        f.save_card(number, card)
        self.action("3 · Передача после новой проверки", "handoff", number, "product-chat")
        self.assertEqual(self.show("3 · Первоначальное решение осталось в карточке", number)["card"]["decisions"][0], old)
        self.observe("3 · Приёмка исходной версии читается в Git", "read", "--commit", accepted,
                     "--path", f.card_path(number), message="A-scanner")

    def test_stale_or_preparation_only_mandate_refuses_implementation(self):
        f = self.f
        parent, first, second, outsider, root, a, b, c, ref = self.feature(stage="prepare")
        f.attempt(second, b, "second", "test-implementer", ref, ["write"])
        self.action("С этапом prepare местная реализация запрещена", "assign", second, "feature-chat", "second",
                    code=1, message="только подготовку")
        prep = f.task("F-search/T-013", "Прочитать правила и подготовить оценку.", stage="prepare", inputs=[NORM])
        prep_ref = f.commit("Местная подготовка в пределах действующего мандата")
        f.attempt("F-search/T-013", prep, "prep", "analyst", prep_ref)
        self.action("Подготовка в тех же границах разрешена", "assign", "F-search/T-013", "feature-chat", "prep")
        root["assignment"].update(revision=2, stage="implement", write_scope=["product"])
        f.save_card(parent, root)
        f.grants("F-search", parent, 2, ["write"])
        f.commit("Общее поручение 2 выдано, функция пока не приняла его")
        self.action("Новая общая выдача не оживляет прежний местный мандат", "assign", second, "feature-chat", "second",
                    code=1, message="мандат отличается")

    def test_explicit_local_stages_stay_narrow_until_new_mandate_is_accepted(self):
        f = self.f
        parent, first, second, _, root, _, implement, _, _ = self.feature()
        root["assignment"]["mandate"]["stages"] = ["prepare"]
        f.save_card(parent, root)
        narrow = f.commit("Общая реализация с передачей функции только подготовки")
        f.scope(F_SCOPE, LOCAL, "feature-chat", first, mandate={"task": parent, "basis": address(narrow, f.card_path(parent))})

        plan = "F-search/T-013"
        preparation = f.task(plan, "Подготовить план проверки.", stage="prepare", inputs=[NORM])
        basis = f.commit("Сохранены оба местных поручения")
        f.attempt(plan, preparation, "plan", "analyst", basis)
        f.attempt(second, implement, "change", "implementer", basis, ["write"])
        self.action("Местная подготовка в пределах узкого мандата", "assign", plan, "feature-chat", "plan")
        self.action("Общий implement сам по себе не выдаёт местный implement", "assign", second, "feature-chat",
                    "change", code=1, message=f"Новое решение выдаёт общая роль «{PRODUCT}»")

        root["assignment"]["revision"] = 2
        root["assignment"]["mandate"]["stages"].append("implement")
        f.save_card(parent, root)
        f.grants("F-search", parent, 2, ["write", "git"])
        wider = f.commit("Координатор расширил перечень местных этапов")
        self.action("Функция ещё ссылается на прежнее решение", "assign", second, "feature-chat", "change",
                    code=1, message="мандат отличается")
        f.scope(F_SCOPE, LOCAL, "feature-chat", first, mandate={"task": parent, "basis": address(wider, f.card_path(parent))})
        f.commit("Местный координатор принял новый снимок мандата")
        self.action("После новой выдачи местная реализация разрешена", "assign", second, "feature-chat", "change")

    def test_external_unknown_holds_even_after_end(self):
        f = self.f
        number, other = "P/T-001", "P/T-002"
        ext = "external:release/test"
        a = f.task(number, "Учебная имитация доставки, без внешнего вызова.", resources=[ext])
        b = f.task(other, "Учебная имитация повторного вызова.", resources=[ext])
        f.grants("P", number, 1, [ext], "original")
        ref = f.commit("Учебная исходная выдача резерва")
        run = f.attempt(number, a, "original", "sender", ref, [ext], external_outcome="pending")
        self.action("Сохранённая выдача имитации", "assign", number, "product-chat", "original")
        f.launch(number, a, run)
        run.update(phase="stopped", external_outcome="unknown",
                   termination={"by": PRODUCT, "source": "Процесс остановлен, факт доставки не установлен."})
        f.save_card(number, a)
        f.attempt(other, b, "retry", "sender-two", ref, [ext], external_outcome="pending")
        self.action("Резерв внешнего исхода сохраняется между работами", "assign", other, "product-chat", "retry",
                    code=1, message="P/T-001/original")
        run["external_outcome"] = "not-done"
        f.save_card(number, a)
        f.grants("P", other, 1, [ext], "retry")
        self.action("Новая выдача после установления внешнего исхода", "assign", other, "product-chat", "retry")

    def test_failed_attempt_and_late_response_stay_on_original_run(self):
        f = self.f
        number = "P/T-001"
        card = f.task(number, "Собрать подтверждение наблюдаемого критерия.")
        ref = f.commit("Поручение на проверку")
        old = f.attempt(number, card, "first", "reader-one", ref)
        self.action("Первый тестовый запуск", "assign", number, "product-chat", "first")
        f.launch(number, card, old)
        f.submitted(number, card, old, "Неуспех: нужный факт не получен.", outcome="failed")
        self.action("Неуспех остаётся с исходной попыткой", "review", number, "product-chat", "first",
                    code=1, message="нужен представленный результат")
        new = f.attempt(number, card, "second", "reader-two", ref)
        self.action("Повтор имеет собственное назначение", "assign", number, "product-chat", "second")
        f.launch(number, card, new)
        f.submitted(number, card, new, "Добавлена независимая проверка критерия.")
        f.write("late.md", "Поздний ответ исполнителя первой проверки.\n")
        after = f.commit("Поступил поздний независимый материал")
        observation(card, "Неуспешная первая попытка прислала ранее недоступную справку.",
                    author="reader-one", source=address(after, "late.md"), attempt="first")
        f.save_card(number, card)
        self.action("Поздний ответ не подменил текущую сдачу", "review", number, "product-chat", "first",
                    code=1, message="прежней попытке")
        self.action("Вторая сдача проверяется отдельно", "review", number, "product-chat", "second")

    def test_fix_04_human_result_after_failed_agent_keeps_both_records(self):
        f = self.f
        number, external, research = "P/T-001", "P/T-002", "P/T-003"
        card = f.task(number, "При пустом поиске виден текст «Ничего не найдено».",
                      write_scope=["product/search.py"])
        target = "external:lab/receipt"
        ext = f.task(external, "Проверить доступность учебной квитанции.", resources=[target])
        f.task(research, "Доказать применимость вывода исследования.", research_review=True)
        f.grants("P", number, 1, ["write"], "old")
        order = f.commit("Поручение поиска, внешняя проверка и исследовательский контроль")
        old = f.attempt(number, card, "old", "worker", order, ["write"])
        self.action("4 · Прежний исполнитель назначен", "assign", number, "product-chat", "old")
        f.launch(number, card, old)
        process = subprocess.Popen([sys.executable, "-B", "-c", "import sys; sys.stdin.read(); sys.exit(3)"],
                                   cwd=f.root, stdin=subprocess.PIPE)
        try:
            old["phase"] = "running"
            f.save_card(number, card)
            self.assertIsNone(process.poll())
            self.action("4 · До завершения исполнителя прямое рассмотрение ждёт", "review", number, "product-chat",
                        code=1, message=f"{number}/old: running")
            self.assertIsNone(process.poll())
        finally:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 3)
        f.submitted(number, card, old, "Команда учебного агента завершилась кодом 3, результата по критерию нет.",
                    outcome="failed")
        f.grants("P", number, 1, [])
        failed = f.commit("Неуспех зафиксирован после выхода команды, право записи освобождено")

        output = f.local_search()
        self.assertEqual(output, "Ничего не найдено")
        path = "messages/after-failed.txt"
        f.write(path, f"Я выполнил пустой поиск в учебной копии и увидел «{output}».\n")
        verified = f.commit("Самостоятельное наблюдение владельца после завершения агента")
        self.assertEqual(f.git("rev-parse", verified + "^"), failed)
        self.assertFalse(f.git("ls-tree", failed, "--", path))
        card["waiting"] = {"remaining": "Установить видимый текст.",
                           "reason": "Сообщение «я поменял строку» само по себе неполно.",
                           "next_step": "Сверить вывод версии, проверенной пользователем."}
        f.save_card(number, card)
        self.action("4 · Одного сообщения о правке мало для передачи", "handoff", number, "product-chat",
                    code=1, message="decisions")
        self.assertEqual(self.show("4 · Неполное наблюдение оставляет видимый остаток", number)["expected_location"],
                         "open")
        card.pop("waiting")
        obs = observation(card, f"После пустого поиска пользователь увидел «{output}».",
                          source=address(verified, path))
        f.save_card(number, card)
        self.action("4 · Самостоятельный результат той же редакции рассматривается", "review", number, "product-chat")
        self.action("4 · Неуспешная сдача осталась неуспехом", "review", number, "product-chat", "old",
                    code=1, message="нужен представленный результат")
        accept(card, "A-user", obs, "Результат выполнил пользователь после окончания запуска.",
               basis=address(verified, f.card_path(number)))
        f.save_card(number, card)
        self.action("4 · Самостоятельная приёмка передаётся", "handoff", number, "product-chat")
        row = self.show("4 · Карточка читается с двумя отдельными основаниями", number)
        self.assertEqual(row["expected_location"], "done")
        self.assertEqual(row["card"]["attempts"][0]["result"]["outcome"], "failed")
        self.assertNotIn("attempt", row["card"]["decisions"][-1])
        self.assertEqual(row["card"]["observations"][-1]["source"], address(verified, path))

        f.grants("P", external, 1, [target], "send")
        send = f.attempt(external, ext, "send", "agent", order, [target], external_outcome="pending")
        f.launch(external, ext, send)
        send.update(phase="stopped", external_outcome="unknown",
                    termination={"by": PRODUCT, "source": "Команда закончилась, исход учебной доставки неизвестен."})
        f.save_card(external, ext)
        self.action("4 · Неизвестный внешний исход удерживает прямое рассмотрение", "review", external,
                    "product-chat", code=1, message=f"{external}/send")
        send["external_outcome"] = "not-done"
        f.save_card(external, ext)
        self.action("4 · После выяснения внешнего исхода рассмотрение доступно", "review", external, "product-chat")
        self.action("4 · Обязательное независимое ревью нельзя обойти", "review", research, "product-chat",
                    code=1, message="для приёмки исследования укажите --attempt")

    def test_research_review_dependency_drift_and_revocation(self):
        f = self.f
        research, consumer = "P/T-001", "P/T-002"
        card = f.task(research, "Обосновать однозначность правила пустого списка.", research_review=True, inputs=[NORM])
        f.write("evidence.txt", "Запуск zz-no-results даёт пустой список в учебном источнике.\n")
        f.write("review.md", "Решающее свидетельство: evidence.txt.\n")
        ref = f.commit("Вопрос, предметная норма и исходное свидетельство")
        run = f.attempt(research, card, "study", "researcher", ref)
        self.action("Назначена проверка предметного источника", "assign", research, "product-chat", "study")
        f.launch(research, card, run)
        f.submitted(research, card, run, "Заключение исследования: правило имеет однозначное условие.")
        self.action("Без заключения принимающего недостаточно основания", "review", research, "product-chat",
                    "study", code=1, message="отсутствует заключение")
        report = {"attempt": "study", "revision": 1, "by": "Независимый проверяющий",
                  "result": run["result"]["source"],
                  "source": address(ref, "review.md"), "decisive_evidence": [],
                  "open_required": ["R-1: сопоставить источник с выводом"]}
        card["research_reviews"] = [report]
        f.save_card(research, card)
        self.action("Обязательное замечание удерживает решение", "review", research, "product-chat",
                    "study", code=1, message="R-1")
        f.write("review-after.md", "R-1 закрыто. Пример проверен, решающее свидетельство в evidence.txt.\n")
        review_ref = f.commit("Проверяющий закрыл замечание по точной редакции")
        final_review = dict(report, source=address(review_ref, "review-after.md"), open_required=[])
        card["research_reviews"].append(final_review)
        f.save_card(research, card)
        self.action("Даже после закрытия замечания нужен указатель на решающее свидетельство",
                    "review", research, "product-chat", "study", code=1, message="решающее свидетельство")
        final_review["decisive_evidence"] = [address(ref, "evidence.txt")]
        final_review["result"] = address(ref, "evidence.txt")
        f.save_card(research, card)
        self.action("Заключение по другой редакции отчёта нельзя использовать",
                    "review", research, "product-chat", "study", code=1, message="другой редакции отчёта")
        final_review["result"] = run["result"]["source"]
        f.save_card(research, card)
        self.action("Заключение и решающий источник проверены", "review", research, "product-chat", "study")
        obs = observation(card, "Норма по этому адресу однозначно требует пустого списка.",
                          author="researcher", source=run["result"]["source"], attempt="study")
        accept(card, "A-study", obs, "Вывод покрыт решающим наблюдением, R-1 закрыто.", attempt="study")
        f.save_card(research, card)
        self.action("Принятое исследование передано", "handoff", research, "product-chat", "study")
        accepted = f.commit("Точная приёмка исследования")
        dependent = f.task(consumer, "Продолжить проверку экрана на основе нормы о пустом списке.")
        link = {"task": research, "criterion": "C-1", "acceptance": "A-study",
                "basis": address(accepted, f.card_path(research))}
        dependent["dependencies"] = [link]
        f.save_card(consumer, dependent)
        order = f.commit("Зависимое поручение с выбранной приёмкой")
        f.attempt(consumer, dependent, "use-1", "consumer", order)
        self.action("Выбранное обязательство применимо", "assign", consumer, "product-chat", "use-1")
        f.write(NORM, "# Правила поиска\n\nЗапрос без совпадений возвращает пустой список.\n")
        current = f.commit("Редакционный заголовок предметной нормы")
        self.action("Зависимость ждёт проверки изменённого входа", "assign", consumer, "product-chat",
                    "use-1", code=1, message=NORM)
        link["compatibility"] = {"by": PRODUCT, "commit": current,
                                 "reason": "Изменён только заголовок, условие пустого списка осталось прежним."}
        f.save_card(consumer, dependent)
        basis = f.commit("Основание совместимости для конкретной зависимости")
        f.attempt(consumer, dependent, "use-2", "consumer", basis)
        self.action("Редактор установил совместимость точных байтов", "assign", consumer, "product-chat", "use-2")
        decide(card, "revoked", "Исследование не покрывало дополнительный тип запроса.",
               revokes="A-study")
        f.save_card(research, card)
        self.action("Отзыв приёмки нельзя покрыть совместимостью", "assign", consumer, "product-chat",
                    "use-2", code=1, message="действующая выбранная приёмка")
        decide(card, "reopened", "Вернуть исследование для расширения проверки.")
        f.save_card(research, card)
        outcomes = self.show("Именованный отзыв и повторное открытие сохранились", research)["card"]["decisions"]
        self.assertEqual([d["outcome"] for d in outcomes], ["accepted", "revoked", "reopened"])
        self.assertEqual(outcomes[1]["revokes"], "A-study")

    def test_fix_05_dependency_checks_live_and_historical_acceptance_sources(self):
        f = self.f
        supplier, consumer = "P/T-001", "P/T-002"
        card = f.task(supplier, "На пустом поиске виден точный текст.")
        dependent = f.task(consumer, "Использовать принятое обязательство в справке.")
        folder = f.card_path(supplier).removesuffix("task.json")
        for file, data in (("receipt.txt", "Наблюдение: после пустого поиска появилось сообщение.\n"),
                           ("signoff.txt", "Основание: пользователь проверил искомый экран.\n")):
            f.write(folder + file, data)
        checked = f.commit("Проверенная версия и сохранённые свидетельства")
        basis = address(checked, f.card_path(supplier))

        def select(key, run_key, previous=None):
            if previous:
                previous.update(phase="stopped", termination={"by": PRODUCT,
                                "source": "Учебная проверка назначения закончена, исполнитель не запускался."})
            f.save_card(supplier, card)
            signed = f.commit(f"Сохранено решение {key}")
            dependent["dependencies"] = [{"task": supplier, "criterion": "C-1", "acceptance": key,
                                          "basis": address(signed, f.card_path(supplier))}]
            f.save_card(consumer, dependent)
            order = f.commit(f"Выбрано основание {key}")
            return f.attempt(consumer, dependent, run_key, "reader", order)

        obs = observation(card, "Пользователь видел сообщение", source={"task": supplier, "path": "receipt.txt"})
        accept(card, "A-live", obs, "Сверен видимый текст по свидетельству.",
               basis=basis, source={"task": supplier, "path": "signoff.txt"})
        launch = select("A-live", "use-live")
        self.action("5 · Доступное основание поставщика", "handoff", supplier, "product-chat")
        self.action("5 · Потребитель может назначить чтение", "assign", consumer, "product-chat", "use-live")
        f.launch(consumer, dependent, launch)
        self.action("5 · Потребитель может начать с доступным основанием", "execute", consumer, "reader", "use-live")
        for path, owner in (("receipt.txt", f"observations/{obs}"), ("signoff.txt", "decision/source")):
            file = f.root / folder / path
            content = file.read_bytes()
            file.unlink()
            location = f"{supplier}:{path}"
            self.action(f"5 · Исходная передача без {path}", "handoff", supplier, "product-chat",
                        code=1, message=f"{owner}: свидетельство {location} недоступно")
            for kind, actor in (("assign", "product-chat"), ("execute", "reader")):
                self.action(f"5 · {kind} ждёт {path}", kind, consumer, actor, "use-live", code=1,
                            message=f"{consumer}/dependencies/{supplier}/C-1/{owner}: свидетельство {location}")
            f.write(folder + path, content)
            self.action(f"5 · После возврата {path} работа продолжается", "execute", consumer, "reader", "use-live")

        decide(card, "revoked", "Нужны фиксированные адреса свидетельств.", revokes="A-live")
        decide(card, "reopened", "Перепроверить и сохранить историческое основание.")
        missing = folder + "receipt-typo.txt"
        obs = observation(card, "Пользователь подтвердил текст, в адресе допущена опечатка.",
                          source=address(checked, missing))
        accept(card, "A-typo", obs, "Учебное решение с ошибочной ссылкой на историю.",
               basis=basis, source=address(checked, folder + "signoff.txt"))
        wrong = select("A-typo", "use-typo", launch)
        f.write(missing, "Файл с таким именем появился только сейчас.\n")
        diagnostic = f"{consumer}/dependencies/{supplier}/C-1/observations/{obs}: свидетельство {checked}:{missing}"
        self.action("5 · Ошибка адреса в коммите останавливает поставщика", "handoff", supplier, "product-chat",
                    code=1, message=f"{checked}:{missing} недоступно")
        self.action("5 · Неверный Git-адрес останавливает назначение", "assign", consumer, "product-chat", "use-typo",
                    code=1, message=diagnostic)
        f.launch(consumer, dependent, wrong)
        self.action("5 · Неверный Git-адрес останавливает и начало", "execute", consumer, "reader", "use-typo",
                    code=1, message=diagnostic)

        decide(card, "revoked", "Опечатка: свидетельства по выбранному адресу нет.", revokes="A-typo")
        decide(card, "reopened", "Сверить исходное наблюдение в сохранённом коммите.")
        f.save_card(supplier, card)
        self.action("5 · Рассмотрено исправленное основание", "review", supplier, "product-chat")
        self.observe("5 · Исторический материал открыт из точного коммита", "read", "--commit", checked,
                     "--path", folder + "receipt.txt", message="Наблюдение")
        obs = observation(card, "По записи исходной проверки подтверждён видимый текст.",
                          source=address(checked, folder + "receipt.txt"))
        accept(card, "A-history", obs, "Проверены точные адреса наблюдения и основания решения в Git.",
               basis=basis, source=address(checked, folder + "signoff.txt"))
        correct = select("A-history", "use-history", wrong)
        for path in ("receipt.txt", "signoff.txt"):
            (f.root / folder / path).unlink()
        self.action("5 · Передача по истории без рабочих копий свидетельств", "handoff", supplier, "product-chat")
        self.action("5 · Пересмотренное основание разрешает назначение", "assign", consumer, "product-chat",
                    "use-history")
        f.launch(consumer, dependent, correct)
        self.action("5 · Исторические байты доступны при начале работы", "execute", consumer, "reader", "use-history")

    def test_product_integration_and_transfer_of_open_obligation(self):
        f = self.f
        parent, first, second, outsider, root, a, b, c, ref = self.finish_first()
        accepted = f.commit("Доступная общему координатору местная приёмка")
        integration = "P/T-002"
        check = f.task(integration, "Пустой результат поиска отображается на общем экране.")
        check["dependencies"] = [{"task": first, "criterion": "C-1", "acceptance": "A-first",
                                  "basis": address(accepted, f.card_path(first))}]
        f.save_card(integration, check)
        f.scope(P_SCOPE, PRODUCT, "product-successor", integration, resources=BOOK)
        checked = f.commit("Общая задача интеграции и преемник")
        self.action("Местная роль не принимает общую интеграцию", "review", integration, "feature-chat",
                    code=1, message="product-successor")
        self.action("Общий редактор проверяет взаимодействие частей", "review", integration, "product-successor")
        obs = observation(check, f"Учебная проверка общего экрана: {f.local_search()}.",
                          author=PRODUCT, source="Команда пустого поиска в учебном экране")
        accept(check, "A-integration", obs, "Интеграция проверена отдельно по принятому местному вкладу.",
               basis=address(checked, f.card_path(integration)))
        f.save_card(integration, check)
        self.action("Общая интеграция передана после собственной приёмки", "handoff", integration, "product-successor")
        new = "P/T-003"
        follow = f.task(new, "Проверить расширенные запросы отдельно от принятого вклада.")
        follow["waiting"] = {"remaining": "Объединить данные для проверки расширенного запроса.",
                             "reason": "Передано из F-search/T-012, исходных данных ещё нет.",
                             "next_step": "Координатор продукта собирает пример запроса и ожидаемый ответ."}
        f.save_card(new, follow)
        decide(b, "replaced", "Ответственный за продолжение — координатор продукта.", replacement=new)
        f.save_card(second, b)
        f.scope(P_SCOPE, PRODUCT, "product-successor", new, resources=BOOK)
        f.grants("P", parent, 1, [])  # All local launches have a recorded outcome.
        self.assertEqual(self.show("Местный остаток передан на P/T-003", second)["expected_location"], "closed")
        self.assertIn("собирает пример", self.show("Владелец и следующий шаг переданного обязательства", new)
                      ["card"]["waiting"]["next_step"])

    def test_fix_06_historical_decision_must_belong_to_its_own_card(self):
        f = self.f
        number, other, pinned = "P/T-001", "F-search/T-001", "P/T-002"
        card = f.task(number, "Архивная проверка пустой выдачи.")
        foreign = f.task(other, "Архивная проверка пустой выдачи.")
        foreign["assignment"]["acceptor"] = PRODUCT
        actual = f.task(pinned, "Архивная проверка с зафиксированной проверенной версией.")
        for n, item in ((number, card), (other, foreign), (pinned, actual)):
            observation(item, "Пустой поиск прежде показал подсказку.", source="Архивное наблюдение")
            item["observations"][0]["date"] = "2026-11-19"
            f.save_card(n, item)
        f.write(F_SCOPE, "# Архив учебной области\n")
        f.save_json("observation.json", card["observations"][0])
        checked = f.commit("Два одинаковых местных ключа наблюдения, приёмок ещё нет")
        restored = accept(card, "A-archive", "O-1", "Прежняя запись принимающего.", date="2026-11-19")
        saved_receipt = f.card_path(number).removesuffix("task.json") + "original-decision.json"
        f.save_json(saved_receipt, restored)
        accepted = accept(actual, "A-archive", "O-1", "Прежняя запись принимающего.", date="2026-11-19",
                          basis=address(checked, f.card_path(pinned)))
        f.save_card(number, card)
        f.save_card(pinned, actual)
        old = f.commit("Оригинальные решения в карточках и один старый отдельный JSON")
        restored["restored_from"] = address(old, "observation.json")
        f.save_card(number, card)
        self.observe("6 · Одно наблюдение не подтверждает старую дату", "check", "--profile", PROFILE,
                     code=1, message="restored_from")
        restored["restored_from"] = address(old, saved_receipt)
        f.save_card(number, card)
        self.observe("6 · Равенство полей отдельного JSON не устанавливает принадлежность", "check", "--profile",
                     PROFILE, code=1, message="снимок должен указывать на эту карточку в её области")
        restored["restored_from"] = address(old, f.card_path(number))
        f.save_card(number, card)
        self.observe("6 · Своё решение восстанавливается по истории", "check", "--profile", PROFILE,
                     message="формальных противоречий не найдено")
        self.action("6 · Старое решение читается, передача требует установленной версии", "handoff", number,
                    "product-chat", code=1, message="/basis")
        foreign["decisions"] = [dict(restored)]
        f.save_card(other, foreign)
        self.observe("6 · Чужая область с тем же локальным номером и теми же полями отклонена",
                     "check", "--profile", PROFILE, code=1,
                     message=f"{other}/restored_from: снимок должен указывать на эту карточку в её области")
        foreign["decisions"][0]["restored_from"] = address(old, f.card_path(other))
        f.save_card(other, foreign)
        self.observe("6 · Историческая карточка без решения тоже не подтверждает дату", "check", "--profile",
                     PROFILE, code=1, message="однозначной записи решения")
        foreign.pop("decisions")
        f.save_card(other, foreign)

        accepted["restored_from"] = address(old, f.card_path(pinned))
        f.save_card(pinned, actual)
        self.action("6 · Восстановленная приёмка с точной версией передаётся", "handoff", pinned, "product-chat")
        actual["observations"][0]["fact"] = "С тем же номером записана другая проверка."
        f.save_card(pinned, actual)
        self.action("6 · Совпавшего ключа при изменённом наблюдении мало", "handoff", pinned, "product-chat",
                    code=1, message="наблюдения отличаются от оснований прежнего решения")
        actual["observations"][0]["fact"] = "Пустой поиск прежде показал подсказку."
        f.save_card(pinned, actual)

        decide(card, "reopened", "Проверить применимость старого наблюдения к нынешней версии.")
        f.save_card(number, card)
        self.assertEqual(f.local_search(), "Ничего не найдено")
        self.action("6 · По прежнему наблюдению выполнена новая проверка", "review", number, "product-chat")
        accept(card, "A-today", "O-1", "Принимающий сверил результат сегодня.", date="2026-11-23",
               basis=address(checked, f.card_path(number)))
        f.save_card(number, card)
        self.action("6 · Новое решение имеет дату самой приёмки", "handoff", number, "product-chat")
        self.assertEqual(self.show("6 · Историческая и новая даты различимы", number)["card"]["decisions"][-1]["date"],
                         "2026-11-23")

    def test_fix_07_installed_reassessment_route_keeps_card_waiting_and_neighbour_readable(self):
        f = self.f
        profile, entrance = (f.root / "AGENTS.md").read_text().split("`")[1::2]
        self.assertEqual(profile, PROFILE)
        self.assertIn('"format_version":"0.3.0"', (f.root / profile).read_text())
        readme, entry = entrance.split("#")

        def section(file, anchor):
            return file.read_text().split(f'<a id="{anchor}"></a>', 1)[1].split('<a id="', 1)[0]

        readme = f.root / readme
        opened = section(readme, entry)
        self.assertIn("procedures/workflow.md#card-workflow", opened)
        workflow = readme.parent / "procedures/workflow.md"
        self.assertIn("#card-review", section(workflow, "review-and-acceptance"))
        review = section(workflow, "card-review")
        reference = re.search(r"\]\(([^)]+#card-reassess)\)", review)
        self.assertIsNotNone(reference)
        document, anchor = reference[1].split("#")
        recovery = workflow.parent / document
        steps = section(recovery, anchor)
        for word in ("revoked", "revokes", "tasks", "waiting", "remaining", "reason", "next_step", "check --action"):
            self.assertIn(f"`{word}`", steps)
        for word in ("revision_review", "blocker", "inspect"):
            self.assertNotIn(f"`{word}`", steps)
            self.assertIn(f"`{word}`", section(recovery, "reassess"))
        if EVIDENCE:
            OBSERVATIONS.append({"case": self._testMethodName, "installed_entry": entrance,
                                 "acceptance": ".ai-kit/procedures/workflow.md#card-review",
                                 "followed_link": reference[1], "installed_steps": steps.strip()})

        supplier, consumer, neighbour = "P/T-001", "P/T-002", "P/T-003"
        source = f.task(supplier, "Проверить исходную формулировку.")
        dependent = f.task(consumer, "Справка зависит от исходной формулировки.")
        f.task(neighbour, "Независимая работа открывается по своему номеру.")
        checked = f.commit("Действующие постановки")
        obs = observation(source, "На проверенной версии формулировка совпала.")
        initial = accept(source, "A-wording", obs, "Исходный текст проверен.",
                         basis=address(checked, f.card_path(supplier)))
        f.save_card(supplier, source)
        signed = f.commit("Действующая приёмка формулировки")
        dependent["dependencies"] = [{"task": supplier, "criterion": "C-1", "acceptance": "A-wording",
                                      "basis": address(signed, f.card_path(supplier))}]
        f.save_card(consumer, dependent)
        order = f.commit("Справка опирается на точную приёмку")
        run = f.attempt(consumer, dependent, "copy", "reader", order)
        f.launch(consumer, dependent, run)
        self.action("7 · До отзыва зависимое действие проходит", "execute", consumer, "reader", "copy")
        decide(source, "revoked", "Целевой экран изменился.", revokes="A-wording")
        f.save_card(supplier, source)
        self.action("7 · Отзыв прекращает действие даже до записи ожидания", "execute", consumer, "reader", "copy",
                    code=1, message="нужна действующая выбранная приёмка")
        dependent["waiting"] = {"remaining": "Установить основание для справки.",
                                "reason": "Отозвана A-wording обязательства C-1 задачи P/T-001.",
                                "next_step": "Координатор проверяет изменённый экран и сохраняет новую приёмку."}
        f.save_card(consumer, dependent)
        self.observe("7 · Структура с ожиданием по карточке допустима", "check", "--profile", PROFILE)
        self.assertEqual(self.show("7 · Причина и шаг видны в целевой карточке", consumer)["card"]["waiting"],
                         dependent["waiting"])
        self.assertEqual(self.show("7 · Прежнее решение и отзыв сохранены", supplier)["card"]["decisions"][0], initial)
        self.action("7 · Преемник видит ближайший шаг при отказе", "execute", consumer, "reader", "copy",
                    code=1, message=dependent["waiting"]["next_step"])
        self.show("7 · Соседняя независимая задача открывается", neighbour)
        dependent["blocker"] = {"reason": "Поле прежнего реестра", "next_step": "Учебная проверка"}
        f.save_card(consumer, dependent)
        self.observe("7 · Чужое поле прежнего реестра всё ещё даёт отказ", "check", "--profile", PROFILE,
                     code=2, message="неизвестные поля: blocker")
        dependent.pop("blocker")
        f.save_card(consumer, dependent)
        self.checkpoint("review-card-reassessment")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    EVIDENCE = args.evidence
    if EVIDENCE:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TaskActionTests)
    results = unittest.TextTestRunner(verbosity=2).run(suite)
    if EVIDENCE:
        (EVIDENCE / "observations.json").write_text(
            json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if results.wasSuccessful() else 1)
