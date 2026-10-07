"""Stored baseline of the Realizer (docs/architecture/realizer/repo-reorg-design.md 21.4; the parity record is docs/proposals/realizer/repo-reorg-parity.md).

The Realizer in src/realizer must reproduce the stored canonical outputs under tests/fixtures/realizer/expected/ for
every scenario under tests/fixtures/realizer/observations/, run the same way twice into different directories, and
absorb a repeated observation without a duplicate ledger entry. The stored outputs were generated from the single-file
Realizer (git tag ``realizer-reorg-oracle-1.0.0``), whose retirement the parity record describes. To regenerate the
baseline after an approved change: ``python scripts/update_realizer_goldens.py --approved-change "<why>"``; to compare the
working tree with a git ref: ``python scripts/compare_realizer_with_ref.py --ref <ref>``.
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

class StoredBaselineTests(unittest.TestCase):
    """The Realizer against the stored baseline."""
    engine_factory = staticmethod(canonical.modular_engine)

    @classmethod
    def setUpClass(cls):
        cls.engine = cls.engine_factory()
        cls.names = canonical.scenario_names()

    def test_engine_is_the_src_package(self):
        module = canonical.modular()
        self.assertEqual(Path(module.__file__).resolve(), (canonical.SRC / "realizer.py").resolve())
        self.assertEqual(self.engine.realize.__module__, "realizer.realizer")
        self.assertEqual(Path(sys.modules["realizer"].__file__).resolve().parent, canonical.SRC.resolve())

    def test_engine_reproduces_stored_outputs(self):
        # After every run of every scenario the state holds the ledger invariants (tests/regression/realizer/invariants.py).
        from tests.regression.realizer import invariants
        for name in self.names:
            with self.subTest(scenario=name):
                check = invariants.RunChecker(name)
                produced = canonical.produce(self.engine, name,
                                             lambda package, step, out: check(package, out, replay_through=step.get("replay_through")))
                self.assertEqual(check.runs, len(canonical.load_scenario(name)["runs"]))
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
            if scenario["runs"][-1].get("observation") is None:
                continue
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                before, after, snap = self._reprocess_last(scenario, Path(tmp))
                ids = [json.loads(line)["ledger_entry_id"] for line in before]
                self.assertEqual(len(ids), len(set(ids)), "ledger holds a duplicate entry")
                self.assertEqual(after, before)
                self.assertEqual(snap.history["entry_count"], len(ids))

    def test_reprocessing_a_restatement_adds_nothing(self):
        """A billing restatement reprocessed records nothing new: its correction is recorded once (GR-026's last run is
        the restatement; restatement-reprocessed stores it reprocessed twice more)."""
        for name in ("GR-026", "restatement-reprocessed"):
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                before, after, snap = self._reprocess_last(canonical.load_scenario(name), Path(tmp))
                self.assertEqual(after, before)
                self.assertEqual(snap.history["correction_count"], 1)

    def test_every_scenario_has_stored_output_and_no_stale_output(self):
        stored = sorted(p.name for p in canonical.EXPECTED.iterdir() if p.is_dir())
        self.assertEqual(stored, self.names)
        self.assertEqual(len(self.names), 41)
        golden = [n for n in self.names if n.startswith("GR-")]
        self.assertEqual(len(golden), 36, "every GR scenario that runs realize() (all but GR-004/035/036/039)")

    def test_example_scenarios_keep_the_documented_identities(self):
        """The two committed examples (exact and modified), frozen as scenarios."""
        identities = {
            "example-exact": ("RPKG-976E6C1C6E88CE76321758ED", "RSNAP-8C6B4D2BC18B6C3958C42378", "REVID-37F7F275591908182339F44B", "RERUN-0FCAA3465636EE32333525D0"),
            "example-modified": ("RPKG-976E6C1C6E88CE76321758ED", "RSNAP-6BA9410AD67A2706DA581986", "REVID-0054A9DB9BEB251DB6B705DA", "RERUN-6A24F009F8BEB67A7129E53A"),
        }
        folders = {"RPKG": "packages", "RSNAP": "snapshots", "REVID": "evidence", "RERUN": "regeneration"}
        for name, ids in identities.items():
            with self.subTest(example=name):
                files = canonical.stored_expected(name)
                for identity in ids:
                    self.assertIn(f"realization/{folders[identity.split('-')[0]]}/{identity}.json", files)
                self.assertEqual(canonical.load_scenario(name)["package"], "inplace-size-canary.json")

    def test_every_scenario_names_one_of_the_fixture_packages(self):
        fixtures = sorted(p.name for p in canonical.PACKAGES.glob("*.json"))
        self.assertEqual(fixtures, ["autostop-daily-oct02.json", "autostop-daily-oct03.json", "autostop-scoped-never-stop.json",
                                    "autostop-scoped-pro.json", "autostop-scoped-serverless.json", "canary-before-cut.json",
                                    "canary-rollback.json", "capacity-add-scoped.json", "capacity-cut-scoped.json",
                                    "inplace-size-canary.json", "migration-priced.json", "min-cut-scoped.json", "scope-migration-only.json",
                                    "successor-after-autostop.json",
                                    "two-ready-steps.json", "unpriced-candidate.json"])
        for name in self.names:
            with self.subTest(scenario=name):
                self.assertIn(canonical.load_scenario(name)["package"], fixtures)

    def test_every_fixture_package_is_a_valid_publisher_package_on_contract_3_1_0(self):
        """Built by scripts/build_realizer_fixtures.py from real Publisher runs; the Realizer's loader gate (schema, id and
        hash recomputed) accepts each, and refuses any other contract version."""
        from realizer.config import loader
        for path in sorted(canonical.PACKAGES.glob("*.json")):
            with self.subTest(package=path.name):
                pkg = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(pkg["contract_version"], "3.1.0", "the Realizer accepts only Reporter JSON Contract 3.1.0 packages")
                self.assertTrue(pkg["recommendation_package_id"].startswith("PKG-"))
                self.assertIsNone(loader.validate_package(pkg))

if __name__ == "__main__":
    unittest.main()
