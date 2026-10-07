"""Failed runs through the extracted Estimator: the exception and the half-written ledger of the single-file release.

The Estimator decides the counterfactual, the measurement and the billing correction before the engine records the
validation episode, so a failure inside it must still surface where the single-file engine raised it: after the
completed validation for the counterfactual eligibility, cost and id; after the counterfactual for the economics, the
quality gates, the attribution and the measurement id; after the measurement for the correction; and the downstream
regeneration after the measurement and the correction. Each case runs the engine over the observations into a fresh
directory and checks the outcomes and the whole output tree (the ledger events in order, the directories and a digest
of every file) against the stored expectation.

The mutation checks at the end prove the checks would notice an Estimator that raised at once, that read the
financial-quality gates before the economics, or that read them for unavailable economics.
"""
import copy
import unittest
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import (  # noqa: E402
    Opaque, Unprintable, Untestable, compact_tree, correction, events, files, fixture_package, run_sequence, with_quality, without_remeasure_window,
)
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.realization import RealizationEstimate  # noqa: E402
from realizer.estimator import estimator  # noqa: E402

VALIDATED = ["VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED"]


class EstimatorFailedRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def econ(self, **patch):
        return {**copy.deepcopy(self.base), "economics": {**self.base["economics"], **patch}}

    def compare(self, label, observations, policy=None, package=None):
        """Run *observations* and check the outcomes and the output tree against the stored expectation."""
        package = self.pkg if package is None else package
        # The observation gate is open: the loader refuses these observations before anything is written (see
        # ``tests/unit/realizer/config``); here they pin where the Estimator's facts raise.
        outcomes, tree = run_sequence(package, observations, policy, observation_gate=False)
        expect.check(self, label, (outcomes, compact_tree(tree)))
        return outcomes, tree

    def assert_fails_after(self, label, observation, last, kind, policy=None, package=None):
        outcomes, tree = self.compare(label, [observation], policy, package)
        self.assertEqual(outcomes[0][0], kind, outcomes)
        self.assertEqual(events(tree)[-1], last, events(tree))
        self.assertEqual(files(tree, "regeneration"), [])
        return tree

    # --- W3: counterfactual eligibility, cost and id fail after the completed validation ---------------------------

    def test_counterfactual_failure_leaves_the_validation_episode(self):
        cases = {
            "economics not a mapping": ({**self.base, "economics": 5}, "AttributeError"),
            "method unprintable": (self.econ(counterfactual_method=Unprintable()), "ValueError"),
            "cost not a number": (self.econ(counterfactual_old_cost="abc"), "InvalidOperation"),
            "cost not finite": (self.econ(counterfactual_old_cost="Infinity"), "ValueError"),
            "workload not serializable": ({**self.base, "workload": Opaque()}, "TypeError"),
        }
        for label, (observation, kind) in cases.items():
            with self.subTest(case=label):
                tree = self.assert_fails_after(label, observation, "VALIDATION_COMPLETED", kind)
                self.assertEqual(events(tree)[-3:], VALIDATED)

    # --- W4: economics, quality gates, attribution and measurement fail after the counterfactual ------------------

    def test_economics_failure_leaves_the_counterfactual(self):
        cases = {
            "new cost not a number": (self.econ(observed_new_cost="abc"), "InvalidOperation"),
            "period not finite": (self.econ(period_days="NaN"), "ValueError"),
            "expected not a number": (self.econ(expected_annual_savings_actual_scope="abc"), "InvalidOperation"),
            "aws component not a number": (self.econ(aws_cash_realized="abc"), "InvalidOperation"),
            "quality gate not finite": (with_quality(self.base, price_coverage_pct="NaN"), "ValueError"),
            "overlap not a number": ({**self.base, "overlap_count": "abc"}, "ValueError"),
            "correction not a mapping": ({**self.base, "correction": 5}, "AttributeError"),
            "correction type not serializable": ({**self.base, "correction": {"type": Opaque()}}, "TypeError"),
        }
        for label, (observation, kind) in cases.items():
            with self.subTest(case=label):
                self.assert_fails_after(label, observation, "COUNTERFACTUAL_RECORDED", kind)

    def test_economics_failure_without_a_counterfactual_cost_leaves_the_validation_episode(self):
        self.assert_fails_after("no cost", self.econ(counterfactual_old_cost=None, observed_new_cost="abc"), "VALIDATION_COMPLETED", "InvalidOperation")

    def test_economics_fail_before_the_quality_gates(self):
        # both malformed: the economics are computed first, so their exception is the one raised
        observation = with_quality(self.econ(observed_new_cost="abc"), attributable_spend_pct="NaN")
        self.assert_fails_after("economics first", observation, "COUNTERFACTUAL_RECORDED", "InvalidOperation")
        # and the gates before the attribution
        self.assert_fails_after("gates before attribution", {**with_quality(self.base, attributable_spend_pct="NaN"), "overlap_count": "abc"}, "COUNTERFACTUAL_RECORDED", "ValueError")

    # --- W5: the billing correction fails after the measurement -----------------------------------------------------

    def test_correction_failure_leaves_the_measurement(self):
        cases = {
            "corrects_entry_id not an RLE id": ({"corrects_entry_id": "bogus"}, "ValueError"),
            "corrects_entry_id truth untestable": ({"corrects_entry_id": Untestable()}, "ValueError"),
            "key truth untestable": ({"key": Untestable()}, "ValueError"),
            "reason not serializable": ({"reason": Opaque()}, "TypeError"),
        }
        for label, (correction, kind) in cases.items():
            with self.subTest(case=label):
                self.assert_fails_after(label, {**self.base, "correction": correction}, "ECONOMIC_MEASUREMENT_RECORDED", kind)

    # --- W6: the downstream regeneration fails after the measurement and the correction -----------------------------

    def test_downstream_regeneration_failure_leaves_the_measurement_and_the_correction(self):
        # the policy window is read only when the plan's REMEASURE step states none
        bad_window, plan = {"remeasure_days_after_invalidating_change": "abc"}, without_remeasure_window(self.pkg)
        self.assert_fails_after("measurement", self.base, "ECONOMIC_MEASUREMENT_RECORDED", "ValueError", bad_window, plan)
        self.assert_fails_after("correction", {**self.base, "correction": correction()}, "BILLING_CORRECTION_RECORDED", "ValueError", bad_window, plan)
        # without available economics, after the completed validation
        self.assert_fails_after("no economics", self.econ(counterfactual_old_cost=None), "VALIDATION_COMPLETED", "ValueError", bad_window, plan)

    # --- unreached input does not fail ------------------------------------------------------------------------------

    def test_unavailable_economics_never_read_the_measurement_inputs(self):
        observation = {**with_quality(self.econ(counterfactual_old_cost=None), attributable_spend_pct="NaN"), "overlap_count": "abc",
                       "correction": {"corrects_entry_id": Untestable()}}
        outcomes, tree = self.compare("unavailable", [observation])
        self.assertEqual(outcomes[0][0], "ok")
        self.assertNotIn("COUNTERFACTUAL_RECORDED", events(tree))
        self.assertNotIn("ECONOMIC_MEASUREMENT_RECORDED", events(tree))

    def test_unvalidated_runs_never_read_the_economics(self):
        broken = {"economics": 5, "overlap_count": "abc", "correction": 5}
        outcomes, _ = self.compare("not ready", [{**self.base, "elapsed_days": 1, **broken}])
        self.assertEqual(outcomes[0][0], "ok")
        outcomes, _ = self.compare("nothing applied", [{**self.base, "config": package_config(self.pkg), **broken}])
        self.assertEqual(outcomes[0][0], "ok")

    # --- recovery ---------------------------------------------------------------------------------------------------

    def test_failure_is_followed_by_the_same_recovery(self):
        later = {**self.base, "observed_at_utc": "2026-10-10T12:00:00Z", "recorded_at_utc": "2026-10-10T12:00:00Z"}
        outcomes, _ = self.compare("recovery", [self.econ(observed_new_cost="abc"), self.base, later, {**later, "correction": correction()}])
        self.assertNotEqual(outcomes[0][0], "ok")
        self.assertEqual([o[0] for o in outcomes[1:]], ["ok", "ok", "ok"])


