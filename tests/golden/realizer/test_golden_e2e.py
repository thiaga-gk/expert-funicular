"""End-to-end golden cases, Publisher to Realizer: one folder per case under tests/golden/realizer/cases/.

The GR scenarios (``test_golden_realization.py``) are the Realizer's contract layer, each on a frozen package and a
hand-built observation. These cases run the whole path instead: a real Publisher package for a demo-fleet warehouse, a
simulated world in which the owner changed the warehouse after the package, the live measurement on that world and
``realize``. Layout of a case folder:

    case.yaml                       id (GE-NN), description, publisher, world, expect
    expected/publisher/package.json the Publisher stage: the package of the case's warehouse
    expected/realizer/              the Realizer stage: measurement.json (evidence), observation.json, oracle.json,
                                    realization/... with runs.json and report.json (the stored-baseline form of
                                    tests/regression/realizer/canonical.py), or refusal.json for a refused case

``case.yaml``:

    id: GE-01
    description: what the case shows
    publisher:
      warehouse: wh-demo-013          # a demo fleet warehouse (wh-demo-<n>, the n-th DEMO_FLEET entry)
      scope: [current:auto-stop]      # --scope values; absent = the full scope
      cost_profile: aggressive        # a --cost-profile flag; absent = the warehouse's tag default
    world:
      warehouse: wh-demo-014          # optional: simulate another warehouse than the package's
      as_of: "2026-11-07T12:00:00Z"   # optional: the measurement time (this is the default)
      changes:                        # owner actions on the warehouse's timeline, each starting a configuration epoch
      - {at: "2026-10-08T14:32:00Z", set: {auto_stop: 10, size: X_SMALL}}   # keys: auto_stop, size, min, max, type
      workload: {from: 2026-10-08, factor: 0.5}   # optional: the fleet workload scaled from that day on (periodic
                                                  # queries 1/factor as often, steady load factor x as concurrent)
      runtime: {MEDIUM: 1.2}        # optional: a query started on that size runs factor x as long (default 1)
      currency: {from: 2026-09-01, code: EUR}   # optional: billed in that currency from that day on (default USD)
    expect: {...}

Before the first change the world is the fleet warehouse the package was built from (analysis time 2026-10-01T12:00Z);
only the last change may start at a time of day. The oracle is the same world with the measured knob held at the
package's source value: its billed DBU cost over the post-change window, minus the world's, is the true saving of the
change, because the simulator's noise depends only on the scenario and the day.

``expect`` (every key optional; all given keys must hold):

    conformance: EXACT              # the RPKG application's implementation_conformance (EXACT, MODIFIED, NOT_APPLIED, CONTRARY)
    relation: LESS_AGGRESSIVE       # its modification_relation
    applied_at: "<UTC time>"        # the observation's applied_at_utc (the change epoch's start)
    window: {first: <day>, last: <day>, pre_first: <day>, pre_last: <day>}   # measured windows (any subset); null = none
    measured: true                  # the observation has measured economics
    not_measured_reason: NOT_SETTLED
    near_oracle: {pct: 1}           # |replay DBU saving - oracle DBU saving| <= pct % of the oracle's
    within_pkg: {component: dbu, low_factor: 1, high_factor: 1.25}
                                    # the realized saving annualized (x 365 / window days) lies between the published
                                    # low x low_factor and high x high_factor of the measured step's candidate
                                    # (component dbu: the DBU saving; total: counterfactual - observed)
    sign: positive                  # sign of the realized saving (counterfactual - observed): positive or negative
    volume_ratio: 0.5               # the measurement's volume_ratio (post-change queries per day over pre-change), as a decimal
    cross_check: AGREES             # the billed cross-check outcome, or {outcome: INCONCLUSIVE, reason: OTHER_KNOB_CHANGED}
    independent_changes: [warehouse_size]   # the measurement's other_knob_changes, exactly
    guards: {pre: {queued_query_pct: 9.7173}, post: {p95_execution_ms_shared_sources: 2500}}
                                    # performance.guards metrics per period (pre, post, cold_start): a number (equal as
                                    # a decimal), positive, negative or null
    billed_change: {dbu: positive, total: positive, days: 28}   # measurement.billed_change fields, the same values; null = none
    measurement: {method: SIZE_RUNTIME, size.runtime_ratio: 1.2}  # observation measurement fields by dotted path, the same values
    economics: {cost_change.basis: BILLED_PRE_POST_AT_PRE_MIX}    # the realization package's economics fields by dotted path
    same_evidence_as: <case name>   # the measurement evidence equals that case's, the package's assumptions aside
    realization_state: REALIZED     # the realization package's current_state.realization_state
    classification: REALIZED        # its economics.classification_detail
    claim: CLAIMABLE                # its attribution.attribution_claim_status
    origin: INFERRED                # its attribution.origin_attribution
    disqualifiers: []               # its attribution.hard_disqualifiers (as a set)
    recognition: CONFIRMED          # the last measurement's recognition status in the ledger
    refused: "<text>"               # the case must be refused: an InputError whose message contains the text

The expectations are about the measurement and the application's conformance; the realization state, claim and
classification are pinned by the stored output only.

Checks: the Realizer stage of every case, run from its stored package, equals ``expected/realizer/`` and passes its
``expect`` block (fast; worlds and stages are cached per process), and its ledger holds the ledger invariants
(``tests/regression/realizer/invariants.py``); a separate test runs the Publisher demo once per
distinct (scope, cost profile) group and compares each stored package. Regenerate with
``python scripts/update_realizer_e2e_goldens.py --approved-change "<why>" [--stage publisher|realizer|all] [--case <name>]``
(``--check`` reports drift and writes nothing).
"""
import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.golden.realizer import e2e  # noqa: E402

