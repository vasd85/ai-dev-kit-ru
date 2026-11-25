"""Exercise incremental overview reads with an installed kit fixture."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from test_kit import fingerprint
from test_task_cards import CardProject, NEW_REPORT, PROFILE, SOURCE, WAITING

from overview import Overview, parse


NUMBER = "P/T-008"
RESTORED = "P/T-010"
RESTORED_PATH = f"{SOURCE}/open/T-010-restored"


def replace(path, before, after):
    info = path.stat()
    data = path.read_bytes()
    assert before in data and len(before) == len(after)
    temporary = path.with_suffix(".new")
    temporary.write_bytes(data.replace(before, after))
    os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
    temporary.replace(path)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="kit-overview-")
        self.addCleanup(directory.cleanup)
        self.project = CardProject(Path(directory.name).resolve() / "project")
        self.root = self.project.root

    def test_cli_source_error_names_project_and_reason(self):
        result = self.project.run_cli("overview", "--profile", "ops/absent.md")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(str(self.root), result.stderr.decode())
        self.assertIn("ops/absent.md", result.stderr.decode())

    def test_summary_and_details_disclose_only_overview_fields(self):
        before = fingerprint(self.root)
        overview = Overview(self.root, PROFILE)
        summary = overview.data(None, None)
        self.assertEqual(len(summary["tasks"]), 5)
        self.assertEqual(overview.files.descriptions, {})
        task = overview.data(NUMBER, summary["version"])
        self.assertNotIn("tasks", task)
        self.assertEqual(set(task["task"]), {"id", "goal", "waiting", "criteria", "description"})
        self.assertIn("показывает подсказку", task["task"]["description"])
        row = next(row for row in summary["tasks"] if row["id"] == NUMBER)
        self.assertEqual(row["dependencies"], ["F-search/T-008"])
        self.assertEqual(set(row), {"id", "title", "parent", "status", "kind", "location", "dependencies"})
        self.assertEqual(fingerprint(self.root), before)

    @patch("overview.SHARE_SECONDS", 0)
    def test_same_size_and_mtime_card_and_description_replacements(self):
        overview = Overview(self.root, PROFILE)
        card = self.root / WAITING
        description = card.with_name("description.md")
        with patch("overview.parse", wraps=parse) as parse_card:
            first = overview.data(NUMBER, None)
            self.assertEqual(parse_card.call_count, 5)
            overview.data(NUMBER, first["version"])
            self.assertEqual(parse_card.call_count, 5)
            replace(card, b"zz-no-results", b"ZZ-no-results")
            updated = overview.data(NUMBER, first["version"])
            self.assertNotIn("tasks", updated)
            self.assertIn("ZZ-no-results", updated["task"]["waiting"]["next_step"])
            self.assertEqual(parse_card.call_count, 6)
            replace(description, "подсказку".encode(), "пояснение".encode())
            updated = overview.data(NUMBER, first["version"])
            self.assertIn("пояснение", updated["task"]["description"])
            self.assertEqual(parse_card.call_count, 6)

    @patch("overview.SHARE_SECONDS", 0)
    def test_incomplete_detail_keeps_last_good_reader_and_recovers(self):
        overview = Overview(self.root, PROFILE)
        path = (self.root / WAITING).with_name("description.md")
        content = path.read_bytes()
        first = overview.data(NUMBER, None)
        reader = overview.reader
        path.unlink()
        failed = overview.data(NUMBER, first["version"])
        self.assertIn("description.md", failed["error"])
        self.assertIs(overview.reader, reader)
        path.write_text(" \n")
        failed = overview.data(NUMBER, first["version"])
        self.assertIn("непустая строка", failed["error"])
        path.write_bytes(content)
        self.assertEqual(overview.data(NUMBER, first["version"])["task"], first["task"])

    @patch("overview.SHARE_SECONDS", 0)
    def test_initial_failure_and_pending_placement(self):
        path = self.root / self.project.supplier_path
        original = path.read_bytes()
        path.write_bytes(b"{")
        overview = Overview(self.root, PROFILE)
        self.assertIn("нечитаемый JSON", overview.data(None, None)["error"])
        self.assertIsNone(overview.reader)
        path.write_bytes(original)
        pending = path.parent.parent.parent / "open" / path.parent.name
        pending.parent.mkdir()
        path.parent.rename(pending)
        result = overview.data("F-search/T-008", None)
        row = next(row for row in result["tasks"] if row["id"] == "F-search/T-008")
        self.assertEqual(row["status"], "Принято, осталось перенести")
        self.assertEqual(row["location"], "open")

    @patch("overview.SHARE_SECONDS", 0)
    def test_retry_confirms_absent_number_and_duplicate(self):
        overview = Overview(self.root, PROFILE)
        first = overview.data(RESTORED, None)
        original = self.root / RESTORED_PATH
        aside = self.root / "temporary-card"
        original.rename(aside)
        scan = overview._scan
        attempts = []

        def reappears():
            if attempts:
                aside.rename(original)
            attempts.append(1)
            return scan()

        with patch.object(overview, "_scan", side_effect=reappears):
            same = overview.data(RESTORED, first["version"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(same["task"], first["task"])

        original.rename(aside)
        with patch.object(overview, "_scan", wraps=scan) as check:
            self.assertIn(RESTORED, overview.data(RESTORED, first["version"])["error"])
            self.assertEqual(check.call_count, 2)
        aside.rename(original)

        duplicate = self.root / SOURCE / "done" / original.name
        shutil.copytree(original, duplicate)
        with patch.object(overview, "_scan", wraps=scan) as check:
            problem = overview.data(RESTORED, first["version"])["error"]
            self.assertEqual(check.call_count, 2)
            self.assertIn("дубль полного номера", problem)
            for path in (original, duplicate):
                self.assertIn(str(path.relative_to(self.root)), problem)

        attempts.clear()

        def deduplicated():
            if attempts:
                shutil.rmtree(duplicate)
            attempts.append(1)
            return scan()

        with patch.object(overview, "_scan", side_effect=deduplicated):
            self.assertEqual(overview.data(RESTORED, None)["task"], first["task"])
        self.assertEqual(len(attempts), 2)

    @patch("overview.SHARE_SECONDS", 0)
    def test_source_change_and_external_symlink_fail_closed(self):
        overview = Overview(self.root, PROFILE)
        first = overview.data(NUMBER, None)
        path = (self.root / WAITING).with_name("description.md")
        external = self.root.parent / "external.md"
        external.write_text("Outside the selected project")
        path.unlink()
        path.symlink_to(external)
        error = overview.data(NUMBER, first["version"])["error"]
        self.assertIn("символические ссылки", error)
        self.assertNotIn("Outside the selected project", error)
        profile = self.root / PROFILE
        profile.write_text(profile.read_text().replace(SOURCE, "another source"))
        self.assertIn("перезапустите обзор", overview.data(NUMBER, first["version"])["error"])

    def test_requests_share_one_scan_and_keep_their_own_selection(self):
        overview = Overview(self.root, PROFILE)
        start = Barrier(2)

        def request(number):
            start.wait()
            return overview.data(number, None)

        with patch.object(overview, "_scan", wraps=overview._scan) as scan:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = list(pool.map(request, [NUMBER, RESTORED]))
            self.assertEqual(scan.call_count, 1)
        self.assertEqual(first["tasks"], second["tasks"])
        self.assertEqual(first["checked_at"], second["checked_at"])
        self.assertEqual([first["task"]["id"], second["task"]["id"]], [NUMBER, RESTORED])

    @patch("overview.monotonic", return_value=100)
    def test_first_response_after_move_replaces_recent_summary_and_detail(self, _clock):
        number = "P/T-009"
        destination = (self.root / NEW_REPORT).parent
        pending = self.root / SOURCE / "open" / destination.name
        destination.rename(pending)
        card = self.project.moving
        card["assignment"]["description"] = {"path": "description.md"}
        self.project.save_json(pending.relative_to(self.root) / "task.json", card)
        description = "Описание перемещённой карточки.\n"
        (pending / "description.md").write_text(description, encoding="utf-8")
        overview = Overview(self.root, PROFILE)

        first = overview.data(number, None)
        self.assertEqual(next(r for r in first["tasks"] if r["id"] == number)["location"], "open")
        self.assertNotIn("error", overview.data(RESTORED, first["version"]))
        pending.rename(destination)
        with patch.object(overview, "_scan", wraps=overview._scan) as scan:
            result = overview.data(number, first["version"])
            self.assertNotIn("error", result)
            self.assertEqual(scan.call_count, 1)
        self.assertNotEqual(result["version"], first["version"])
        row = next(row for row in result["tasks"] if row["id"] == number)
        self.assertEqual((row["location"], row["status"]), ("done", "Завершена"))
        self.assertEqual(result["task"]["id"], number)
        self.assertEqual(result["task"]["description"], description)

    @patch("overview.monotonic", return_value=100)
    def test_recent_address_failure_confirms_real_loss_and_duplicate(self, _clock):
        original = self.root / RESTORED_PATH
        done = self.root / SOURCE / "done" / original.name
        closed = self.root / SOURCE / "closed" / original.name
        closed.parent.mkdir()
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                overview = Overview(self.root, PROFILE)
                first = overview.data(RESTORED, None)
                moved = done if duplicate else self.root / "missing-task"
                original.rename(moved)
                try:
                    if duplicate:
                        shutil.copytree(moved, closed)
                    with patch.object(overview, "_scan", wraps=overview._scan) as scan:
                        response = overview.data(RESTORED, first["version"])
                    self.assertEqual(scan.call_count, 2)
                    self.assertEqual(set(response), {"error"})
                    expected = "дубль полного номера" if duplicate else "карточка отсутствует"
                    self.assertIn(expected, response["error"])
                    self.assertIn(RESTORED, response["error"])
                finally:
                    if duplicate:
                        shutil.rmtree(closed)
                    moved.rename(original)

    @patch("overview.monotonic", return_value=100)
    def test_missing_description_at_recent_unchanged_card_reports_description(self, _clock):
        overview = Overview(self.root, PROFILE)
        first = overview.data(NUMBER, None)
        (self.root / WAITING).with_name("description.md").unlink()
        with patch.object(overview, "_scan", wraps=overview._scan) as scan:
            result = overview.data(NUMBER, first["version"])
        self.assertEqual(scan.call_count, 0)
        self.assertEqual(set(result), {"error"})
        self.assertIn("description.md", result["error"])
