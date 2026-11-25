"""Regression checks through the installed migration command on local Git copies."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_kit import environment, fingerprint
from test_task_actions import Project


DECISION = "records/decision.json"
CARD = "preview/tasks/open/T-001/task.json"


class MigrationFixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kit-migration-fixes-")
        self.addCleanup(temporary.cleanup)
        self.lab = Path(temporary.name).resolve()
        self.project = Project(self.lab / "Original")
        self.origin = self.project.root
        self.copy = self.lab / "Candidate"
        self.review = self.lab / "review.json"
        self.snapshot = self.lab / "snapshot.json"

    def migration(self, phase, code=0, *, root=None, origin=None, bindings=None, actor="case"):
        root = root or (self.origin if phase == "inventory" else self.copy)
        args = ["--root", str(root), "--review", str(self.review)]
        if phase != "inventory":
            args += ["--origin", str(origin or self.origin), "--target", "preview",
                     "--snapshot", str(self.snapshot)]
            if bindings:
                args += ["--bindings", str(bindings)]
        if phase == "switch":
            args += ["--actor", actor, "--pause", "ops/pause.json"]
        result = subprocess.run([sys.executable, "-B", str(self.origin / ".ai-kit/tools/kit.py"),
                                 "migration", phase, *args], cwd=self.lab, env=environment(),
                                capture_output=True, text=True, check=False, timeout=40)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        if phase == "inventory" and code == 0:
            self.snapshot.write_text(result.stdout, encoding="utf-8")
        return json.loads(result.stdout) if result.stdout else result.stderr

    def partial(self, accepted=False, historic_only=False):
        p = self.project
        p.write("ops/legacy.md", "Список работ в lists/source.txt, приёмка в records/decision.json.\n")
        p.write("lists/source.txt", "One,Done\n")
        observed_at, decided_at = "2026-11-14T09:00:00+03:00", "2026-11-14T10:00:00+03:00"
        p.write("records/witness.txt", "Учебное наблюдение открытия копии.\n")
        observation = {"commit": p.commit("Список и свидетельство"), "path": "records/witness.txt"}
        decision = {"id": "Turn-approved", "work": "One", "by": "Координатор", "role": "Координатор",
                    "observed_at": observed_at, "decided_at": decided_at, "criteria": ["C-1"],
                    "fact": "Файл открылся.", "evidence": observation}
        p.save_json(DECISION, decision)
        decided = p.commit("Решение о приёмке")
        if historic_only:
            p.git("rm", "--", DECISION)
            p.commit("Прежнее решение доступно по коммиту")
        work = {"new": "P/T-001", "owner": "Координатор", "meaning": "Автор поставил Done.",
                "next_step": "Читать основание.",
                "grounds": [{"basis": "Критерий", "status": "applicable", "reason": "Сверен.",
                             "next_step": "Читать основание."}]}
        if accepted:
            work["history"] = {"decision": {"commit": decided, "path": DECISION}, "evidence": observation}
            work["accept"] = {"decision": "decision", "evidence": "evidence",
                              "observed_at": observed_at, "decided_at": decided_at,
                              "current_decision": None if historic_only else DECISION}
        else:
            work["waiting"] = {"remaining": "Сверить открытие.", "reason": "Нужна проверка.",
                               "next_step": work["next_step"]}
        review = {
            "entry": ["AGENTS.md"], "profile": "ops/legacy.md", "editor": "Координатор",
            "source": {"kind": "partial", "path": "lists/source.txt",
                       "diagnostic": {"cwd": ".", "command": "cat lists/source.txt", "scope": "Список"}},
            "scopes": {"P": {"source": "lists/source.txt", "continuation": "One",
                             "editor": {"role": "Координатор", "session": "case"},
                             "next_step": "Проверить остаток."}},
            "rows": [{"id": "One", "line": 1, "text": "One,Done", "status": "Done", "by": "Автор",
                      "date": "2026-11-14", "title": "Копия", "goal": "Открытие копии",
                      "criteria": [{"id": "C-1", "expectation": "Файл открывается."}]}],
            "works": {"One": work},
        }
        self.review.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        shutil.copytree(self.origin, self.copy)
        return self.migration("inventory")

    def test_index_conflict_inside_candidate_blocks_both_operations(self):
        self.partial()
        self.assertEqual(self.migration("prepare")["preparation"], "created")
        self.assertEqual(self.migration("verify")["status"], "ready")
        git = ["git", "-c", "core.hooksPath=/dev/null"]
        blob = subprocess.check_output([*git, "hash-object", "-w", "--", CARD],
                                       cwd=self.copy, env=environment(), text=True).strip()
        record = "".join(f"100644 {blob} {stage}\t{CARD}\n" for stage in (1, 2, 3))
        subprocess.run([*git, "update-index", "--index-info"], input=record, text=True,
                       cwd=self.copy, env=environment(), capture_output=True, check=True)
        before = fingerprint(self.copy)
        for phase in ("verify", "prepare"):
            result = self.migration(phase, code=1)
            self.assertEqual(result["status"], "blocked")
            issue, = result["exceptions"]
            self.assertEqual((issue["path"], issue["stages"]), (CARD, ["1", "2", "3"]))
            self.assertIn("Владелец Git", issue["next_step"])
        self.assertEqual(fingerprint(self.copy), before)
        subprocess.check_call([*git, "add", "--", CARD], cwd=self.copy, env=environment())
        self.assertEqual(self.migration("verify")["status"], "ready")
        self.assertEqual(self.migration("prepare")["preparation"], "identical")

    def test_physical_root_guard_precedes_any_candidate_processing(self):
        self.partial()
        inside = self.origin / "Inside"
        inside.mkdir()
        before = fingerprint(self.origin)
        for phase in ("verify", "prepare"):
            with self.subTest(phase=phase):
                self.assertIn("один физический каталог", self.migration(phase, 1, root=self.origin))
                alias = self.origin.with_name("original")
                if alias.exists() and alias.samefile(self.origin):
                    self.assertIn("один физический каталог", self.migration(phase, 1, root=alias))
                for origin, copy in ((self.origin, inside), (inside, self.origin)):
                    self.assertIn("вложены", self.migration(phase, 1, root=copy, origin=origin))
        self.assertEqual(fingerprint(self.origin), before)
        self.assertEqual(self.migration("prepare")["preparation"], "created")
        self.assertEqual(self.migration("verify")["status"], "ready")

    def test_working_revocation_is_detected_on_either_side_and_on_fresh_preparation(self):
        frozen = self.partial(accepted=True)
        self.assertIn(DECISION, frozen["git"]["files"])
        self.assertEqual(self.migration("prepare")["preparation"], "created")
        for side, root in (("origin", self.origin), ("copy", self.copy)):
            with self.subTest(side=side):
                target = root / DECISION
                old = target.read_bytes()
                target.write_text(json.dumps({**json.loads(old), "outcome": "revoked"}), encoding="utf-8")
                result = self.migration("verify", code=1)
                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["changes"][side], [{"path": DECISION, "layers": ["work"]}])
                target.write_bytes(old)
        for root in (self.origin, self.copy):
            (root / DECISION).write_text('{"outcome":"revoked"}\n', encoding="utf-8")
        self.migration("inventory")
        refused = self.migration("prepare", code=1)
        self.assertIn(DECISION, refused)
        self.assertIn("изменение или отзыв", refused)

    def test_historical_only_acceptance_requires_no_current_copy(self):
        frozen = self.partial(accepted=True, historic_only=True)
        self.assertNotIn(DECISION, frozen["git"]["files"])
        self.assertEqual(self.migration("prepare")["preparation"], "created")
        card = json.loads((self.copy / CARD.replace("/open/", "/done/")).read_bytes())
        self.assertEqual(card["decisions"][0]["source"]["path"], DECISION)

    def paused_candidate(self):
        self.partial()
        self.assertEqual(self.migration("prepare")["preparation"], "created")
        record = {"state": "paused", "profile": "ops/legacy.md", "target": "preview", "actor": "case",
                  "owners": "Координатор", "attempts": "Запусков нет.", "external": "Внешних действий нет.",
                  "reserves": "Резервов нет.", "index": "Индекс сверил владелец.",
                  "next_step": "Сохранить профиль и проверить применимость поручений."}
        (self.copy / "ops/pause.json").write_text(json.dumps(record), encoding="utf-8")

    def check_profile(self, code=0):
        command = [sys.executable, "-B", str(self.origin / ".ai-kit/tools/kit.py"),
                   "check", "--root", str(self.copy), "--profile", "ops/legacy.md"]
        result = subprocess.run(command, cwd=self.lab, env=environment(), capture_output=True,
                                text=True, check=False, timeout=40)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result.stdout

    def update_pause(self, **changes):
        path = self.copy / "ops/pause.json"
        value = json.loads(path.read_bytes()) | changes
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def check_transition_refusal(self, actor, *fragments):
        before = fingerprint(self.copy)
        check = self.check_profile(1)
        repeat = self.migration("switch", 1, actor=actor)
        error, = [line for line in check.splitlines() if line.startswith("ERROR: ")]
        self.assertEqual(error, repeat.strip())
        for fragment in fragments:
            self.assertIn(fragment, error)
        self.assertEqual(fingerprint(self.copy), before)
        return error

    def test_switch_inserts_one_selector_and_retry_preserves_other_files(self):
        self.paused_candidate()
        previous = fingerprint(self.copy)
        original = fingerprint(self.origin)
        self.assertIn("actor", self.migration("switch", 1, actor="other"))
        self.assertEqual(fingerprint(self.copy), previous)
        self.assertEqual(self.migration("switch")["status"], "selected")
        changed = fingerprint(self.copy)
        self.assertEqual({path for path in previous if previous[path] != changed[path]},
                         {"ops/legacy.md"})
        self.assertEqual(changed.keys(), previous.keys())
        self.assertEqual(self.migration("switch")["status"], "already-selected")
        self.assertEqual(fingerprint(self.copy), changed)
        self.assertEqual(fingerprint(self.origin), original)

    def test_switch_refuses_changed_legacy_work_and_keeps_staged_bytes(self):
        self.paused_candidate()
        legacy_path = self.copy / "lists/source.txt"
        legacy_path.write_text("One,Changed\n", encoding="utf-8")
        (self.copy / "notes.md").write_text("Чужие байты в индексе.\n", encoding="utf-8")
        subprocess.check_call(["git", "add", "--", "notes.md"], cwd=self.copy, env=environment())
        previous = fingerprint(self.copy)
        result = self.migration("switch", 1)
        self.assertEqual(result["status"], "stale")
        reported = {row["path"]: row["layers"] for row in result["changes"]["copy"]}
        self.assertEqual(reported["lists/source.txt"], ["work"])
        self.assertIn("index", reported["notes.md"])
        self.assertEqual(fingerprint(self.copy), previous)

    def test_active_resume_uses_the_current_common_editor(self):
        self.paused_candidate()
        self.assertEqual(self.migration("switch")["status"], "selected")
        self.update_pause(state="reviewed", actor="local", evidence=["Проверки пройдены."])
        for caller in ("case", "local"):
            self.check_transition_refusal(caller, "возобновлении выдачи", "preview/tasks",
                                          "ops/pause.json", "«Координатор» (case)")

        self.update_pause(actor="case")
        before = fingerprint(self.copy)
        self.assertIn("Выдача проверенных работ возобновлена", self.check_profile())
        self.assertEqual(self.migration("switch")["issuance"], "reviewed")
        self.assertIn("«Координатор» (case)", self.migration("switch", 1, actor="local"))
        self.assertEqual(fingerprint(self.copy), before)

        scope = self.copy / "preview/tasks/scope.md"
        text = scope.read_text(encoding="utf-8")
        self.assertIn('"session": "case"', text)
        scope.write_text(text.replace('"session": "case"', '"session": "successor"'), encoding="utf-8")
        self.check_transition_refusal("successor", "«Координатор» (successor)")
        self.update_pause(state="paused")
        self.assertIn("Выдача приостановлена", self.check_profile())
        self.assertEqual(self.migration("switch", actor="successor")["issuance"], "paused")
        self.update_pause(state="reviewed", actor="successor")
        before = fingerprint(self.copy)
        self.assertIn("Координатор (successor)", self.check_profile())
        self.assertEqual(self.migration("switch", actor="successor")["issuance"], "reviewed")
        self.assertIn("(successor)", self.migration("switch", 1))
        self.assertEqual(fingerprint(self.copy), before)

    def test_active_cards_reject_a_late_stop_in_both_commands(self):
        self.paused_candidate()
        self.assertEqual(self.migration("switch")["status"], "selected")
        before = fingerprint(self.copy)
        repeat = self.migration("switch")
        self.assertEqual(repeat["issuance"], "paused")
        self.assertIn(repeat["next_step"], self.check_profile())
        self.assertEqual(fingerprint(self.copy), before)
        false_step = "Работать по lists/source.txt."
        self.update_pause(state="stopped", next_step=false_step)
        for caller in ("case", "local"):
            error = self.check_transition_refusal(caller, "preview/tasks", "ops/pause.json",
                                                   "«Координатор» (case)", "сверяет запись перехода")
            self.assertNotIn(false_step, error)

    def test_stopped_legacy_source_keeps_the_authorized_continuation(self):
        self.paused_candidate()
        profile = self.copy / "ops/legacy.md"
        state = json.loads((self.origin / ".ai-kit/templates/state.json").read_bytes())
        state.update(profile="ops/legacy.md", editor={"role": "Координатор", "session": "case", "status": "active"})
        (self.copy / "ops/state.json").write_text(json.dumps(state), encoding="utf-8")
        profile.write_text('<!-- ai-kit:task-source {"format_version":"0.2.0","path":"ops/state.json",'
                           '"pause":"ops/pause.json"} -->\n', encoding="utf-8")
        next_step = "Уточнить критерий прежней задачи."
        self.update_pause(state="stopped", next_step=next_step)
        before = fingerprint(self.copy)
        check = self.check_profile()
        self.assertIn("ops/state.json (0.2.0)", check)
        self.assertIn(next_step, check)
        self.assertIn(next_step, self.migration("switch", 1))
        self.assertIn("(case)", self.migration("switch", 1, actor="local"))
        self.assertEqual(fingerprint(self.copy), before)
        self.update_pause(actor="local")
        self.check_transition_refusal("case", "об остановке перехода", "(case)")
        self.update_pause(state="reviewed", actor="case", evidence=["Учебное решение."])
        self.check_transition_refusal("case", "ops/state.json", "сверяет запись перехода")