CASE_KEYS = {"id", "description", "publisher", "world", "expect"}


class EndToEndCaseTests(unittest.TestCase):

    def test_cases_are_well_formed(self):
        names = e2e.case_names()
        self.assertTrue(names, "no end-to-end golden cases found")
        ids = []
        for name in names:
            with self.subTest(case=name):
                case = e2e.load_case(name)
                self.assertEqual(set(case) - CASE_KEYS, set(), "unknown case keys")
                self.assertRegex(case["id"], r"^GE-\d{2}$")
                self.assertTrue(name.startswith(f"ge{case['id'][3:]}-"), "the folder name starts with ge<NN>-")
                self.assertTrue(case.get("description"))
                self.assertTrue(case["world"]["changes"])
                self.assertEqual(set(case["world"]) - e2e.WORLD_KEYS, set(), "unknown world keys")
                self.assertTrue(case["expect"], "an empty expect block")
                self.assertEqual(set(case["expect"]) - e2e.EXPECT_KEYS, set(), "unknown expect keys")
                package = e2e.stored_package(name)
                self.assertEqual(package["warehouse"]["warehouse_id"], case["publisher"]["warehouse"])
                self.assertEqual(package["run_scope"] is None, not case["publisher"].get("scope"))
                if case["publisher"].get("cost_profile"):
                    self.assertEqual(package["cost_profile"]["name"], case["publisher"]["cost_profile"].split("=")[0])
                ids.append(case["id"])
        self.assertEqual(len(ids), len(set(ids)), "duplicate case ids")

    def test_realizer_stage_matches_expected(self):
        for name in e2e.case_names():
            with self.subTest(case=name):
                produced, stored = e2e.realizer_stage(name), e2e.stored_realizer_files(name)
                self.assertEqual(sorted(produced), sorted(stored), f"{name}: expected/realizer files")
                for path, text in produced.items():
                    self.assertTrue(stored[path] == text, f"{name}: expected/realizer/{path} differs from the current output "
                                    "(scripts/update_realizer_e2e_goldens.py --check)")

    def test_expect(self):
        names = e2e.case_names()
        produced = {name: e2e.realizer_stage(name) for name in names}
        for name in names:
            with self.subTest(case=name):
                case = e2e.load_case(name)
                failures = e2e.check_expect(name, case, e2e.stored_package(name), produced[name], produced)
                self.assertEqual(failures, [], f"{case['id']} {name}")

    def test_ledger_invariants(self):
        # The Realizer stage's state of every case holds the ledger invariants (tests/regression/realizer/invariants.py).
        from tests.regression.realizer import invariants
        checked = 0
        for name in e2e.case_names():
            with self.subTest(case=name):
                files = e2e.realizer_stage(name)
                if "refusal.json" in files:
                    self.assertEqual(invariants.ledgers(files), {})
                    continue
                self.assertTrue(invariants.ledgers(files), f"{name}: no ledger")
                invariants.assert_state(files, name, packages=[e2e.stored_package(name)])
                checked += 1
        self.assertGreater(checked, 0)

    def test_the_world_changes_the_package_warehouse_after_its_fleet_epochs(self):
        spec = e2e.world_spec(13, [{"at": "2026-10-01T00:00:00Z", "set": {"size": "X_SMALL"}},
                                   {"at": "2026-10-08T14:32:00Z", "set": {"auto_stop": 10}}])
        self.assertEqual([p["start"] for p in spec["prior"]], ["2025-07-01", "2026-10-01"])
        self.assertEqual((spec["prior"][1]["size"], spec["prior"][1]["auto_stop"]), ("X_SMALL", 30))
        self.assertEqual((spec["size"], spec["auto_stop"], spec["epoch_start"], spec["epoch_start_minute"]), ("X_SMALL", 10, "2026-10-08", 872))
        with self.assertRaises(ValueError):  # only the last change may start at a time of day
            e2e.world_spec(13, [{"at": "2026-10-01T09:00:00Z", "set": {"size": "X_SMALL"}}, {"at": "2026-10-08T00:00:00Z", "set": {"auto_stop": 10}}])
        with self.assertRaises(ValueError):
            e2e.world_spec(13, [{"at": "2026-10-08T00:00:00Z", "set": {"photon": False}}])

    def test_expect_failures_are_reported(self):
        name = "ge01-auto-stop-exact-scoped"
        case = dict(e2e.load_case(name), expect={"conformance": "MODIFIED", "sign": "negative", "near_oracle": {"pct": 0},
                                                  "within_pkg": {"component": "dbu", "low_factor": 1, "high_factor": 1},
                                                  "refused": "x"})
        failures = e2e.check_expect(name, case, e2e.stored_package(name), e2e.realizer_stage(name))
        self.assertEqual(len(failures), 1)
        self.assertIn("refused", failures[0])
        del case["expect"]["refused"]
        failures = e2e.check_expect(name, case, e2e.stored_package(name), e2e.realizer_stage(name))
        self.assertEqual([re.split(r"[: ]", f)[0] for f in failures], ["conformance", "sign", "within_pkg"])  # near_oracle 0 % holds: exact


    def test_the_update_script_writes_only_with_an_approved_change(self):
        from scripts import update_realizer_e2e_goldens as script
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as out:
            for argv in ([], ["--approved-change", " "], ["--check", "--case", "no-such-case"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    script.main(argv)
            self.assertEqual(script.main(["--check", "--stage", "realizer", "--case", "ge10-not-settled"]), 0)  # writes nothing
        self.assertIn("stored output matches", out.getvalue())


class EndToEndPublisherStageTests(unittest.TestCase):
    """The stored packages are the Publisher's current output (one demo run per scope and cost profile group)."""

    def test_stored_packages_equal_the_publisher_output(self):
        names = e2e.case_names()
        built = e2e.publisher_packages(names)
        for name in names:
            with self.subTest(case=name):
                stored = (e2e.case_dir(name) / e2e.PACKAGE_FILE).read_text(encoding="utf-8")
                self.assertTrue(stored == built[name], f"{name}: expected/publisher/package.json differs from the Publisher output "
                                "(scripts/update_realizer_e2e_goldens.py --check --stage publisher)")


if __name__ == "__main__":
    unittest.main()
