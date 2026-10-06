"""Single-file Realizer compatibility baseline (realizer-repo-reorg-design.md 21.4, Phase 0).

The frozen oracle (legacy/realizer_single_file/realizer.py) must reproduce the stored canonical outputs under
tests/fixtures/realizer/expected/ for every scenario under tests/fixtures/realizer/observations/, run the same way
twice, and absorb a repeated observation without a duplicate ledger entry. The exact and modified scenarios must also
reproduce the examples committed with the snapshot. To regenerate the baseline after an approved change:
``python scripts/update_realizer_goldens.py --approved-change "<why>"``; to compare a candidate implementation:
``python scripts/compare_realizer_single_file.py``.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402

EXAMPLES = ROOT / "legacy" / "realizer_single_file" / "examples"
# Scenarios whose last observation is not idempotent in the oracle (recorded in docs/proposals/realizer-repo-reorg-parity.md;
# the restatement-reprocessed scenario stores the same behavior for the candidate comparison and, being already
# reprocessed, takes the ordinary idempotency check).
KNOWN_NON_IDEMPOTENT = ("GR-026",)


class SingleFileCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = canonical.oracle_engine()
        cls.names = canonical.scenario_names()

    def test_every_scenario_has_stored_output_and_no_stale_output(self):
        stored = sorted(p.name for p in canonical.EXPECTED.iterdir() if p.is_dir())
        self.assertEqual(stored, self.names)
        golden = [n for n in self.names if n.startswith("GR-")]
        self.assertEqual(len(golden), 36, "every GR scenario that runs realize() (all but GR-004/035/036/039)")

    def test_oracle_reproduces_stored_outputs(self):
        for name in self.names:
            with self.subTest(scenario=name):
                produced = canonical.produce(self.engine, name)
                problems = canonical.semantic_diff(name, canonical.stored_expected(name), produced)
                self.assertEqual(problems, [])

    def test_deterministic_across_output_directories(self):
        for name in self.names:
            with self.subTest(scenario=name):
                scenario = canonical.load_scenario(name)
                with tempfile.TemporaryDirectory(prefix="a-") as a, tempfile.TemporaryDirectory(prefix="other-b-") as b:
                    self.assertEqual(canonical.run_scenario(self.engine, scenario, Path(a)),
                                     canonical.run_scenario(self.engine, scenario, Path(b)))

    def _reprocess_last(self, scenario, out: Path):
        """Run *scenario* into *out*, then its last observation again; returns (ledger lines before, after, snapshot)."""
        canonical.run_scenario(self.engine, scenario, out)
        ledger = next((out / "realization" / "ledger").glob("*.jsonl"))
        before = ledger.read_text(encoding="utf-8").splitlines()
        last = scenario["runs"][-1]
        snap = self.engine.realize(scenario["package_doc"], last["observation"], out,
                                   policy=self.engine.policy(last.get("policy")))
        return before, ledger.read_text(encoding="utf-8").splitlines(), snap

    def test_reprocessing_identical_observation_adds_no_ledger_entry(self):
        for name in self.names:
            scenario = canonical.load_scenario(name)
            if scenario["runs"][-1].get("observation") is None or name in KNOWN_NON_IDEMPOTENT:
                continue
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                before, after, snap = self._reprocess_last(scenario, Path(tmp))
                ids = [json.loads(line)["ledger_entry_id"] for line in before]
                self.assertEqual(len(ids), len(set(ids)), "ledger holds a duplicate entry")
                self.assertEqual(after, before)
                self.assertEqual(snap.history["entry_count"], len(ids))

    def test_known_non_idempotent_restatement_is_pinned(self):
        """Oracle defect, kept for parity (design rule 24): reprocessing a restatement observation once appends a second
        BILLING_CORRECTION_RECORDED that points at the restated measurement itself; a further pass adds nothing."""
        for name in KNOWN_NON_IDEMPOTENT:
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                scenario = canonical.load_scenario(name)
                before, after, snap = self._reprocess_last(scenario, out)
                self.assertEqual(after[:len(before)], before, "history is append-only")
                added = [json.loads(line) for line in after[len(before):]]
                self.assertEqual([e["event_type"] for e in added], ["BILLING_CORRECTION_RECORDED"])
                restated = [json.loads(line) for line in before if json.loads(line)["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED"][-1]
                self.assertEqual(added[0]["corrects_entry_id"], restated["ledger_entry_id"])
                self.assertEqual(snap.history["correction_count"], 2)
                last = scenario["runs"][-1]
                self.engine.realize(scenario["package_doc"], last["observation"], out)
                self.assertEqual(next((out / "realization" / "ledger").glob("*.jsonl")).read_text(encoding="utf-8").splitlines(), after)

    def test_example_scenarios_match_committed_examples(self):
        for kind in ("exact", "modified"):
            with self.subTest(example=kind):
                source = EXAMPLES / "realizer_jsons" / kind
                scenario = canonical.load_scenario(f"example-{kind}")
                self.assertEqual(scenario["runs"][0]["observation"],
                                 json.loads((source / "observation.json").read_text(encoding="utf-8")))
                self.assertEqual(scenario["package_doc"],
                                 json.loads((source / "source-package.json").read_text(encoding="utf-8")))
                committed = {p.relative_to(source).as_posix(): p.read_text(encoding="utf-8")
                             for p in sorted((source / "realization").rglob("*")) if p.is_file()}
                stored = {rel: text for rel, text in canonical.stored_expected(f"example-{kind}").items()
                          if rel.startswith("realization/")}
                self.assertEqual(stored, committed)

    def test_frozen_package_fixture_is_the_snapshot_publisher_output(self):
        fixture = canonical.PACKAGES / "PKG-C1928BFAD6E14C166A0F518D.json"
        for source in (EXAMPLES / "realizer_jsons" / "exact" / "source-package.json",
                       EXAMPLES / "realizer_demo" / "publisher_out" / "packages" / "1234567890123456__wh-synth-001.json"):
            self.assertEqual(fixture.read_bytes(), source.read_bytes(), source)
        pkg = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(pkg["contract_version"], "1.0.0", "the oracle accepts only Reporter JSON Contract 1.0.0")


if __name__ == "__main__":
    unittest.main()
