"""Package-2 CLI checks on synthetic local Git repositories, without services."""

import argparse
import copy
import json
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_kit import Fixture as LegacyFixture, TOOL, environment, fingerprint


STATE = "docs/ai-work/state.json"
PROFILE = "docs/ai-work/project.md"
BASE_CODE = '''import json
from pathlib import Path

def read_books(path):
    source = Path(path)
    return json.loads(source.read_text()) if source.exists() else []

def save_book(path, title):
    books = read_books(path)
    books.append({"title": title})
    Path(path).write_text(json.dumps(books))
'''
NOTE_CODE = '''
def add_note(path, title, note):
    books = read_books(path)
    for book in books:
        if book["title"] == title:
            book["note"] = note
    Path(path).write_text(json.dumps(books))
'''
BASE_TESTS = '''import tempfile
from pathlib import Path
import unittest
from source import read_books, save_book

class BooksTest(unittest.TestCase):
    def test_saved_book_survives_read(self):
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "books.json"
            save_book(path, "Book A")
            self.assertEqual(read_books(path), [{"title": "Book A"}])
'''
NOTE_TESTS = '''
    def test_note_preserves_saved_book(self):
        from source import add_note
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "books.json"
            save_book(path, "Book A")
            add_note(path, "Book A", "A note")
            self.assertEqual(read_books(path), [{"title": "Book A", "note": "A note"}])
'''


