"""Failed and lazy runs through the extracted Governor: the exception and the half-written ledger are stored.

The Governor evaluates its policy checks before the run needs them, so the engine must still fail (or not fail) where
the single-file release did: a malformed field raises its exception at the step that reads it, after the ledger
entries before that step were appended; a malformed field the run never reads does not raise. Each case runs the
observations through the engine into one directory and compares the outcomes and the whole output tree (files and
directories, compacted to the ledger events and a digest per file) with the stored expectation.
"""
import copy
import unittest

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import compact_tree, events, fixture_package, run_sequence  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402


class FailedRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)
        cls.source_config = package_config(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def compare(self, label, observations, policy=None):
        """Run *observations*, check the outcomes and the output tree against the stored expectation *label*, and return them."""
        outcomes, tree = run_sequence(self.pkg, observations, policy)
        expect.check(self, label, (outcomes, compact_tree(tree)))
        return outcomes, tree

    # --- failure windows reached through a Governor check -------------------------------------------------------

    def test_readiness_failure_leaves_the_lifecycle_entries(self):
        for patch in ({"elapsed_days": "abc"}, {"query_count": "x"}, {"elapsed_days": [1]}):
            with self.subTest(patch=patch):
                outcomes, tree = self.compare(f"readiness {patch}", [{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok")
                self.assertIn("APPLICATION_OBSERVED", events(tree))
                self.assertNotIn("VALIDATION_READY", events(tree))

    def test_performance_failure_leaves_ready_and_started_but_not_completed(self):
        for patch in ({"performance": 5}, {"performance": "x"}, {"performance": {"normalized_p95_runtime_regression_pct": "abc"}},
                      {"performance": {"normalized_p95_runtime_regression_pct": "NaN"}}):
            with self.subTest(patch=patch):
                outcomes, tree = self.compare(f"performance {patch}", [{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok")
                self.assertEqual(events(tree)[-2:], ["VALIDATION_READY", "VALIDATION_STARTED"])

    def test_counterfactual_failure_leaves_the_completed_validation(self):
        outcomes, tree = self.compare("counterfactual", [{**self.base, "economics": "x"}])
        self.assertEqual(outcomes[0][0], "AttributeError")
        self.assertEqual(events(tree)[-1], "VALIDATION_COMPLETED")

    def test_economic_quality_failure_leaves_the_counterfactual_but_no_measurement(self):
        for patch in ({"attributable_spend_pct": "abc"}, {"price_coverage_pct": "NaN"}, {"currency_consistency_pct": None},
                      {"attributable_spend_pct": None}):
            with self.subTest(patch=patch):
                outcomes, tree = self.compare(f"economic quality {patch}", [{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok", outcomes)
                self.assertEqual(events(tree)[-1], "COUNTERFACTUAL_RECORDED")
                self.assertNotIn("ECONOMIC_MEASUREMENT_RECORDED", events(tree))

    def test_failure_is_followed_by_recovery(self):
        for patch in ({"elapsed_days": "abc"}, {"performance": 5}, {"economics": "x"}, {"attributable_spend_pct": "abc"}):
            with self.subTest(patch=patch):
                outcomes, _ = self.compare(f"recovery {patch}", [{**self.base, **patch}, self.base, self.later(10)])
                self.assertEqual([o[0] for o in outcomes][0] == "ok", False)
                self.assertEqual(outcomes[1][0], "ok")
                self.assertEqual(outcomes[2][0], "ok")

    def test_failure_under_another_policy(self):
        for policy in ({"minimum_days": 1}, {"max_normalized_p95_runtime_regression_pct": "1"}, {"minimum_attributable_spend_pct": 100}):
            with self.subTest(policy=policy):
                self.compare(f"policy {policy} performance", [{**self.base, "performance": 5}], policy)
                self.compare(f"policy {policy} economic quality", [{**self.base, "attributable_spend_pct": "abc"}], policy)

    # --- malformed fields the run never reads: no failure, as before ------------------------------------------------

    def test_unused_malformed_fields_do_not_fail_a_run_that_never_reaches_them(self):
        unchanged = {**self.base, "config": dict(self.source_config)}      # nothing applied: no validation episode
        unready = {**self.base, "elapsed_days": 1}                          # applied but not ready: stops at readiness
        malformed = {"performance": 5, "economics": "x", "attributable_spend_pct": "abc", "price_coverage_pct": "NaN", "overlap_count": "x"}
        for label, observation in (("unchanged", unchanged), ("unready", unready)):
            with self.subTest(case=label):
                outcomes, tree = self.compare(f"unused {label}", [{**observation, **malformed}])
                self.assertEqual(outcomes[0][0], "ok")
        # Only on the lifecycle-less run is readiness itself unused as well.
        outcomes, tree = self.compare("unused, nothing applied", [{**unchanged, "elapsed_days": "abc", "query_count": "x", **malformed}])
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("VALIDATION_READY", events(tree))

    def test_unused_economic_quality_gate_input_does_not_fail_when_economics_are_unavailable(self):
        observation = {**self.base, "attributable_spend_pct": "abc", "price_coverage_pct": "NaN", "currency_consistency_pct": []}
        observation["economics"] = {**observation["economics"], "observed_new_cost": None}
        outcomes, tree = self.compare("unused economic quality input", [observation])
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("ECONOMIC_MEASUREMENT_RECORDED", events(tree))

    def test_unused_counterfactual_input_does_not_fail_a_regeneration_run(self):
        malformed = {"elapsed_days": "abc", "performance": 5, "economics": "x", "attributable_spend_pct": "abc"}
        first = self.base
        drifted = self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, **malformed)     # drift: regenerates before validation
        contrary = self.later(11, config={**self.base["config"], "warehouse_size": "X_LARGE"}, **malformed)
        outcomes, tree = self.compare("unused counterfactual input, drift", [first, drifted])
        self.assertEqual([o[0] for o in outcomes], ["ok", "ok"])
        self.assertIn("DRIFT_DETECTED", events(tree))
        self.assertIn("REGENERATION_REQUESTED", events(tree))
        self.compare("unused counterfactual input, contrary", [contrary])

    def test_unused_malformed_field_on_a_lineage_only_run(self):
        outcomes, _ = self.compare("lineage only", [None, {}])
        self.assertEqual([o[0] for o in outcomes], ["ok", "ok"])


if __name__ == "__main__":
    unittest.main()
