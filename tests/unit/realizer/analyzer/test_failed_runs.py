"""Failed runs through the extracted Analyzer: the exception and the half-written output stored from the single-file release.

A malformed observation or package fails after the lineage entry (REALIZATION_CREATED) was appended and before anything
else was written, exactly as in the single-file engine; a failure while writing the evidence file leaves the same
directories. Each case runs the engine into a fresh directory and checks the outcomes and the output tree (ledger
event types, directories, a digest per file) against the stored expectation.
"""
import copy
import decimal
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, compact_tree, events, fixture_package, run_sequence  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.realizer import realize  # noqa: E402


class AnalyzerFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def compare(self, label, observations, package=None):
        package = self.pkg if package is None else package
        outcomes, tree = run_sequence(package, observations)
        expect.check(self, label, (outcomes, compact_tree(tree)))
        return outcomes, tree

    def test_unreadable_observation_fails_after_the_lineage_entry_and_before_the_evidence_file(self):
        cases = [
            ("no times", {"observed_at_utc": None, "recorded_at_utc": None}), ("config a number", {"config": 5}), ("config a list", {"config": ["x"]}),
            ("observed_at not a time", {"observed_at_utc": "nope"}), ("recorded_at not a time", {"recorded_at_utc": "nope"}),
            ("applied_at not a time", {"applied_at_utc": "nope"}), ("source unserializable", {"source": Opaque()}), ("window unserializable", {"window": Opaque()}),
        ]
        for label, patch in cases:
            with self.subTest(case=label):
                outcomes, tree = self.compare(label, [{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok")
                self.assertEqual(events(tree), ["REALIZATION_CREATED"])
                self.assertFalse(any(rel.startswith("realization/evidence/") and tree[rel] is not None for rel in tree))

    def test_malformed_package_fails_the_same_way(self):
        cases = [
            ("current_config text", {"current_config": "x"}),
            ("photon a string", {"current_config": {**self.pkg["current_config"], "photon": "ENABLED"}}),
            ("warehouse without a warehouse_id", {"warehouse": {"workspace_id": "w"}}), ("warehouse text", {"warehouse": "x"}),
        ]
        for label, patch in cases:
            with self.subTest(case=label):
                outcomes, tree = self.compare(label, [self.base], {**copy.deepcopy(self.pkg), **patch})
                self.assertNotEqual(outcomes[0][0], "ok")

    def test_evidence_write_failure_leaves_the_same_directories(self):
        # The hash accepts a Decimal, the JSON writer does not: the failure comes while writing the evidence file.
        outcomes, tree = self.compare("decimal source", [{**self.base, "source": decimal.Decimal("1.5")}])
        self.assertEqual(outcomes[0][0], "TypeError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED"])
        self.assertIn("realization/evidence", tree)
        self.assertFalse(any(rel.startswith("realization/evidence/") for rel in tree))

    def test_failure_is_followed_by_the_same_recovery(self):
        outcomes, _ = self.compare("failure then recovery", [{**self.base, "config": 5}, self.base])
        self.assertNotEqual(outcomes[0][0], "ok")
        self.assertEqual(outcomes[1][0], "ok")

    def test_corrupt_ledger_fails_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(copy.deepcopy(self.pkg), None, out)
            ledger = next((out / "realization" / "ledger").glob("*.jsonl"))
            ledger.write_text("not json\n", encoding="utf-8")
            before = sorted(p.name for p in out.rglob("*"))
            with self.assertRaises(json.JSONDecodeError):
                realize(copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
            self.assertEqual(sorted(p.name for p in out.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
