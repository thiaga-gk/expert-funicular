"""Failed runs through the extracted Reconciler: same exception, same half-written ledger as the oracle.

The Reconciler decides every lifecycle event of a run before the engine records any of them, so a failure inside it
must still surface after exactly the ledger entries (and files) the single-file engine had written when it raised:
matching failures right after the evidence file, a scenario-scope failure before any lifecycle entry, a failure in the
middle of the lifecycle after the entries decided before it, a regeneration request after every lifecycle entry, the
readiness check before the validation episode, and the downstream regeneration after the measurement. Each case runs
the oracle and the modular engine over the same observations into separate directories and compares the outcomes and
the whole output tree (files and directories).
"""
import copy
import json
import unittest
from unittest import mock

from tests.unit.realizer.parity import Unprintable, Untestable, engine, events, files, fixture_package, oengine, run_sequence  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402

MIGRATION = "SCN-1DFDCB17E0622D63C6758AB4"


def two_step_package():
    return json.loads((canonical.PACKAGES / "GR-019-two-ready-steps.json").read_text(encoding="utf-8"))


def recommended_plan(package):
    sid = package["recommended_plan"]["scenario_id"]
    return next(s for s in package["scenarios"] if s["scenario_id"] == sid)["execution_plan"]


class ReconcilerFailedRunParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)
        cls.source_config = oengine.package_config(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def compare(self, observations, policy=None, package=None):
        package = self.pkg if package is None else package
        new = run_sequence(engine, package, observations, policy)
        old = run_sequence(oengine, package, observations, policy)
        self.assertEqual(new[0], old[0], "outcomes")
        self.assertEqual(new[1], old[1], "output tree")
        return old

    def assert_failed(self, outcomes, index=0, kind=None):
        self.assertNotEqual(outcomes[index][0], "ok", outcomes)
        if kind is not None:
            self.assertEqual(outcomes[index][0], kind, outcomes)

    # --- matching: fails after the evidence file, before any lifecycle entry ----------------------------------------

    def malformed_plans(self):
        def step_not_a_number(p):
            recommended_plan(p)[0]["step"] = "abc"

        def step_not_a_mapping(p):
            recommended_plan(p).insert(0, 5)

        def invalidates_not_a_list(p):
            recommended_plan(p)[0]["invalidates"] = 5

        def scenario_not_a_mapping(p):
            p["scenarios"].insert(0, 5)

        def recommended_plan_not_a_mapping(p):
            p["recommended_plan"] = 5

        return {f.__name__: f for f in (step_not_a_number, step_not_a_mapping, invalidates_not_a_list, scenario_not_a_mapping, recommended_plan_not_a_mapping)}

    def test_matching_failure_leaves_the_lineage_entry_and_the_evidence_file(self):
        for label, corrupt in self.malformed_plans().items():
            with self.subTest(case=label):
                package = copy.deepcopy(self.pkg)
                corrupt(package)
                outcomes, tree = self.compare([self.base], package=package)
                self.assert_failed(outcomes)
                self.assertEqual(events(tree), ["REALIZATION_CREATED"])
                self.assertEqual(len(files(tree, "evidence")), 1)

    def test_matching_failure_for_an_applied_scenario_other_than_the_recommended_one(self):
        package = copy.deepcopy(self.pkg)
        next(s for s in package["scenarios"] if s["scenario_id"] == MIGRATION)["execution_plan"][1]["step"] = "x"
        migrated = {**self.base, "applied_scenario_id": MIGRATION, "config": {**self.source_config, "warehouse_type": "SERVERLESS"}}
        outcomes, tree = self.compare([migrated], package=package)
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED"])

    # --- scenario scope: fails before any lifecycle entry -----------------------------------------------------------

    def test_scenario_scope_failure_records_nothing_after_the_evidence(self):
        observation = canonical.load_scenario("GR-019")["runs"][0]["observation"]
        for label, invalidates in (("mixed types", [1]), ("unhashable", [{"knob": "max_clusters"}])):
            with self.subTest(case=label):
                package = two_step_package()
                recommended_plan(package)[0]["invalidates"] = invalidates
                outcomes, tree = self.compare([observation], package=package)
                self.assert_failed(outcomes, kind="TypeError")
                self.assertEqual(events(tree), ["REALIZATION_CREATED"])

    def test_scenario_scope_readiness_failure_leaves_the_scenario_application_and_intent(self):
        observation = {**canonical.load_scenario("GR-019")["runs"][0]["observation"], "elapsed_days": "abc"}
        outcomes, tree = self.compare([observation], package=two_step_package())
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED"])

    # --- in the middle of the lifecycle: fails after the entries decided before it ----------------------------------

    def test_failure_after_drift_keeps_the_drift_entry(self):
        cases = {
            # the changed step's payload reads the value after the drift entry
            "step payload": self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, effective_time_quality=Unprintable()),
            # nothing changed on a plan step: the intent is the first to read it
            "intent": self.later(10, config={**self.source_config, "warehouse_channel": "PREVIEW"}, intent_state=Unprintable()),
        }
        for label, drifted in cases.items():
            with self.subTest(case=label):
                outcomes, tree = self.compare([self.base, drifted])
                self.assertEqual(outcomes[0][0], "ok")
                self.assert_failed(outcomes, 1, kind="ValueError")
                self.assertEqual(events(tree)[-1], "DRIFT_DETECTED")

    def test_failure_after_intent_and_rollback_keeps_what_came_before(self):
        cases = {
            "rollback requested": ({"rollback_requested": Untestable()}, "INTENT_RECORDED"),
            "rollback observed": ({"rollback_requested": True, "rollback_observed": Untestable()}, "ROLLBACK_REQUESTED"),
            "replacement package": ({"rollback_observed": True, "replacement_package_id": Untestable()}, "ROLLBACK_OBSERVED"),
        }
        for label, (patch, last) in cases.items():
            with self.subTest(case=label):
                outcomes, tree = self.compare([{**self.base, **patch}])
                self.assert_failed(outcomes, kind="ValueError")
                self.assertEqual(events(tree)[-1], last)
                self.assertIn("APPLICATION_OBSERVED", events(tree))

    # --- the regeneration request ends the lifecycle: fails after every lifecycle entry ----------------------------

    def test_terminal_regeneration_failure_leaves_every_lifecycle_entry_and_no_rerun_file(self):
        unhashable_policy = {"realized_ratio_min": "NaN"}   # the policy snapshot id cannot be computed
        drifted = self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, replacement_package_id="PKG-AAAAAAAAAAAAAAAAAAAAAAAA")
        outcomes, tree = self.compare([self.base, drifted], unhashable_policy)
        self.assert_failed(outcomes, 0, kind="ValueError")   # the first run fails at its validation episode
        self.assert_failed(outcomes, 1, kind="ValueError")
        # (the second run's intent is the first run's entry again, on the same episode, so it is suppressed)
        self.assertEqual(events(tree)[-3:], ["DRIFT_DETECTED", "SOURCE_PACKAGE_SUPERSEDED", "REPLACEMENT_PACKAGE_LINKED"])
        self.assertNotIn("REGENERATION_REQUESTED", events(tree))
        self.assertEqual(files(tree, "regeneration"), [])

        contrary = {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}}
        outcomes, tree = self.compare([contrary], unhashable_policy)
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED"])
        self.assertEqual(files(tree, "regeneration"), [])

    # --- the downstream regeneration: fails after the measurement ---------------------------------------------------

    def test_downstream_regeneration_failure_leaves_the_measurement(self):
        outcomes, tree = self.compare([self.base], {"remeasure_days_after_invalidating_change": "abc"})
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree)[-1], "ECONOMIC_MEASUREMENT_RECORDED")
        self.assertEqual(files(tree, "regeneration"), [])

        corrected = {**self.base, "correction": {"type": "RESTATEMENT", "key": "c1", "version": 1}}
        outcomes, tree = self.compare([corrected], {"remeasure_days_after_invalidating_change": "abc"})
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree)[-1], "BILLING_CORRECTION_RECORDED")

        package = copy.deepcopy(self.pkg)
        recommended_plan(package)[0]["validation"] = {"remeasure_days": "soon"}
        outcomes, tree = self.compare([self.base], package=package)
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree)[-1], "ECONOMIC_MEASUREMENT_RECORDED")

    def test_unreached_downstream_regeneration_input_does_not_fail(self):
        # Not ready: the run stops before validation and never needs the downstream regeneration.
        outcomes, tree = self.compare([{**self.base, "elapsed_days": 1}], {"remeasure_days_after_invalidating_change": "abc"})
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("VALIDATION_READY", events(tree))
        # Nothing applied: no active application at all.
        outcomes, _ = self.compare([{**self.base, "config": dict(self.source_config)}], {"remeasure_days_after_invalidating_change": "abc"})
        self.assertEqual(outcomes[0][0], "ok")

    def test_unreached_regeneration_input_does_not_fail(self):
        # An unhashable policy on a run that applies nothing never computes a regeneration or a validation id.
        outcomes, _ = self.compare([{**self.base, "config": dict(self.source_config)}], {"realized_ratio_min": "NaN"})
        self.assertEqual(outcomes[0][0], "ok")

    # --- recovery ---------------------------------------------------------------------------------------------------

    def test_failure_is_followed_by_the_same_recovery(self):
        broken = {**self.base, "rollback_requested": Untestable()}
        outcomes, _ = self.compare([broken, self.base, self.later(10), self.later(11, config={**self.base["config"], "warehouse_size": "SMALL"})])
        self.assertNotEqual(outcomes[0][0], "ok")
        self.assertEqual([o[0] for o in outcomes[1:]], ["ok", "ok", "ok"])