class EstimatorMutationTests(unittest.TestCase):
    """Mutation checks: an Estimator that raised at once, or read the quality gates eagerly, is caught."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def test_raising_before_recording_changes_the_failed_run_tree(self):
        original = estimator.run

        def eager(*args):
            result = original(*args)
            if result.halt is not None:
                raise result.halt
            return result

        cases = [[{**self.base, "economics": 5}], [{**self.base, "overlap_count": "abc"}], [{**self.base, "correction": {"corrects_entry_id": Untestable()}}]]
        for index, observations in enumerate(cases):
            with self.subTest(observations=observations):
                normal = run_sequence(self.pkg, observations, observation_gate=False)
                expect.check(self, str(index), (normal[0], compact_tree(normal[1])))
                with mock.patch.object(estimator, "run", eager):
                    mutated = run_sequence(self.pkg, observations, observation_gate=False)
                self.assertEqual(mutated[0], normal[0], "the exception itself is the same")
                self.assertNotEqual(mutated[1], normal[1], "but the ledger it leaves is not")

    def test_reading_the_quality_gates_eagerly_is_caught(self):
        original = estimator.run

        def eager_gates(context, analysis, governance, reconciliation):
            if reconciliation.validation_due:
                try:
                    governance.economic_quality.get()
                except Exception as exc:  # noqa: BLE001 - a halt, as the real stage would keep it
                    return RealizationEstimate(halt=exc)
            return original(context, analysis, governance, reconciliation)

        econ = self.base["economics"]
        cases = [
            # unavailable economics: the engine never reads the gates and succeeds
            [with_quality({**self.base, "economics": {**econ, "counterfactual_old_cost": None}}, attributable_spend_pct="NaN")],
            # both malformed: the engine raises the economics' exception
            [with_quality({**self.base, "economics": {**econ, "observed_new_cost": "abc"}}, attributable_spend_pct="NaN")],
        ]
        for index, observations in enumerate(cases):
            with self.subTest(observations=observations):
                normal = run_sequence(self.pkg, observations, observation_gate=False)
                expect.check(self, str(index), (normal[0], compact_tree(normal[1])))
                with mock.patch.object(estimator, "run", eager_gates):
                    mutated = run_sequence(self.pkg, observations, observation_gate=False)
                self.assertNotEqual(mutated, normal)


if __name__ == "__main__":
    unittest.main()
