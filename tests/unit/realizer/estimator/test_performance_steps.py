"""Performance and capacity steps (src/realizer/estimator/performance.py).

A step whose role is PERFORMANCE (a capacity add priced as a cost increase) or CAPACITY (a cut priced at zero) is not a
saving: it is judged on queueing and p95 before and after the change, its cost change is reported signed and never
counted as savings, and nothing is claimed.

- PERFORMANCE_VALIDATED: the queued share fell to at most half its pre-change value or to the safe level, and p95 did not
  rise past the canary rule; flagged for review when the cost increase exceeds 1.25 x the modeled increase.
- CAPACITY_VALIDATED: the queued share stayed at or below the safe level, or did not rise.
- Otherwise MONITORING with the reason and re-optimization recommended; without guard evidence MONITORING, unverified.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.projector import project_observation
from realizer.realizer import realize

ADD = world.package("capacity-add-scoped")  # wh-demo-008: max clusters 2 -> 5, PERFORMANCE, modeled -15,628.48/yr
CUT = world.package("capacity-cut-scoped")  # wh-demo-006: max clusters 4 -> 2, COST_GUARDRAIL, priced 0


def run(package, observation):
    with tempfile.TemporaryDirectory() as tmp:
        return realize(package, observation, Path(tmp) / "out").to_dict()


def with_queue(observation, pre, post):
    obs = copy.deepcopy(observation)
    obs["performance"]["guards"]["pre"]["queued_query_pct"] = pre
    obs["performance"]["guards"]["post"]["queued_query_pct"] = post
    return obs


class CapacityAddTests(unittest.TestCase):
    """The simulated world: queued share 9.7173 -> 0.7564 percent, p95 2,500 ms both, 3,795 cluster-minutes above the old
    cap in 28 days (+17,030.48/yr), billed +12,887.78/yr at the pre mix."""

    @classmethod
    def setUpClass(cls):
        cls.observation = project_observation(ADD, world.measure(ADD, world.world_spec("C08", {"max": 5}), 8))

    def test_the_queueing_fix_is_validated_and_its_cost_reported_not_claimed(self):
        snapshot = run(ADD, self.observation)
        econ = snapshot["economics"]
        self.assertEqual((snapshot["current_state"]["realization_state"], econ["classification_detail"]),
                         ("PERFORMANCE_VALIDATED", "PERFORMANCE_VALIDATED"))
        self.assertEqual(econ["performance"]["queued_query_pct"], {"pre": "9.7173", "post": "0.7564"})
        self.assertEqual(econ["cost_change"], {"annualized_cost_increase": "17030.48", "basis": "CLUSTER_LEVELS_ABOVE_OLD_CAP", "component": "total",
                                               "billed_annualized_cost_increase": "12887.78",
                                               "modeled_annual_cost_increase": "15628.48", "review_above": "19535.60"})
        self.assertIsNone(econ["successful_realized_savings"])
        self.assertEqual(snapshot["attribution"]["recommendation_attributed_realized_savings"], "0.00")
        self.assertIn("NOT_A_SAVINGS_STEP", snapshot["attribution"]["hard_disqualifiers"])

    def test_queueing_that_did_not_fall_far_enough_is_not_improved(self):
        snapshot = run(ADD, with_queue(self.observation, "9.7173", "6.0"))
        self.assertEqual((snapshot["current_state"]["realization_state"], snapshot["economics"]["classification_detail"]),
                         ("MONITORING", "PERFORMANCE_NOT_IMPROVED"))
        self.assertTrue(snapshot["economics"]["reoptimization_recommended"])

    def test_a_cost_far_above_the_model_is_flagged(self):
        obs = copy.deepcopy(self.observation)
        obs["measurement"]["levels"]["annualized_total_cost"] = "25000.00"
        econ = run(ADD, obs)["economics"]
        self.assertEqual((econ["realization_state"], econ["classification_detail"], econ["review_required"]),
                         ("PERFORMANCE_VALIDATED", "PERFORMANCE_VALIDATED_COST_ABOVE_MODEL", True))

    def test_without_guards_it_is_unverified(self):
        # A caller-supplied observation with verdicts but no guard rows: validated, but queueing cannot be judged.
        obs = copy.deepcopy(self.observation)
        obs["performance"] = {"normalized_p95_runtime_regression_pct": "0", "reliability_pass": True, "correctness_pass": True, "guards": None}
        econ = run(ADD, obs)["economics"]
        self.assertEqual((econ["realization_state"], econ["classification_detail"]), ("MONITORING", "PERFORMANCE_UNVERIFIED"))


class ZeroPricedCutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observation = project_observation(CUT, world.measure(CUT, world.world_spec("C06", {"max": 2}), 6))

    def test_a_cut_that_keeps_queueing_safe_is_capacity_validated(self):
        snapshot = run(CUT, with_queue(self.observation, "0.1", "0.3"))
        self.assertEqual(snapshot["current_state"]["realization_state"], "CAPACITY_VALIDATED")

    def test_a_cut_that_raises_queueing_past_the_safe_level_is_not(self):
        snapshot = run(CUT, with_queue(self.observation, "0.1", "2.0"))
        self.assertEqual((snapshot["current_state"]["realization_state"], snapshot["economics"]["classification_detail"]),
                         ("MONITORING", "CAPACITY_QUEUEING_REGRESSED"))


if __name__ == "__main__":
    unittest.main()