class EagerRaisingIsDetectedTests(unittest.TestCase):
    """Mutation check: a Reconciler that raised at once (instead of keeping the failure as ``halt``) would lose the
    entries decided before the failure, and the comparisons above would see it."""

    def test_raising_before_recording_changes_the_failed_run_tree(self):
        package = fixture_package()
        base = oengine.demo_observation(package)
        original = reconciler.run

        def eager(context, analysis, governance):
            result = original(context, analysis, governance)
            if result.halt is not None:
                raise result.halt
            return result

        cases = [
            [{**base, "rollback_requested": Untestable()}],
            [base, {**base, "observed_at_utc": "2026-10-10T12:00:00Z", "recorded_at_utc": "2026-10-10T12:00:00Z",
                    "config": {**base["config"], "warehouse_size": "SMALL"}, "effective_time_quality": Unprintable()}],
            [{**base, "elapsed_days": "abc"}],
        ]
        for observations in cases:
            with self.subTest(observations=len(observations)):
                old = run_sequence(oengine, package, observations)
                with mock.patch.object(engine.reconciler, "run", eager):
                    new = run_sequence(engine, package, observations)
                self.assertEqual(new[0], old[0], "the exception itself is the same")
                self.assertNotEqual(new[1], old[1], "but the ledger it leaves is not")
                self.assertEqual(run_sequence(engine, package, observations), old)


if __name__ == "__main__":
    unittest.main()
