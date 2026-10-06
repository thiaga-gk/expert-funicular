"""Failed runs through the extracted Analyzer: same exception, same half-written output as the oracle.

A malformed observation or package fails after the lineage entry (REALIZATION_CREATED) was appended and before anything
else was written, exactly as in the single-file engine; a failure while writing the evidence file leaves the same
directories. Each case runs the oracle and the modular engine into separate directories and compares the outcomes and
the whole output tree.
"""
import copy
import decimal
import json
import unittest

from tests.unit.realizer.parity import Opaque, engine, fixture_package, oengine, run_sequence  # noqa: E402


def events(tree):
    lines = [line for rel, text in tree.items() if rel.startswith("realization/ledger/") and text for line in text.splitlines() if line.strip()]
    return [json.loads(line)["event_type"] for line in lines]


class AnalyzerFailureParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def compare(self, observations, package=None):
        package = self.pkg if package is None else package
        new = run_sequence(engine, package, observations)
        old = run_sequence(oengine, package, observations)
        self.assertEqual(new[0], old[0], "outcomes")
        self.assertEqual(new[1], old[1], "output tree")
        return old

    def test_unreadable_observation_fails_after_the_lineage_entry_and_before_the_evidence_file(self):
        for patch in ({"observed_at_utc": None, "recorded_at_utc": None}, {"config": 5}, {"config": ["x"]}, {"observed_at_utc": "nope"},
                      {"recorded_at_utc": "nope"}, {"applied_at_utc": "nope"}, {"source": Opaque()}, {"window": Opaque()}):
            with self.subTest(patch=sorted(patch)):
                outcomes, tree = self.compare([{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok")
                self.assertEqual(events(tree), ["REALIZATION_CREATED"])
                self.assertFalse(any(rel.startswith("realization/evidence/") and tree[rel] is not None for rel in tree))

    def test_malformed_package_fails_the_same_way(self):
        for patch in ({"current_config": "x"}, {"current_config": {**self.pkg["current_config"], "photon": "ENABLED"}},
                      {"warehouse": {"workspace_id": "w"}}, {"warehouse": "x"}):
            with self.subTest(patch=sorted(patch)):
                outcomes, tree = self.compare([self.base], {**copy.deepcopy(self.pkg), **patch})
                self.assertNotEqual(outcomes[0][0], "ok")

    def test_evidence_write_failure_leaves_the_same_directories(self):
        # The hash accepts a Decimal, the JSON writer does not: the failure comes while writing the evidence file.
        outcomes, tree = self.compare([{**self.base, "source": decimal.Decimal("1.5")}])
        self.assertEqual(outcomes[0][0], "TypeError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED"])
        self.assertIn("realization/evidence", tree)
        self.assertFalse(any(rel.startswith("realization/evidence/") for rel in tree))

    def test_failure_is_followed_by_the_same_recovery(self):
        outcomes, _ = self.compare([{**self.base, "config": 5}, self.base])
        self.assertNotEqual(outcomes[0][0], "ok")
        self.assertEqual(outcomes[1][0], "ok")

    def test_corrupt_ledger_fails_before_anything_is_written(self):
        import tempfile
        from pathlib import Path
        for module in (engine, oengine):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                module.realize(copy.deepcopy(self.pkg), None, out)
                ledger = next((out / "realization" / "ledger").glob("*.jsonl"))
                ledger.write_text("not json\n", encoding="utf-8")
                before = sorted(p.name for p in out.rglob("*"))
                with self.assertRaises(json.JSONDecodeError):
                    module.realize(copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
                self.assertEqual(sorted(p.name for p in out.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
