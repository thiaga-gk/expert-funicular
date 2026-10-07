"""Failed runs through the extracted Reconciler: the stored exception and half-written ledger of the single-file release.

The Reconciler decides every lifecycle event of a run before the engine records any of them, so a failure inside it
must still surface after exactly the ledger entries (and files) the single-file engine had written when it raised:
matching failures right after the evidence file, a scenario-scope failure before any lifecycle entry, a failure in the
middle of the lifecycle after the entries decided before it, a regeneration request after every lifecycle entry, the
readiness check before the validation episode, and the downstream regeneration after the measurement. Each case runs
the engine over its observations into one directory and compares the outcomes and the output tree (files and
directories, compact) with the stored expectation; the events at the failure position are asserted in the test.
"""
import copy
import json
import re
import unittest
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Unprintable, Untestable, compact_tree, correction, events, files, fixture_package, migration_scenario_id, run_sequence  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402

MIGRATION = migration_scenario_id(fixture_package())


def two_step_package():
    return fixture_package("two-ready-steps")


def recommended_plan(package):
    sid = package["recommended_plan"]["scenario_id"]
    return next(s for s in package["scenarios"] if s["scenario_id"] == sid)["execution_plan"]


def stable_message(text):
    """"'<' not supported between instances of 'int' and 'str'" names the two types in set-iteration order, which varies
    with the string hash seed: the pair, sorted."""
    return re.sub(r"between instances of '(\w+)' and '(\w+)'", lambda m: "between instances of '%s' and '%s'" % tuple(sorted(m.groups())), text)


class ReconcilerFailedRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)
        cls.source_config = package_config(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def compare(self, observations, policy=None, package=None, label=None, gate=True):
        """Run *observations* and check the outcomes and the compact output tree against the stored expectation; returns
        ``(outcomes, tree)``. ``gate=False`` opens the loader's package gate for a package the Publisher's schema refuses;
        the observation gate is always open here (the loader refuses a flag with no truth value or an unprintable value
        before anything is written, see ``tests/unit/realizer/config``; these cases pin what the Reconciler leaves behind)."""
        package = self.pkg if package is None else package
        outcomes, run_tree = run_sequence(package, observations, policy, gate=gate, observation_gate=False)
        outcomes = [(kind, stable_message(text)) for kind, text in outcomes]
        self.checks = getattr(self, "checks", 0) + 1
        expect.check(self, label or f"run{self.checks}", (outcomes, compact_tree(run_tree)))
        return outcomes, run_tree

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
                outcomes, tree = self.compare([self.base], package=package, label=label, gate=False)
                self.assert_failed(outcomes)
                self.assertEqual(events(tree), ["REALIZATION_CREATED"])
                self.assertEqual(len(files(tree, "evidence")), 1)

    def test_the_package_gate_refuses_those_malformed_plans_before_anything_is_written(self):
        for label, corrupt in self.malformed_plans().items():
            with self.subTest(case=label):
                package = copy.deepcopy(self.pkg)
                corrupt(package)
                outcomes, tree = run_sequence(package, [self.base])
                self.assert_failed(outcomes, kind="ValueError")
                self.assertEqual(tree, {})

    def test_matching_failure_for_an_applied_scenario_other_than_the_recommended_one(self):
        package = copy.deepcopy(self.pkg)
        next(s for s in package["scenarios"] if s["scenario_id"] == MIGRATION)["execution_plan"][1]["step"] = "x"
        migrated = {**self.base, "applied_scenario_id": MIGRATION, "config": {**self.source_config, "warehouse_type": "SERVERLESS"}}
        outcomes, tree = self.compare([migrated], package=package, label="migration step", gate=False)
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED"])

    # --- scenario scope: fails before any lifecycle entry -----------------------------------------------------------

    def test_scenario_scope_failure_records_nothing_after_the_evidence(self):
        observation = canonical.load_scenario("GR-019")["runs"][0]["observation"]
        # the applied steps' invalidated knobs are merged into one set and sorted: an int among the names cannot be sorted
        for label, corrupt in (("mixed types", lambda invalidates: [*invalidates, 1]), ("unhashable", lambda invalidates: [{"knob": "max_clusters"}])):
            with self.subTest(case=label):
                package = two_step_package()
                step = recommended_plan(package)[0]
                step["invalidates"] = corrupt(step["invalidates"])
                outcomes, tree = self.compare([observation], package=package, label=label, gate=False)
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
                outcomes, tree = self.compare([self.base, drifted], label=label)
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
                outcomes, tree = self.compare([{**self.base, **patch}], label=label)
                self.assert_failed(outcomes, kind="ValueError")
                self.assertEqual(events(tree)[-1], last)
                self.assertIn("APPLICATION_OBSERVED", events(tree))

    # --- the regeneration request ends the lifecycle: fails after every lifecycle entry ----------------------------

    def test_terminal_regeneration_failure_leaves_every_lifecycle_entry_and_no_rerun_file(self):
        unhashable_policy = {"realized_ratio_min": "NaN"}   # the policy snapshot id cannot be computed
        drifted = self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, replacement_package_id="PKG-AAAAAAAAAAAAAAAAAAAAAAAA")
        outcomes, tree = self.compare([self.base, drifted], unhashable_policy, label="drift and supersession")
        self.assert_failed(outcomes, 0, kind="ValueError")   # the first run fails at its validation episode
        self.assert_failed(outcomes, 1, kind="ValueError")
        # (the second run's intent is the first run's entry again, on the same episode, so it is suppressed)
        self.assertEqual(events(tree)[-3:], ["DRIFT_DETECTED", "SOURCE_PACKAGE_SUPERSEDED", "REPLACEMENT_PACKAGE_LINKED"])
        self.assertNotIn("REGENERATION_REQUESTED", events(tree))
        self.assertEqual(files(tree, "regeneration"), [])

        contrary = {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}}
        outcomes, tree = self.compare([contrary], unhashable_policy, label="contrary")
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree), ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED"])
        self.assertEqual(files(tree, "regeneration"), [])

    # --- the downstream regeneration: fails after the measurement ---------------------------------------------------

    def test_downstream_regeneration_failure_leaves_the_measurement(self):
        outcomes, tree = self.compare([self.base], {"remeasure_days_after_invalidating_change": "abc"}, label="policy remeasure days")
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree)[-1], "ECONOMIC_MEASUREMENT_RECORDED")
        self.assertEqual(files(tree, "regeneration"), [])

        corrected = {**self.base, "correction": correction(version=1)}
        outcomes, tree = self.compare([corrected], {"remeasure_days_after_invalidating_change": "abc"}, label="correction")
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree)[-1], "BILLING_CORRECTION_RECORDED")

        package = copy.deepcopy(self.pkg)
        recommended_plan(package)[0]["validation"] = {"remeasure_days": "soon"}
        outcomes, tree = self.compare([self.base], package=package, label="step remeasure days", gate=False)
        self.assert_failed(outcomes, kind="ValueError")
        self.assertEqual(events(tree)[-1], "ECONOMIC_MEASUREMENT_RECORDED")

    def test_unreached_downstream_regeneration_input_does_not_fail(self):
        # Not ready: the run stops before validation and never needs the downstream regeneration.
        outcomes, tree = self.compare([{**self.base, "elapsed_days": 1}], {"remeasure_days_after_invalidating_change": "abc"}, label="not ready")
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("VALIDATION_READY", events(tree))
        # Nothing applied: no active application at all.
        outcomes, _ = self.compare([{**self.base, "config": dict(self.source_config)}], {"remeasure_days_after_invalidating_change": "abc"}, label="nothing applied")
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
        base = demo_observation(package)
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
        for index, observations in enumerate(cases):
            with self.subTest(observations=len(observations)):
                normal = run_sequence(package, observations, observation_gate=False)
                expect.check(self, f"case{index}", (normal[0], compact_tree(normal[1])))
                with mock.patch.object(reconciler, "run", eager):
                    mutated = run_sequence(package, observations, observation_gate=False)
                self.assertEqual(mutated[0], normal[0], "the exception itself is the same")
                self.assertNotEqual(mutated[1], normal[1], "but the ledger it leaves is not")
                self.assertEqual(run_sequence(package, observations, observation_gate=False), normal)


if __name__ == "__main__":
    unittest.main()
