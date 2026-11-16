"""CLI compatibility of canonical format labels, aliases and saved Git history."""

from pathlib import Path
import tempfile
import unittest

from test_kit import Fixture as LegacyFixture, fingerprint
from test_records import Fixture
from test_recovery import FeatureFixture


class VersionTests(unittest.TestCase):
    def fixture(self, cls):
        parent = tempfile.TemporaryDirectory(prefix="kit-version-")
        self.addCleanup(parent.cleanup)
        return cls(Path(parent.name).resolve() / "repo")

    def check(self, fixture, code=0, text=""):
        before = fingerprint(fixture.root)
        result = fixture.check()
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn(text, result.stdout + result.stderr)
        self.assertEqual(before, fingerprint(fixture.root))

    def finish(self, fixture):
        if isinstance(fixture, Fixture):
            fixture.submit("A")
            fixture.accept("A")
        else:
            fixture.submit()
            fixture.accept()

    def test_complete_paths_for_canonical_and_old_labels(self):
        for cls, labels in ((LegacyFixture, ("0.1.0", "0.1-p1")),
                            (Fixture, ("0.2.0", "0.1-p2"))):
            for label in labels:
                with self.subTest(label=label):
                    f = self.fixture(cls)
                    f.data["format_version"] = label
                    f.save()
                    self.check(f)
                    self.finish(f)
                    self.check(f)

    def test_label_change_preserves_accepted_history(self):
        for cls, old, new in ((LegacyFixture, "0.1-p1", "0.1.0"),
                              (Fixture, "0.1-p2", "0.2.0")):
            for source, target in ((old, new), (new, old)):
                with self.subTest(source=source, target=target):
                    f = self.fixture(cls)
                    f.data["format_version"] = source
                    f.save()
                    self.finish(f)
                    f.data["format_version"] = target
                    f.save()
                    self.check(f)

    def test_alias_does_not_hide_changed_assignment(self):
        for cls, label in ((LegacyFixture, "0.1.0"), (Fixture, "0.2.0")):
            with self.subTest(label=label):
                f = self.fixture(cls)
                self.finish(f)
                f.data["format_version"] = label
                work = f.work("A") if isinstance(f, Fixture) else f.data["task"]
                work["assignment"]["goal"] = "A different, unchecked goal"
                f.save()
                self.check(f, 1)

    def test_mixed_product_and_feature_labels(self):
        for product, feature in (("0.2.0", "0.1-p2"), ("0.1-p2", "0.2.0"),
                                 ("0.2.0", "0.2.0")):
            with self.subTest(product=product, feature=feature):
                f = self.fixture(FeatureFixture)
                f.data["format_version"] = product
                f.local["format_version"] = feature
                f.save()
                self.check(f)

    def test_unknown_and_non_string_versions_stay_unsupported(self):
        f = self.fixture(Fixture)
        for value in ("0.2.1", "0.3.0", "0.1-p999", "v0.2.0", "0.2", None, [], {}, True):
            with self.subTest(value=value):
                f.data["format_version"] = value
                f.save()
                self.check(f, 2, "format_version")

    def test_legacy_stopped_state_keeps_its_limit(self):
        f = self.fixture(LegacyFixture)
        f.data["format_version"] = "0.1.0"
        f.save()
        f.stop_before_test()
        self.check(f, 2, "task/status")


if __name__ == "__main__":
    unittest.main()