class Fixture:
    git = LegacyFixture.git
    commit = LegacyFixture.commit

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "docs/ai-work").mkdir(parents=True)
        (self.root / "code").mkdir()
        self.state_path = self.root / STATE
        (self.root / PROFILE).write_text(
            "# Synthetic package-2 check\n\n"
            "The user authorized only this temporary test fixture. Books persist in a local JSON file; "
            "notes preserve the saved title. No product, release, service or paid action is authorized.\n"
            "One logical coordinator and one writer own local Git. Registered inputs are this profile "
            "and the complete code directory; no secrets or ignored external inputs are covered.\n",
            encoding="utf-8")
        (self.root / "code/source.py").write_text(BASE_CODE, encoding="utf-8")
        (self.root / "code/test_source.py").write_text(BASE_TESTS, encoding="utf-8")
        self.data = {
            "format_version": "0.1-p2", "id": "product-ledger", "revision": 1,
            "scope": "product", "profile": PROFILE,
            "editor": {"role": "product-coordinator", "session": "fixture-session", "status": "active"},
            "iterations": [{"id": "iteration-1", "goal": "Persist books, then add notes", "status": "active"}],
            "works": [], "actions": [],
            "continuation": {"owner": "product-coordinator", "last_completed": "Test fixture prepared",
                             "unverified": ["No product checks run yet"],
                             "next_step": "Assign only the declared action; preserve prior attempts"}}
        self.add_work("A")
        self.save()
        self.git("init", "--quiet", "--template=", "--initial-branch=main")
        self.commit("Prepared package-2 assignment")

    def save(self):
        self.state_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def work(self, wid):
        return next(work for work in self.data["works"] if work["id"] == wid)

    def add_work(self, wid, status="ready", dependencies=None, effects=None):
        work = {
            "id": wid, "kind": "task", "status": status, "iteration": "iteration-1",
            "assignment": {
                "id": wid + "-assignment", "revision": 1,
                "goal": {"A": "Persist and reload the saved book", "B": "Add notes while preserving saved books"}.get(wid, "Prepare the next allowed increment"),
                "boundaries": "Only the registered code directory; no services or release",
                "inputs": [PROFILE, "code"], "write_scope": ["code"],
                "criteria": [{"id": wid + "-criterion", "expectation": "Saved book survives reload" if wid == "A" else "The declared behavior passes its product check"}],
                "plan": ["Read the assignment", "Change the scoped code", "Commit inputs", "Test the saved version", "Submit the result"],
                "acceptor": "product-coordinator", "stage": "implement",
                "authority": {"by": "user", "source": PROFILE}}}
        if dependencies:
            work["assignment"]["dependencies"] = copy.deepcopy(dependencies)
        if status in ("ready", "in_progress"):
            aid = wid + "-attempt-1"
            work["attempts"] = [{"id": aid, "assignment_revision": 1, "executor": wid + "-executor",
                                 "phase": "assigned" if status == "ready" else "running",
                                 "effects": effects or ["read", "write", "git"]}]
            work["active_attempt"] = aid
            self.action(wid, "assign" if status == "ready" else "execute", [])
            if "write" in work["attempts"][0]["effects"] or "git" in work["attempts"][0]["effects"]:
                self.data["write_owner"] = self.data["id"]
                if "git" in work["attempts"][0]["effects"]:
                    self.data["git_owner"] = self.data["id"]
        elif status in ("blocked", "paused", "cancelled", "revise"):
            work["blocker"] = {"reason": "Required preceding work is not accepted", "next_step": "Inspect and resolve the dependency before assigning execution"}
        self.data["works"].append(work)
        return work

    def action(self, wid, kind, bases=None):
        self.data["actions"] = [a for a in self.data["actions"] if a["work"] != wid]
        action = {"work": wid, "kind": kind}
        if kind != "inspect":
            action["bases"] = [] if bases is None else copy.deepcopy(bases)
        self.data["actions"].append(action)

    def check(self):
        return subprocess.run([sys.executable, "-B", str(TOOL), "check", "--root", str(self.root)],
                              env=environment(), capture_output=True, text=True)

    def running(self, wid):
        work = self.work(wid)
        work["status"] = "in_progress"
        work["attempts"][-1]["phase"] = "running"
        self.action(wid, "execute", [])
        self.save()

    def submit(self, wid, extra_covers=()):
        self.running(wid)
        work = self.work(wid)
        attempt = work["attempts"][-1]
        # Commit both the assigned attempt and exact assignment BEFORE actual tests.
        checked = self.commit("Saved inputs for " + wid)
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "code", "-v"]
        run = subprocess.run(command, cwd=self.root, env=environment(), capture_output=True, text=True)
        if run.returncode:
            raise AssertionError(run.stdout + run.stderr)
        own = {"work": wid, "assignment_revision": 1, "criterion": wid + "-criterion"}
        result = {
            "id": wid + "-result-1", "revision": 1,
            "assignment_id": work["assignment"]["id"], "assignment_revision": work["assignment"]["revision"],
            "attempt_id": attempt["id"], "executor": attempt["executor"],
            "summary": "The saved version passed the actual local product checks",
            "commit": checked, "artifacts": ["code/source.py", "code/test_source.py"],
            "checks": [{"id": wid + "-check-1", "covers": [own, *copy.deepcopy(extra_covers)],
                        "command": " ".join(command), "cwd": ".", "expected": "All declared book persistence checks pass",
                        "observed": (run.stdout + run.stderr).strip(), "outcome": "passed",
                        "environment": f"Python {platform.python_version()}, {platform.system()} {platform.release()}"}],
            "unverified": [], "limitations": ["Only this synthetic book/notes behavior and registered inputs"],
            "next_step": "The accepting role compares the result with the assignment"}
        attempt.update(phase="finished", termination={"by": "test-parent", "fact": "subprocess.run waited for the local test process; return code 0"}, result=result)
        work["status"] = "submitted"
        self.action(wid, "review", [])
        self.data.pop("write_owner", None)
        self.data.pop("git_owner", None)
        self.data["continuation"].update(last_completed=wid + " submitted after real product checks", unverified=[], next_step="Review the candidate; it has not accepted itself")
        self.save()
        return result

    def accept(self, wid, handoff=True):
        work = self.work(wid)
        result = work["attempts"][-1]["result"]
        ac = {"id": wid + "-acceptance-1", "result": result["id"], "result_revision": result["revision"],
              "assignment_revision": result["assignment_revision"], "by": "product-coordinator",
              "evidence": [result["checks"][0]["id"]], "unresolved": [],
              "applicability": "Accepted historical obligations at the exact checked version; future actions select current evidence"}
        work.update(status="accepted", acceptances=[ac], current_acceptance=ac["id"])
        self.data["actions"] = [a for a in self.data["actions"] if a["work"] != wid]
        if handoff:
            self.action(wid, "handoff", [])
        self.data["continuation"].update(last_completed=wid + " accepted by the responsible role", next_step="Read the exact acceptance before selecting new work")
        self.save()
        return ac

    def r1(self):
        a_result = self.submit("A")
        self.accept("A", handoff=False)
        self.add_work("B")
        (self.root / "code/source.py").write_text(BASE_CODE + NOTE_CODE, encoding="utf-8")
        (self.root / "code/test_source.py").write_text(BASE_TESTS + NOTE_TESTS, encoding="utf-8")
        b_result = self.submit("B", [{"work": "A", "assignment_revision": 1, "criterion": "A-criterion"}])
        self.accept("B", handoff=False)
        dep = {"work": "A", "acceptance": "A-acceptance-1", "criterion": "A-criterion"}
        self.add_work("D", dependencies=[dep])
        basis = {**dep, "result": b_result["id"], "result_revision": 1, "check": "B-check-1",
                 "replacements": [{"result": a_result["id"], "revision": 1, "check": "A-check-1",
                                   "reason": "Notes changed shared storage; the new product check explicitly verifies saved books again", "by": "product-coordinator"}]}
        self.action("D", "assign", [basis])
        self.data["continuation"].update(last_completed="R1: A/C1 preserved; B/C2 accepted and explicitly covers A's book persistence obligation",
                                         next_step="D may use B/C2 as current evidence for the exact historically accepted A obligation")
        self.save()
        return a_result, b_result


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-p2-test-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(Path(self.tmp.name) / "repo")

    def assertCheck(self, code=0, text=""):
        before = fingerprint(self.fixture.root)
        run = self.fixture.check()
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        self.assertIn(text, run.stdout)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(fingerprint(self.fixture.root), before, "read-only CLI changed the fixture or Git")
        return run

    def test_short_ready_running_submitted_accepted_handoff(self):
        self.assertCheck()
        self.fixture.running("A")
        self.assertCheck()
        result = self.fixture.submit("A")
        self.assertIn("test_saved_book_survives_read", result["checks"][0]["observed"])
        self.assertNotIn("acceptances", self.fixture.work("A"))
        self.assertCheck()  # Candidate does not need prior acceptance of itself.
        self.fixture.accept("A", handoff=False)
        self.assertCheck()  # Historical acceptance is readable without a new action.
        self.fixture.action("A", "handoff", [])
        self.fixture.save()
        self.assertCheck()

    def test_future_unaccepted_dependency_does_not_block_independent_action(self):
        f = self.fixture
        f.add_work("future-prerequisite", status="blocked")
        f.add_work("future", status="blocked", dependencies=[{"work": "future-prerequisite", "acceptance": None, "criterion": "future-prerequisite-criterion"}])
        f.action("future", "inspect")
        f.save()
        self.assertCheck(text="только чтение")

    def test_r1_new_confirmation_preserves_history(self):
        old, new = self.fixture.r1()
        self.assertNotEqual(old["commit"], new["commit"])
        self.assertEqual(self.fixture.work("A")["attempts"][0]["result"]["commit"], old["commit"])
        self.assertCheck()

    def test_unaccepted_dependency_cannot_authorize_assignment(self):
        f = self.fixture
        f.submit("A")
        f.add_work("D", dependencies=[{"work": "A", "acceptance": None, "criterion": "A-criterion"}])
        f.save()
        self.assertCheck(1, "пропущено обязательное основание")

    def test_r1_old_evidence_and_missing_required_basis_fail(self):
        f = self.fixture
        f.r1()
        original = copy.deepcopy(f.data)
        f.data["actions"][0]["bases"] = []
        f.save()
        self.assertCheck(1, "пропущено обязательное основание")
        f.data = copy.deepcopy(original)
        b = f.data["actions"][0]["bases"][0]
        b.update(result="A-result-1", check="A-check-1")
        b.pop("replacements")
        f.save()
        self.assertCheck(1, "входы в HEAD")

    def test_dependency_unknown_cancelled_and_cyclic_fail(self):
        f = self.fixture
        f.r1()
        original = copy.deepcopy(f.data)
        f.work("D")["assignment"]["dependencies"][0]["work"] = "missing-work"
        f.save()
        self.assertCheck(1, "неизвестная работа")
        f.data = copy.deepcopy(original)
        f.work("A").update(status="cancelled", blocker={"reason": "Cancelled by coordinator", "next_step": "Revise D before execution"})
        f.work("A").pop("current_acceptance")
        f.save()
        self.assertCheck(1, "отменённая/непринятая")
        f.data = copy.deepcopy(original)
        f.add_work("future-X", status="blocked", dependencies=[{"work": "future-Y", "acceptance": None, "criterion": "future-Y-criterion"}])
        f.add_work("future-Y", status="blocked", dependencies=[{"work": "future-X", "acceptance": None, "criterion": "future-X-criterion"}])
        f.save()
        self.assertCheck(1, "цикл зависимостей")

    def test_wrong_current_evidence_links_fail(self):
        f = self.fixture
        f.r1()
        original = copy.deepcopy(f.data)
        cases = [("criterion", "wrong-criterion"), ("acceptance", "wrong-acceptance"),
                 ("result_revision", 2), ("check", "wrong-check"), ("result", "missing-result")]
        for key, value in cases:
            with self.subTest(key=key):
                f.data = copy.deepcopy(original)
                f.data["actions"][0]["bases"][0][key] = value
                f.save()
                self.assertCheck(1)

    def test_result_wrong_attempt_commit_source_and_obligation_fail(self):
        f = self.fixture
        f.r1()
        original = copy.deepcopy(f.data)
        for key, value in (("attempt_id", "wrong-attempt"), ("assignment_revision", 2), ("commit", "0" * 40)):
            with self.subTest(key=key):
                f.data = copy.deepcopy(original)
                f.work("B")["attempts"][0]["result"][key] = value
                f.save()
                self.assertCheck(1)
        f.data = copy.deepcopy(original)
        f.work("B")["attempts"][0]["result"] = {"path": "docs/ai-work/missing-result.json"}
        f.save()
        self.assertCheck(1, "отсутствует источник")
        f.data = copy.deepcopy(original)
        f.work("B")["attempts"][0]["result"]["checks"][0]["covers"][1]["criterion"] = "wrong-criterion"
        f.save()
        self.assertCheck(1, "неизвестный критерий")

    def test_replacement_requires_preserved_source_reason_and_role(self):
        f = self.fixture
        f.r1()
        original = copy.deepcopy(f.data)
        for key in ("result", "reason", "by"):
            with self.subTest(key=key):
                f.data = copy.deepcopy(original)
                prior = f.data["actions"][0]["bases"][0]["replacements"][0]
                if key == "result":
                    prior[key] = "missing-old-result"
                else:
                    prior[key] = ""
                f.save()
                self.assertCheck(1)

    def test_current_assignment_changed_without_revision_fails(self):
        f = self.fixture
        f.submit("A")
        f.work("A")["assignment"]["goal"] = "Different requirement under the old revision number"
        f.save()
        self.assertCheck(1, "содержание поручения изменилось")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--example":
        parser = argparse.ArgumentParser(description="Create a new synthetic R1 repository; never replace an existing path")
        parser.add_argument("--example", type=Path)
        args = parser.parse_args()
        fixture = Fixture(args.example)
        old, new = fixture.r1()
        run = fixture.check()
        print(run.stdout, end="")
        if run.stderr:
            print(run.stderr, file=sys.stderr)
        print(json.dumps({"root": str(fixture.root), "state": str(fixture.state_path),
                          "C1": old["commit"], "C2": new["commit"], "expected_code": 0}, ensure_ascii=False))
        return run.returncode
    unittest.main()


if __name__ == "__main__":
    sys.exit(main())
