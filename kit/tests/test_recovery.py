"""Recovery, delegated authority and interrupted-revision CLI checks for package 2."""

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_records import BASE_CODE, BASE_TESTS, NOTE_CODE, NOTE_TESTS, Fixture, PROFILE, STATE
from test_kit import environment, fingerprint


LOCAL_STATE = "features/notes/state.json"
DECISION = "docs/ai-work/storage-decision.md"


def pause_after_real_edit(fixture):
    """The owned subprocess exits and is reaped before any product test is run."""
    fixture.running("A")
    code = BASE_CODE + "\n# Existing edit preserved for the next attempt.\n"
    command = [sys.executable, "-B", "-c",
               "from pathlib import Path; import json; "
               f"Path('code/source.py').write_text({code!r}, encoding='utf-8'); "
               "print(json.dumps({'edited': 'code/source.py', 'tests_run': False, 'child_commands': False}))"]
    process = subprocess.Popen(command, cwd=fixture.root, env=environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode != 0 or stderr:
        raise AssertionError(repr((process.returncode, stdout, stderr)))
    fact = {"pid": process.pid, "returncode": process.returncode, "stdout": stdout.strip(),
            "stderr": stderr, "waited": process.poll() == 0,
            "tests_run_before_termination": False,
            "meaning": "Parent communicate() waited for this owned child; it edited one file, spawned no children and ran no product test"}
    work = fixture.work("A")
    work.update(status="paused", blocker={"reason": "Writer ended after the edit and before the test",
                                           "next_step": "Read the existing diff and assign a new attempt only after confirming termination"})
    work["attempts"][0].update(phase="stopped", termination={"by": "test-parent", "fact": json.dumps(fact, ensure_ascii=False)})
    fixture.action("A", "inspect")
    fixture.data.pop("write_owner", None)
    fixture.data.pop("git_owner", None)
    fixture.data["continuation"].update(
        last_completed="A-attempt-1 edited code/source.py; its actual child process was waited and exited with code 0 before any test",
        unverified=["The existing edit in code/source.py has not been tested; no result or acceptance exists"],
        next_step="Inspect the existing edit and actual termination fact. Reading is allowed; a new writing attempt requires a fresh assignment")
    fixture.save()
    fixture.stop_fact = fact
    return fact


def resume_same_work(fixture):
    work = fixture.work("A")
    work["attempts"].append({"id": "A-attempt-2", "assignment_revision": work["assignment"]["revision"],
                             "executor": "A-next-executor", "phase": "assigned", "effects": ["read", "write", "git"]})
    work.update(status="ready", active_attempt="A-attempt-2")
    work.pop("blocker", None)
    fixture.action("A", "assign", [])
    fixture.data.update(write_owner=fixture.data["id"], git_owner=fixture.data["id"])
    fixture.data["continuation"].update(last_completed="The old process is confirmed stopped and the existing diff is retained",
                                         next_step="A-attempt-2 checks or repairs the existing edit; do not create a competing implementation")
    fixture.save()


def add_late_old_result(fixture):
    """A synthetic late record retains its old attempt and cannot become current."""
    work = fixture.work("A")
    result = copy.deepcopy(work["attempts"][-1]["result"])
    result.update(id="A-late-result", attempt_id="A-attempt-1", executor=work["attempts"][0]["executor"],
                  summary="Late synthetic record for the stopped old attempt; the parent checked the retained edit after recovery")
    result["checks"][0]["id"] = "A-late-check"
    work["attempts"][0]["result"] = result
    ac = copy.deepcopy(work["acceptances"][0])
    ac.update(id="A-late-acceptance", result=result["id"], evidence=["A-late-check"],
              applicability="Historical late record for A-attempt-1 only; it does not accept A-attempt-2")
    work["acceptances"].append(ac)
    fixture.save()
    return ac


class FeatureFixture(Fixture):
    """The product grants a logical area; its current executor lives locally."""

    def __init__(self, root):
        super().__init__(root)
        self.data["works"] = []
        self.data["actions"] = []
        self.data.pop("write_owner", None)
        self.data.pop("git_owner", None)
        feature = self.add_work("notes-feature", status="in_progress", effects=["read"])
        feature.update(kind="feature", local_state=LOCAL_STATE)
        feature["assignment"]["mandate"] = {
            "role": "notes-coordinator", "stages": ["prepare", "implement"],
            "interfaces": "Preserve book titles and the shared JSON storage interface; escalate shared-interface changes to the product coordinator and storage owner",
            "closing": "Local acceptance, overall integration, knowledge and open obligations with a maintenance owner"}
        local = self.add_work("notes-code", status="in_progress")
        self.data["works"].remove(local)
        self.data["actions"] = [a for a in self.data["actions"] if a["work"] != "notes-code"]
        local["iteration"] = "notes-iteration"
        local["assignment"]["acceptor"] = "notes-coordinator"
        self.local = {
            "format_version": "0.1-p2", "id": "notes-ledger", "revision": 1,
            "scope": "feature", "profile": PROFILE,
            "editor": {"role": "notes-coordinator", "session": "notes-session", "status": "active"},
            "parent": {"work": "notes-feature", "assignment_revision": 1, "commit": "0" * 40},
            "iterations": [{"id": "notes-iteration", "goal": "A scoped implementation accepted by the local role", "status": "active"}],
            "works": [local], "actions": [{"work": "notes-code", "kind": "execute", "bases": []}],
            "continuation": {"owner": "notes-coordinator", "last_completed": "The local role accepted the exact common mandate and assigned one scoped writer",
                             "unverified": ["The local implementation has not yet been submitted"],
                             "next_step": "The assigned local writer may continue inside the common mandate even if the product coordinator is stopped"}}
        self.data.update(write_owner="notes-feature", git_owner="notes-feature")
        self.save()
        self.rebind_parent()

    def save(self):
        super().save()
        if hasattr(self, "local"):
            path = self.root / LOCAL_STATE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.local, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def rebind_parent(self):
        self.save()
        self.local["parent"]["commit"] = self.commit("Exact common feature mandate before local continuation")
        self.save()

    def save_local(self):
        """The local role does not rewrite even identical bytes to the product ledger."""
        path = self.root / LOCAL_STATE
        path.write_text(json.dumps(self.local, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def submit_local(self):
        work = self.local["works"][0]
        attempt = work["attempts"][0]
        self.save_local()
        checked = self.commit("Local assigned attempt and inputs before actual checks")
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "code", "-v"]
        run = subprocess.run(command, cwd=self.root, env=environment(), capture_output=True, text=True)
        if run.returncode:
            raise AssertionError(run.stdout + run.stderr)
        result = {"id": "notes-code-result", "revision": 1, "assignment_id": work["assignment"]["id"],
                  "assignment_revision": 1, "attempt_id": attempt["id"], "executor": attempt["executor"],
                  "summary": "The local role submitted the saved version after actual product checks", "commit": checked,
                  "artifacts": ["code/source.py", "code/test_source.py"],
                  "checks": [{"id": "notes-local-check", "covers": [{"work": work["id"], "assignment_revision": 1, "criterion": "notes-code-criterion"}],
                              "command": " ".join(command), "cwd": ".", "expected": "Saved books survive reload",
                              "observed": (run.stdout + run.stderr).strip(), "outcome": "passed", "environment": "Owned local Python subprocess; no external service"}],
                  "unverified": [], "limitations": ["Local acceptance does not replace product integration"], "next_step": "The local accepting role compares the result with its mandate"}
        attempt.update(phase="finished", termination={"by": "test-parent", "fact": "The local product test process was waited; return code 0"}, result=result)
        work["status"] = "submitted"
        self.local["revision"] += 1
        self.local["actions"] = [{"work": work["id"], "kind": "review", "bases": []}]
        self.local["continuation"].update(last_completed="Local product checks passed and the candidate is submitted", unverified=[], next_step="Local acceptance; product integration remains separate")
        self.save_local()
        return result

    def accept_local_and_assign_next(self):
        work = self.local["works"][0]
        result = work["attempts"][0]["result"]
        work.update(status="accepted", current_acceptance="notes-local-acceptance", acceptances=[{
            "id": "notes-local-acceptance", "result": result["id"], "result_revision": 1, "assignment_revision": 1,
            "by": "notes-coordinator", "evidence": ["notes-local-check"], "unresolved": [],
            "applicability": "Only this local obligation; the product coordinator still owes overall integration"}])
        next_work = copy.deepcopy(work)
        next_work.update(id="notes-next", status="ready", active_attempt="notes-next-attempt",
                         attempts=[{"id": "notes-next-attempt", "assignment_revision": 1, "executor": "notes-next-executor", "phase": "assigned", "effects": ["read", "write", "git"]}])
        next_work.pop("acceptances")
        next_work.pop("current_acceptance")
        next_work["assignment"].update(id="notes-next-assignment", goal="Continue within the same already permitted feature mandate",
                                       criteria=[{"id": "notes-next-criterion", "expectation": "The next local result meets its scoped assignment"}])
        self.local["works"].append(next_work)
        self.local["actions"] = [{"work": "notes-next", "kind": "assign", "bases": []}]
        self.local["revision"] += 1
        self.local["continuation"].update(last_completed="The local role accepted its candidate and assigned the next local attempt",
                                             next_step="Continue the same common scope without editing the stopped product coordinator's source")
        self.save_local()


def prepare_r3(fixture):
    (fixture.root / DECISION).write_text("# Storage decision revision 2\nKeep saved titles while adding notes.\n", encoding="utf-8")
    fixture.running("A")
    a = fixture.work("A")
    a["assignment"].update(revision=2, decisions=[{"id": "storage-decision", "revision": 2}])
    a["assignment"]["inputs"].append(DECISION)
    a["attempts"][0]["assignment_revision"] = 2
    b = fixture.add_work("search", status="blocked")
    b["assignment"]["decisions"] = [{"id": "storage-decision", "revision": 1}]
    b["assignment"]["inputs"].append(DECISION)
    b["blocker"] = {"reason": "The storage decision is revision 2 but this assignment still cites revision 1",
                    "next_step": "The same product-coordinator role finishes this recorded revision review before dependent execution"}
    fixture.action("search", "inspect")
    fixture.data.update(
        decisions=[{"id": "storage-decision", "revision": 2, "source": DECISION}],
        revision_review={"decision": "storage-decision", "revision": 2, "owner": "product-coordinator",
                         "pending": ["search"], "updated": ["A"],
                         "independent_actions": ["Inspect search and the current decision; A may continue only under its existing scoped writer assignment"]})
    fixture.data["editor"]["status"] = "stopped"
    fixture.data["continuation"].update(last_completed="New decision 2 is saved and A updated; search remains at decision 1; the coordinator then stopped",
                                         unverified=["The search assignment and its affected evidence still require reconciliation"],
                                         next_step="Resume the same product-coordinator role, finish pending search reconciliation and check remaining authority; independent reading remains allowed")
    fixture.save()


def full_feature_cycle(root):
    """A concrete prepared-data lifecycle; semantic review is explicitly separate."""
    f = FeatureFixture(root)
    feature = f.work("notes-feature")
    feature["assignment"].update(
        goal="Add a note to the one existing uniquely titled book while preserving its title and JSON storage",
        boundaries="Synthetic one-book path only, local JSON and no services or release; duplicate-title policy and malformed storage are outside this fixture",
        criteria=[{"id": "notes-feature-criterion", "expectation": "A saved book reloads with its original title and added note; overall integration is accepted separately"}])
    template = copy.deepcopy(f.local["works"][0])

    def make_work(wid, goal, expectation, stage, effects, kind="task", dependencies=None):
        work = copy.deepcopy(template)
        work.update(id=wid, kind=kind, status="in_progress", active_attempt=wid + "-attempt",
                    attempts=[{"id": wid + "-attempt", "assignment_revision": 1,
                               "executor": wid + "-executor", "phase": "running", "effects": effects}])
        work["assignment"].update(id=wid + "-assignment", goal=goal, stage=stage,
                                  write_scope=[] if stage == "prepare" else ["code"],
                                  criteria=[{"id": wid + "-criterion", "expectation": expectation}])
        if dependencies:
            work["assignment"]["dependencies"] = copy.deepcopy(dependencies)
        return work

    analysis = make_work("notes-analysis", "Determine the actual JSON shape and a compatible extension for notes",
                         "Books are a list of objects with title; the reader preserves an optional note field",
                         "prepare", ["read"], "question")
    f.local["works"] = [analysis]
    f.local["actions"] = [{"work": analysis["id"], "kind": "execute", "bases": []}]
    f.local["revision"] += 1
    f.rebind_parent()
    f.data["editor"]["status"] = "stopped"
    f.save()
    common_before = (f.state_path.read_bytes(), f.state_path.stat().st_mtime_ns)
    report = {"purpose": "Full feature lifecycle on prepared temporary data, not a product/team effectiveness experiment",
              "temporary_repository": str(f.root), "diagnostics": [], "actual_checks": [],
              "automatic_scope": "CLI structure/links/authority/input freshness plus actually executed schema and product checks",
              "semantic_scope": "The accepting agent reads the exact source/tests and explains sufficiency, risks and acceptance; CLI does not prove that judgment"}

    def diagnose(label, expected=0, text=""):
        before = fingerprint(f.root)
        run = f.check()
        if run.returncode != expected or text not in run.stdout or fingerprint(f.root) != before:
            raise AssertionError(label + "\n" + run.stdout + run.stderr)
        report["diagnostics"].append({"label": label, "exit_code": run.returncode,
                                      "stdout": run.stdout, "unchanged": True})
        return run

    def obligation(work):
        return {"work": work["id"], "assignment_revision": 1, "criterion": work["assignment"]["criteria"][0]["id"]}

    def execute_check(cid, command, covers, expected):
        run = subprocess.run(command, cwd=f.root, env=environment(), capture_output=True, text=True)
        if run.returncode:
            raise AssertionError(run.stdout + run.stderr)
        check = {"id": cid, "covers": copy.deepcopy(covers), "command": " ".join(command), "cwd": ".",
                 "expected": expected, "observed": (run.stdout + run.stderr).strip(), "outcome": "passed",
                 "environment": "Actual owned Python subprocess on the saved temporary inputs; no network or service"}
        report["actual_checks"].append(copy.deepcopy(check))
        return check

    def result(work, commit, checks, summary):
        return {"id": work["id"] + "-result", "revision": 1,
                "assignment_id": work["assignment"]["id"], "assignment_revision": 1,
                "attempt_id": work["active_attempt"], "executor": work["attempts"][0]["executor"],
                "summary": summary, "commit": commit, "artifacts": ["code/source.py", "code/test_source.py"],
                "checks": checks, "unverified": [], "limitations": ["Only the declared one-book behavior; no real product or release"],
                "next_step": "The designated accepting role reviews this exact candidate and its limitations"}

    def submit(work, record, state, bases):
        work["attempts"][0].update(phase="finished", termination={"by": "test-parent", "fact": "The owned bounded check process was waited with return code 0"}, result=record)
        work["status"] = "submitted"
        state["actions"] = [a for a in state["actions"] if a["work"] != work["id"]]
        state["actions"].append({"work": work["id"], "kind": "review", "bases": copy.deepcopy(bases)})
        state["revision"] += 1
        f.save_local() if state is f.local else f.save()

    def accept(work, state, applicability):
        record = work["attempts"][0]["result"]
        acceptance = {"id": work["id"] + "-acceptance", "result": record["id"], "result_revision": 1,
                      "assignment_revision": 1, "by": work["assignment"]["acceptor"],
                      "evidence": [c["id"] for c in record["checks"]], "unresolved": [], "applicability": applicability}
        work.update(status="accepted", acceptances=[acceptance], current_acceptance=acceptance["id"])
        state["actions"] = [a for a in state["actions"] if a["work"] != work["id"]]
        state["revision"] += 1
        f.save_local() if state is f.local else f.save()
        return acceptance

    def basis(dep_work, ac, evidence, cid, replacements=None):
        selected = {"work": dep_work["id"], "acceptance": ac["id"],
                    "criterion": dep_work["assignment"]["criteria"][0]["id"],
                    "result": evidence["id"], "result_revision": evidence["revision"], "check": cid}
        if replacements:
            selected["replacements"] = replacements
        return selected

    schema_command = [sys.executable, "-B", "-c",
                      "import json,sys,tempfile; from pathlib import Path; sys.path.insert(0,'code'); "
                      "from source import save_book,read_books; "
                      "parent=tempfile.TemporaryDirectory(); path=Path(parent.name)/'books.json'; "
                      "save_book(path,'Book A'); books=read_books(path); "
                      "assert books==[{'title':'Book A'}]; books[0]['note']='Probe'; "
                      "path.write_text(json.dumps(books)); assert read_books(path)==[{'title':'Book A','note':'Probe'}]; "
                      "print(json.dumps({'shape':'list of objects','required':'title','optional_note_preserved':True})); parent.cleanup()"]
    product_command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "code", "-v"]
    integration_command = [sys.executable, "-B", "-c",
                           "import json,sys,tempfile; from pathlib import Path; sys.path.insert(0,'code'); "
                           "from source import save_book,read_books,add_note; "
                           "parent=tempfile.TemporaryDirectory(); path=Path(parent.name)/'books.json'; "
                           "save_book(path,'Legacy book'); save_book(path,'Book A'); add_note(path,'Book A','A note'); "
                           "assert read_books(path)==[{'title':'Legacy book'},{'title':'Book A','note':'A note'}]; "
                           "save_book(path,'Later book'); "
                           "assert read_books(path)==[{'title':'Legacy book'},{'title':'Book A','note':'A note'},{'title':'Later book'}]; "
                           "print(json.dumps({'legacy_book_preserved':True,'note_survives_next_save':True,'different_book_unchanged':True})); parent.cleanup()"]

    diagnose("Preparation is independently assigned within the common mandate")
    prep_commit = f.commit("Preparation assignment and exact inspected inputs")
    prep_check = execute_check("notes-schema-before", schema_command, [obligation(analysis)], "The JSON reader preserves a title and an optional note field")
    prep_result = result(analysis, prep_commit, [prep_check],
                         "Concrete answer: read_books returns the JSON list unchanged; save_book appends a title-only object. An optional note on the existing object preserves title and requires no reader-format change. Use add_note to rewrite that object, not a new parallel store.")
    submit(analysis, prep_result, f.local, [])
    diagnose("Preparation candidate may be reviewed before acceptance")
    prep_acceptance = accept(analysis, f.local, "The actual shape probe and source reading support this extension for the single existing book; accepted preparation does not itself implement notes")
    diagnose("The local role accepted the concrete preparation answer")

    dep = {"work": analysis["id"], "acceptance": prep_acceptance["id"], "criterion": "notes-analysis-criterion"}
    implementation = make_work("notes-implementation", "Implement the permitted note on the existing persisted book",
                               "The saved title survives adding and reading the note", "implement", ["read", "write", "git"], dependencies=[dep])
    f.local["works"].append(implementation)
    old_basis = basis(analysis, prep_acceptance, prep_result, prep_check["id"])
    f.local["actions"] = [{"work": implementation["id"], "kind": "execute", "bases": [old_basis]}]
    f.local["revision"] += 1
    f.save_local()
    diagnose("Implementation starts from the exact accepted preparation")
    (f.root / "code/source.py").write_text(BASE_CODE + NOTE_CODE, encoding="utf-8")
    (f.root / "code/test_source.py").write_text(BASE_TESTS + NOTE_TESTS, encoding="utf-8")
    impl_commit = f.commit("Notes implementation, tests and exact local assignment before checks")
    impl_check = execute_check("notes-implementation-product", product_command, [obligation(implementation)], "Both saved-book reload and note-preserves-title tests pass")
    current_schema = execute_check("notes-schema-after", schema_command, [obligation(analysis)], "The accepted JSON-shape obligation still holds after implementation")
    impl_result = result(implementation, impl_commit, [impl_check, current_schema], "add_note updates the existing book object, preserves its title and persists the note; the two real product tests and renewed shape probe passed")
    replacement = [{"result": prep_result["id"], "revision": 1, "check": prep_check["id"],
                    "reason": "The implementation changed shared source/test inputs; its renewed schema probe confirms the same accepted preparation obligation at the new exact commit",
                    "by": "notes-coordinator"}]
    renewed = basis(analysis, prep_acceptance, impl_result, current_schema["id"], replacement)
    submit(implementation, impl_result, f.local, [renewed])
    if "acceptances" in implementation:
        raise AssertionError("A new candidate must not be accepted before its local review")
    diagnose("The own submitted candidate renews its accepted preparation dependency without accepting itself")

    # A different action cannot borrow this still-unaccepted candidate as authority.
    next_work = make_work("premature-next", "Try to borrow an unaccepted candidate", "No such authority exists yet", "prepare", ["read"], dependencies=[dep])
    next_work["status"] = "ready"
    next_work["attempts"][0]["phase"] = "assigned"
    f.local["works"].append(next_work)
    f.local["actions"].append({"work": next_work["id"], "kind": "assign", "bases": [renewed]})
    f.save_local()
    diagnose("A separate next assignment cannot use an unaccepted implementation result", 1, "текущее подтверждение ещё не принято")
    f.local["works"].remove(next_work)
    f.local["actions"] = [a for a in f.local["actions"] if a["work"] != next_work["id"]]
    f.save_local()

    review = {
        "reviewed_commit": impl_commit, "role": "notes-coordinator", "type": "Agent content review, not an automatic product guarantee",
        "observations": ["read_books returns all JSON fields. save_book adds title-only records. add_note mutates the matching existing dictionary and dumps the same list, so the saved title is retained.",
                         "The real product tests verify both the prior saved-book behavior and the newly added note. The renewed shape probe also checks optional-field preservation at the exact implementation commit."],
        "risks_and_scope": ["The synthetic requirement covers one existing uniquely titled book. Duplicate-title selection, a missing title, malformed JSON and concurrent file updates are not established by these checks.",
                            "No external interface change or new service was introduced. A change to the common JSON interface must be sent to the product coordinator and storage owner; local acceptance cannot decide it silently."],
        "decision": "Accept this bounded local implementation; retain its limitations and require a separate product integration result before closing the common feature"}
    report["content_review"] = review
    local_acceptance = accept(implementation, f.local, "Read the exact add_note/read_books/save_book source and both product tests: existing dictionary fields and title are preserved for the declared one-book case. Duplicates, malformed JSON and concurrency remain outside this fixture. This is local acceptance only; overall integration remains open.")
    f.local["continuation"].update(last_completed="Prepared answer, actual notes implementation and addressed source/test review are accepted locally",
                                     unverified=["Product integration and common closure are not yet accepted"],
                                     next_step="The product-coordinator role resumes and independently integrates the exact final inputs")
    f.save_local()
    diagnose("Local acceptance leaves product iteration active and common integration unaccepted")
    if f.data["iterations"][0]["status"] != "active" or feature["status"] != "in_progress":
        raise AssertionError("Local acceptance must not implicitly integrate the product")
    report["local_stage"] = {"product_state": copy.deepcopy(f.data), "local_state": copy.deepcopy(f.local),
                             "common_source_bytes_and_mtime_unchanged": common_before == (f.state_path.read_bytes(), f.state_path.stat().st_mtime_ns)}
    if not report["local_stage"]["common_source_bytes_and_mtime_unchanged"]:
        raise AssertionError("The stopped product source was edited during local autonomy")

    # The same logical product role returns; no new role or duplicate local queue.
    f.data["editor"].update(status="active", session="resumed-product-session")
    f.data["iterations"][0].update(status="integrated", integration=local_acceptance["id"])
    f.save()
    diagnose("Local acceptance cannot substitute for common integration", 1, "нужна отдельная приёмка общей интеграции")
    f.data["iterations"][0].update(status="active")
    f.data["iterations"][0].pop("integration")
    integration = f.add_work("product-integration", status="in_progress", effects=["read", "write", "git"],
                             dependencies=[{"work": implementation["id"], "acceptance": local_acceptance["id"], "criterion": "notes-implementation-criterion"}])
    integration["kind"] = "integration"
    integration["assignment"].update(goal="Check the final combined saved-book and notes behavior after local acceptance",
                                         criteria=[{"id": "product-integration-criterion", "expectation": "Both old persistence and new note behavior pass on the final product inputs"}])
    integration_basis = basis(implementation, local_acceptance, impl_result, impl_check["id"])
    f.action(integration["id"], "execute", [integration_basis])
    f.save()
    diagnose("A separate common integration action has its own scope and accepted local dependency")
    final_commit = f.commit("Final common integration assignment and combined product inputs")
    common_check = execute_check("product-integration-check", integration_command,
                                 [obligation(integration), obligation(feature)],
                                 "A distinct end-to-end product scenario preserves an unrelated saved book and the existing note after another book is added")
    integration_result = result(integration, final_commit, [common_check], "Separate common integration tested the final combined saved-book and note behavior after local acceptance")
    submit(integration, integration_result, f.data, [integration_basis])
    diagnose("The common integration result is a submitted candidate")
    integration_acceptance = accept(integration, f.data, "The product-coordinator role checked both old persistence and notes on the final combined inputs; local acceptance alone was insufficient")

    # The common feature report cites the actual final integration check without
    # duplicating its execution or treating local acceptance as product acceptance.
    feature_check = copy.deepcopy(common_check)
    feature_check["id"] = "notes-feature-final-check"
    feature_check["covers"] = [obligation(feature)]
    feature_check["command"] = "Read the separately executed product-integration-check and its exact final Git inputs"
    feature_check["observed"] = "Separate accepted integration " + integration_acceptance["id"] + " at " + final_commit + ":\n" + common_check["observed"]
    feature_check["environment"] = "Content review of the actual common integration result; this does not claim another test process"
    feature_result = result(feature, final_commit, [feature_check], "The common mandate is met by the accepted local contribution and separately accepted overall integration")
    submit(feature, feature_result, f.data, [])
    f.data.pop("write_owner", None)
    f.data.pop("git_owner", None)
    feature["closure"] = {"knowledge": "Storage stays a JSON list of book objects; add_note updates the existing object and preserves title. The schema probe and product tests document the path.",
                          "obligations": ["Retain these checks; before expanding beyond one existing unique title, decide duplicate/missing-title and malformed-storage behavior"],
                          "owner": "product-coordinator"}
    f.save()
    diagnose("The common feature candidate can be reviewed after actual overall integration and resource release")
    accept(feature, f.data, "The product role checked the local result and separate overall integration at the final inputs; knowledge and future obligations remain owned after closure")
    f.data["iterations"][0].update(status="integrated", integration=integration_acceptance["id"])
    f.data["continuation"].update(last_completed="The feature and separate common integration are accepted; local workers and the common feature attempt are finished; writing/Git slots released",
                                    unverified=[], next_step="Stop after this prepared-data scenario; future maintenance belongs to product-coordinator")
    f.save()
    diagnose("Overall integration accepted, feature closed with knowledge/obligations/owner, and both slots released")
    report["final_state"] = copy.deepcopy(f.data)
    report["final_local_state"] = copy.deepcopy(f.local)
    report["checks_not_repeated"] = "The feature report points to the actual common integration process rather than claiming a second identical run"
    report["commits"] = {"preparation": prep_commit, "implementation": impl_commit, "common_integration": final_commit}
    return report


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-p2-recovery-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(Path(self.tmp.name) / "repo")

    def assertCheck(self, code=0, text=""):
        before = fingerprint(self.fixture.root)
        run = self.fixture.check()
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        self.assertIn(text, run.stdout)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(fingerprint(self.fixture.root), before, "CLI changed recovery inputs or Git")
        return run

    def test_actual_stop_inspect_then_new_attempt_tests_existing_edit(self):
        f = self.fixture
        fact = pause_after_real_edit(f)
        saved = json.loads(f.state_path.read_text())
        self.assertTrue(fact["waited"])
        self.assertFalse(fact["tests_run_before_termination"])
        self.assertFalse(json.loads(fact["stdout"])["tests_run"])
        self.assertEqual(saved["works"][0]["attempts"][0]["phase"], "stopped")
        self.assertNotIn("result", saved["works"][0]["attempts"][0])
        old = copy.deepcopy(saved["works"][0]["attempts"][0])
        self.assertCheck(text="только чтение/разбор")
        f.action("A", "execute", [])
        f.save()
        self.assertCheck(1, "допускает только inspect")
        f.action("A", "inspect")
        resume_same_work(f)
        self.assertCheck()
        result = f.submit("A")
        self.assertIn("test_saved_book_survives_read", result["checks"][0]["observed"])
        self.assertEqual(f.work("A")["attempts"][0], old)
        self.assertIn("Existing edit preserved", (f.root / "code/source.py").read_text())
        self.assertCheck()
        f.accept("A")
        self.assertCheck()

    def test_late_result_keeps_old_attempt_and_does_not_accept_new_one(self):
        f = self.fixture
        pause_after_real_edit(f)
        resume_same_work(f)
        f.submit("A")
        f.accept("A")
        current = f.work("A")["current_acceptance"]
        late = add_late_old_result(f)
        self.assertEqual(f.work("A")["current_acceptance"], current)
        self.assertCheck()
        f.work("A")["current_acceptance"] = late["id"]
        f.save()
        self.assertCheck(1, "current_acceptance/attempt")
        f.work("A")["current_acceptance"] = current
        dep = {"work": "A", "acceptance": late["id"], "criterion": "A-criterion"}
        f.add_work("next", dependencies=[dep])
        f.action("next", "assign", [{**dep, "result": "A-late-result", "result_revision": 1, "check": "A-late-check"}])
        f.save()
        self.assertCheck(1, "отменённая/непринятая/заменённая")

    def test_failed_product_check_then_corrected_new_attempt_preserves_failure(self):
        f = self.fixture
        f.running("A")
        (f.root / "code/source.py").write_text(BASE_CODE.replace('books.append({"title": title})', 'books.append({"title": "WRONG"})'), encoding="utf-8")
        checked = f.commit("A genuine failing implementation before correction")
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "code", "-v"]
        run = subprocess.run(command, cwd=f.root, env=environment(), capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0, "The prepared defect must actually fail the product check")
        failed = {"id": "A-failed-result", "revision": 1, "assignment_id": "A-assignment", "assignment_revision": 1,
                  "attempt_id": "A-attempt-1", "executor": "A-executor", "summary": "The actual product check failed before correction",
                  "commit": checked, "artifacts": ["code/source.py", "code/test_source.py"],
                  "checks": [{"id": "A-failed-check", "covers": [{"work": "A", "assignment_revision": 1, "criterion": "A-criterion"}],
                              "command": " ".join(command), "cwd": ".", "expected": "The saved title survives reload",
                              "observed": (run.stdout + run.stderr).strip(), "outcome": "failed", "environment": "Actual owned local Python test process"}],
                  "unverified": ["The persistence obligation failed"], "limitations": ["This is a failed attempt, not accepted evidence"],
                  "next_step": "Correct the title preservation in a new attempt; preserve this failure"}
        work = f.work("A")
        work["attempts"][0].update(phase="finished", termination={"by": "test-parent", "fact": f"Actual failing product test was waited; return code {run.returncode}"}, result=failed)
        work["status"] = "submitted"
        f.action("A", "review", [])
        f.data.pop("write_owner")
        f.data.pop("git_owner")
        f.save()
        self.assertCheck(1, "не все обязательные критерии")
        old_attempt = copy.deepcopy(work["attempts"][0])
        resume_same_work(f)
        (f.root / "code/source.py").write_text(BASE_CODE, encoding="utf-8")
        self.assertCheck()
        f.submit("A")
        f.accept("A")
        self.assertEqual(f.work("A")["attempts"][0], old_attempt)
        self.assertCheck()

    def test_unknown_or_running_paused_cancelled_writer_is_not_released(self):
        f = self.fixture
        original = copy.deepcopy(f.data)
        for status, phase in (("paused", "unknown"), ("cancelled", "running")):
            with self.subTest(status=status, phase=phase):
                f.data = copy.deepcopy(original)
                f.work("A").update(status=status, blocker={"reason": "Its previous execution is not confirmed stopped", "next_step": "Establish the actual process outcome before handing writing to another attempt"})
                f.work("A")["attempts"][0]["phase"] = phase
                f.action("A", "inspect")
                f.add_work("independent-reading", status="blocked")
                f.action("independent-reading", "inspect")
                f.save()
                self.assertCheck(text="только чтение/разбор")
                f.data.pop("write_owner")
                f.data.pop("git_owner")
                f.save()
                self.assertCheck(1, "ownership/write_owner")

    def test_two_writers_and_foreign_active_attempt_fail(self):
        f = self.fixture
        f.add_work("other")
        f.save()
        self.assertCheck(1, "несколько активных писателей")
        f.work("A")["active_attempt"] = "other-attempt-1"
        f.save()
        self.assertCheck(1, "active_attempt")

    def test_unknown_external_outcome_prevents_blind_repeat(self):
        f = self.fixture
        work = f.work("A")
        work["attempts"][0].update(phase="finished", effects=["external"], external_outcome="unknown",
                                    termination={"by": "test-parent", "fact": "Synthetic external-action record only; no real release performed"})
        work["attempts"].append({"id": "A-attempt-2", "assignment_revision": 1, "executor": "next-executor",
                                 "phase": "assigned", "effects": ["external"], "external_outcome": "not-performed"})
        work["active_attempt"] = "A-attempt-2"
        f.data.pop("write_owner")
        f.data.pop("git_owner")
        f.save()
        self.assertCheck(1, "неизвестный исход внешнего действия")

    def test_feature_logical_owner_and_local_writer_continue_with_stopped_product_role(self):
        self.fixture = FeatureFixture(Path(self.tmp.name) / "feature-repo")
        f = self.fixture
        f.data["editor"]["status"] = "stopped"
        f.save()
        self.assertNotEqual(f.data["write_owner"], f.local["works"][0]["attempts"][0]["executor"])
        self.assertCheck()

    def test_full_feature_preparation_notes_review_local_acceptance_and_overall_integration(self):
        report = full_feature_cycle(Path(self.tmp.name) / "full-feature-repo")
        self.assertTrue(report["local_stage"]["common_source_bytes_and_mtime_unchanged"])
        self.assertEqual(report["final_state"]["iterations"][0]["status"], "integrated")
        feature = next(w for w in report["final_state"]["works"] if w["id"] == "notes-feature")
        self.assertEqual(feature["status"], "accepted")
        self.assertTrue(feature["closure"]["obligations"])
        self.assertNotIn("write_owner", report["final_state"])
        self.assertNotIn("git_owner", report["final_state"])
        self.assertIn("test_note_preserves_saved_book", report["actual_checks"][1]["observed"])
        self.assertEqual(sum(d["exit_code"] == 1 for d in report["diagnostics"]), 2)

    def test_local_submit_accept_and_next_assignment_leave_product_source_unchanged(self):
        self.fixture = FeatureFixture(Path(self.tmp.name) / "feature-repo")
        f = self.fixture
        f.data["editor"]["status"] = "stopped"
        f.save()
        root_before = (f.state_path.read_bytes(), f.state_path.stat().st_mtime_ns)
        self.assertCheck()
        f.local["editor"]["session"] = "replacement-notes-session"
        f.local["revision"] += 1
        f.save_local()
        self.assertCheck()  # The same logical role moves sessions only in its own source.
        f.submit_local()
        self.assertCheck()  # No concrete writer while the common interval remains granted.
        f.accept_local_and_assign_next()
        self.assertCheck()
        self.assertEqual((f.state_path.read_bytes(), f.state_path.stat().st_mtime_ns), root_before)
        self.assertEqual(f.data["git_owner"], "notes-feature")

    def test_feature_stale_parent_revision_content_view_or_role_fail(self):
        self.fixture = FeatureFixture(Path(self.tmp.name) / "feature-repo")
        f = self.fixture
        original, local = copy.deepcopy(f.data), copy.deepcopy(f.local)
        changes = ("parent_revision", "parent_content", "view", "role", "parent_commit")
        for change in changes:
            with self.subTest(change=change):
                f.data, f.local = copy.deepcopy(original), copy.deepcopy(local)
                if change == "parent_revision":
                    f.local["parent"]["assignment_revision"] = 2
                elif change == "parent_content":
                    f.work("notes-feature")["assignment"]["goal"] = "Silently widened common mandate under the same revision"
                elif change == "view":
                    f.work("notes-feature")["view"] = {"state": LOCAL_STATE, "revision": 2,
                                                       "summary": "A deliberately stale derived view"}
                elif change == "role":
                    f.local["editor"]["role"] = "other-coordinator"
                    f.local["works"][0]["assignment"]["acceptor"] = "other-coordinator"
                else:
                    f.local["parent"]["commit"] = "0" * 40
                f.save()
                self.assertCheck(1)

    def test_feature_implementation_requires_stage_and_scoped_write_permission(self):
        self.fixture = FeatureFixture(Path(self.tmp.name) / "feature-repo")
        f = self.fixture
        original, local = copy.deepcopy(f.data), copy.deepcopy(f.local)
        f.work("notes-feature")["assignment"]["mandate"]["stages"] = ["prepare"]
        f.rebind_parent()
        self.assertCheck(1, "этап не разрешён общим мандатом")
        f.data, f.local = copy.deepcopy(original), copy.deepcopy(local)
        (f.root / "outside").mkdir()
        (f.root / "outside/source.txt").write_text("An input outside the feature's write interval\n")
        f.local["works"][0]["assignment"]["inputs"].append("outside")
        f.local["works"][0]["assignment"]["write_scope"] = ["outside"]
        f.save()
        self.assertCheck(1, "выход за общий отрезок")

    def test_r3_pending_revision_is_recoverable_and_independent_action_survives(self):
        f = self.fixture
        prepare_r3(f)
        recovered = json.loads(f.state_path.read_text())
        self.assertEqual(recovered["revision_review"]["owner"], "product-coordinator")
        self.assertEqual(recovered["revision_review"]["pending"], ["search"])
        self.assertEqual(recovered["revision_review"]["updated"], ["A"])
        self.assertCheck(text="search: только чтение/разбор")
        original = copy.deepcopy(f.data)
        search = f.work("search")
        search.update(status="ready", active_attempt="search-attempt-1", attempts=[{
            "id": "search-attempt-1", "assignment_revision": 1, "executor": "search-executor", "phase": "assigned", "effects": ["read"]}])
        search.pop("blocker")
        f.action("search", "assign", [])
        f.save()
        self.assertCheck(1, "decisions/revision")
        f.data = copy.deepcopy(original)
        f.data.pop("write_owner")
        f.data.pop("git_owner")
        f.save()
        self.assertCheck(1, "ownership/write_owner")
        f.data = copy.deepcopy(original)
        f.data["revision_review"]["pending"] = []
        f.save()
        self.assertCheck(1, "revision_review/pending")


