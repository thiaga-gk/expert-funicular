"""Failed and lazy runs through the extracted Governor: same exception, same half-written ledger as the oracle.

The Governor evaluates its policy checks before the run needs them, so the engine must still fail (or not fail) exactly
where the single-file engine did: a malformed field raises, with the oracle's exception, at the step that reads it,
after the ledger entries before that step were appended; a malformed field the run never reads does not raise. Each
case runs the oracle and the modular engine over the same observations into separate directories and compares the
outcomes and the whole output tree (files and directories).
"""
import copy
import json
import unittest

from tests.unit.realizer.parity import fixture_package, oengine, engine, run_sequence  # noqa: E402


def events(tree):
    """The ordered ledger event types of a run tree."""
    lines = [line for rel, text in tree.items() if rel.startswith("realization/ledger/") and text for line in text.splitlines() if line.strip()]
    return [json.loads(line)["event_type"] for line in lines]


class FailedRunParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)
        cls.source_config = oengine.package_config(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def compare(self, observations, policy=None):
        new = run_sequence(engine, self.pkg, observations, policy)
        old = run_sequence(oengine, self.pkg, observations, policy)
        self.assertEqual(new[0], old[0], "outcomes")
        self.assertEqual(new[1], old[1], "output tree")
        return old

    # --- failure windows reached through a Governor check -------------------------------------------------------

    def test_readiness_failure_leaves_the_lifecycle_entries(self):
        for patch in ({"elapsed_days": "abc"}, {"query_count": "x"}, {"elapsed_days": [1]}):
            with self.subTest(patch=patch):
                outcomes, tree = self.compare([{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok")
                self.assertIn("APPLICATION_OBSERVED", events(tree))
                self.assertNotIn("VALIDATION_READY", events(tree))

    def test_performance_failure_leaves_ready_and_started_but_not_completed(self):
        for patch in ({"performance": 5}, {"performance": "x"}, {"performance": {"normalized_p95_runtime_regression_pct": "abc"}},
                      {"performance": {"normalized_p95_runtime_regression_pct": "NaN"}}):
            with self.subTest(patch=patch):
                outcomes, tree = self.compare([{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok")
                self.assertEqual(events(tree)[-2:], ["VALIDATION_READY", "VALIDATION_STARTED"])

    def test_counterfactual_failure_leaves_the_completed_validation(self):
        outcomes, tree = self.compare([{**self.base, "economics": "x"}])
        self.assertEqual(outcomes[0][0], "AttributeError")
        self.assertEqual(events(tree)[-1], "VALIDATION_COMPLETED")

    def test_economic_quality_failure_leaves_the_counterfactual_but_no_measurement(self):
        for patch in ({"attributable_spend_pct": "abc"}, {"price_coverage_pct": "NaN"}, {"currency_consistency_pct": None},
                      {"attributable_spend_pct": None}):
            with self.subTest(patch=patch):
                outcomes, tree = self.compare([{**self.base, **patch}])
                self.assertNotEqual(outcomes[0][0], "ok", outcomes)
                self.assertEqual(events(tree)[-1], "COUNTERFACTUAL_RECORDED")
                self.assertNotIn("ECONOMIC_MEASUREMENT_RECORDED", events(tree))

    def test_failure_is_followed_by_the_same_recovery(self):
        for patch in ({"elapsed_days": "abc"}, {"performance": 5}, {"economics": "x"}, {"attributable_spend_pct": "abc"}):
            with self.subTest(patch=patch):
                outcomes, _ = self.compare([{**self.base, **patch}, self.base, self.later(10)])
                self.assertEqual([o[0] for o in outcomes][0] == "ok", False)
                self.assertEqual(outcomes[1][0], "ok")
                self.assertEqual(outcomes[2][0], "ok")

    def test_failure_under_another_policy(self):
        for policy in ({"minimum_days": 1}, {"max_normalized_p95_runtime_regression_pct": "1"}, {"minimum_attributable_spend_pct": 100}):
            with self.subTest(policy=policy):
                self.compare([{**self.base, "performance": 5}], policy)
                self.compare([{**self.base, "attributable_spend_pct": "abc"}], policy)

    # --- malformed fields the run never reads: no failure, as before ------------------------------------------------

    def test_unused_malformed_fields_do_not_fail_a_run_that_never_reaches_them(self):
        unchanged = {**self.base, "config": dict(self.source_config)}      # nothing applied: no validation episode
        unready = {**self.base, "elapsed_days": 1}                          # applied but not ready: stops at readiness
        malformed = {"performance": 5, "economics": "x", "attributable_spend_pct": "abc", "price_coverage_pct": "NaN", "overlap_count": "x"}
        for label, observation in (("unchanged", unchanged), ("unready", unready)):
            with self.subTest(case=label):
                outcomes, tree = self.compare([{**observation, **malformed}])
                self.assertEqual(outcomes[0][0], "ok")
        # Only on the lifecycle-less run is readiness itself unused as well.
        outcomes, tree = self.compare([{**unchanged, "elapsed_days": "abc", "query_count": "x", **malformed}])
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("VALIDATION_READY", events(tree))

    def test_unused_economic_quality_gate_input_does_not_fail_when_economics_are_unavailable(self):
        observation = {**self.base, "attributable_spend_pct": "abc", "price_coverage_pct": "NaN", "currency_consistency_pct": []}
        observation["economics"] = {**observation["economics"], "observed_new_cost": None}
        outcomes, tree = self.compare([observation])
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("ECONOMIC_MEASUREMENT_RECORDED", events(tree))

    def test_unused_counterfactual_input_does_not_fail_a_regeneration_run(self):
        malformed = {"elapsed_days": "abc", "performance": 5, "economics": "x", "attributable_spend_pct": "abc"}
        first = self.base
        drifted = self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, **malformed)     # drift: regenerates before validation
        contrary = self.later(11, config={**self.base["config"], "warehouse_size": "X_LARGE"}, **malformed)
        outcomes, tree = self.compare([first, drifted])
        self.assertEqual([o[0] for o in outcomes], ["ok", "ok"])
        self.assertIn("DRIFT_DETECTED", events(tree))
        self.assertIn("REGENERATION_REQUESTED", events(tree))
        self.compare([contrary])

    def test_unused_malformed_field_on_a_lineage_only_run(self):
        outcomes, _ = self.compare([None, {}])
        self.assertEqual([o[0] for o in outcomes], ["ok", "ok"])


if __name__ == "__main__":
    unittest.main()