def example(parent):
    fixture = Fixture(parent / "recovery-repo")
    fact = pause_after_real_edit(fixture)
    paused = copy.deepcopy(fixture.data)
    inspect = fixture.check()
    if inspect.returncode:
        raise AssertionError(inspect.stdout + inspect.stderr)
    resume_same_work(fixture)
    result = fixture.submit("A")
    fixture.accept("A")
    final = fixture.check()
    if final.returncode:
        raise AssertionError(final.stdout + final.stderr)
    feature = FeatureFixture(parent / "feature-repo")
    feature.data["editor"]["status"] = "stopped"
    feature.save()
    delegated = feature.check()
    common_before = (feature.state_path.read_bytes(), feature.state_path.stat().st_mtime_ns)
    feature.local["editor"]["session"] = "replacement-notes-session"
    feature.local["revision"] += 1
    feature.save_local()
    local_result = feature.submit_local()
    local_submitted = feature.check()
    feature.accept_local_and_assign_next()
    local_accepted = feature.check()
    common_unchanged = common_before == (feature.state_path.read_bytes(), feature.state_path.stat().st_mtime_ns)
    r3 = Fixture(parent / "r3-repo")
    prepare_r3(r3)
    interrupted = r3.check()
    if delegated.returncode or local_submitted.returncode or local_accepted.returncode or interrupted.returncode or not common_unchanged:
        raise AssertionError(delegated.stdout + local_submitted.stdout + local_accepted.stdout + interrupted.stdout)
    return {"purpose": "Checkpoint 2: actual owned process ends before tests; existing edit is tested by a new attempt; delegated authority and interrupted revision are reconstructed from files",
            "temporary_root": str(parent), "termination_fact": fact, "paused_state": paused,
            "inspect_diagnostic": {"exit_code": inspect.returncode, "stdout": inspect.stdout},
            "resumed_state": fixture.data, "product_check": result["checks"][0],
            "final_diagnostic": {"exit_code": final.returncode, "stdout": final.stdout},
            "feature_product_state": feature.data, "feature_local_state": feature.local,
            "feature_diagnostic": {"exit_code": delegated.returncode, "stdout": delegated.stdout},
            "feature_local_product_check": local_result["checks"][0],
            "feature_submitted_diagnostic": {"exit_code": local_submitted.returncode, "stdout": local_submitted.stdout},
            "feature_accepted_next_assigned_diagnostic": {"exit_code": local_accepted.returncode, "stdout": local_accepted.stdout},
            "feature_common_source_bytes_and_mtime_unchanged": common_unchanged,
            "r3_state": r3.data, "r3_diagnostic": {"exit_code": interrupted.returncode, "stdout": interrupted.stdout},
            "limitation": "Only the recorded actions are checked. Inspect does not grant writing; check cannot establish process termination by itself or prove team effectiveness"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--feature-example":
        parser = argparse.ArgumentParser(description="Create a new temporary full-feature lifecycle, without replacing an existing directory")
        parser.add_argument("--feature-example", type=Path)
        args = parser.parse_args()
        print(json.dumps(full_feature_cycle(args.feature_example.resolve()), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--example":
        parser = argparse.ArgumentParser()
        parser.add_argument("--example", type=Path)
        args = parser.parse_args()
        args.example.mkdir(parents=True, exist_ok=False)
        print(json.dumps(example(args.example.resolve()), ensure_ascii=False, indent=2))
    else:
        unittest.main()
