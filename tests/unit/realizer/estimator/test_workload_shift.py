"""A workload that shifts after the change (src/realizer/estimator/economics.py, src/realizer/analyzer/projector.py).

The replay counterfactual replays the post-change days with the old value, so its saving is real at the workload the
warehouse actually ran; only the package's expectation was priced at the old workload. When the post-change query
volume per day is outside 0.75 to 1.25 of the pre-change window's, an auto-stop replay is still measured and reported
(the saving, its run-rate and the volume ratio), as MONITORING with the detail WORKLOAD_SHIFT: never classified against
the package's range, never under-realized, never a re-optimization; the denominator stays the package's and the claim
follows the usual gates. A step judged on queueing and p95 (no replay) still waits for a comparable workload, and the
readiness check states the shift as its reason.

The simulated world: the package fixture ``autostop-scoped-pro`` (wh-demo-013, auto-stop 30 -> 10, published total
range 6,242.34 / 9,363.51 / 12,484.69 a year), its workload (a query every 45 minutes on weekdays 08:00-18:00) changed
from the change day on.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.projector import project_observation
from realizer.estimator.economics import calculate_economics
from realizer.governor.policy import RealizerPolicy, readiness
from realizer.realizer import realize

PRO = world.package("autostop-scoped-pro")
STEP = next(s for sc in PRO["scenarios"] if sc["scenario_id"] == PRO["recommended_plan"]["scenario_id"] for s in sc["execution_plan"] if s["step_type"] == "CANDIDATE")
APP = {"implementation_conformance": "EXACT", "application_scope": "CANDIDATE", "step": STEP["step"], "scenario_id": PRO["recommended_plan"]["scenario_id"]}
ADD = world.package("capacity-add-scoped")  # wh-demo-008: max clusters 2 -> 5, judged on queueing and p95


def shifted_work(scenario, factor):
    """The fleet workload of *scenario* until the change day, then scaled by *factor*."""
    return {"work": world.shifted_work(next(s for s in world.demo.DEMO_FLEET if s["scenario"] == scenario)["work"], factor)}


def measured(factor):
    extra = None if factor == 1 else shifted_work("C13", factor)
    return project_observation(PRO, world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}, extra=extra), 13))


def classify(observation):
    return calculate_economics(PRO, APP, observation, RealizerPolicy(), True, True)


def run(package, observation):
    with tempfile.TemporaryDirectory() as tmp:
        return realize(package, observation, Path(tmp) / "out").to_dict()


class AutoStopReplayUnderAShiftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.half, cls.double, cls.in_band = measured(0.5), measured(2), measured(0.8)

    def test_the_shift_is_measured_not_held(self):
        for obs, ratio in ((self.half, "0.5"), (self.double, "1.9286")):
            with self.subTest(ratio=ratio):
                self.assertEqual(obs["measurement"]["volume_ratio"], ratio)
                self.assertIs(obs["representative_regime"], True)
                self.assertTrue(readiness(obs, RealizerPolicy())["ready"])

    def test_a_halved_workload_is_reported_as_a_workload_shift(self):
        e = classify(self.half)
        # 479.53 in 28 days (DBU 304.81 + modeled AWS 174.72): 6,251.02 a year. Not classified against the published range:
        # the package's expectation was priced at twice this volume.
        self.assertEqual((e["realized_savings_period"], e["annualized_realized_savings"]), ("479.53", "6251.02"))
        self.assertEqual((e["realization_state"], e["classification_detail"], e["classification_basis"]),
                         ("MONITORING", "WORKLOAD_SHIFT", "INFORMATIONAL"))
        self.assertEqual((e["reoptimization_recommended"], e["review_required"]), (False, False))
        self.assertEqual((e["expected_same_scope_annual_savings"], e["denominator_source"]), ("9363.51", "PUBLISHER_CANDIDATE"))
        self.assertEqual(e["successful_realized_savings"], "479.53")
        self.assertEqual(e["measurement"]["volume_ratio"], "0.5")

    def test_a_doubled_workload_is_never_under_realized(self):
        e = classify(self.double)
        # 558.31 in 28 days, 7,277.97 a year at 1.93 x the volume: more traffic leaves fewer idle gaps to cut, so scaling
        # the expectation by the volume ratio (9,363.51 x 1.9286 = 18,058.47) would have called it under-realized.
        self.assertEqual((e["realized_savings_period"], e["annualized_realized_savings"]), ("558.31", "7277.97"))
        self.assertEqual((e["realization_state"], e["classification_detail"], e["reoptimization_recommended"]),
                         ("MONITORING", "WORKLOAD_SHIFT", False))

    def test_a_shortfall_that_would_ask_for_reoptimization_does_not_under_a_shift(self):
        obs = copy.deepcopy(self.half)
        # 2,000 a year, below half the low (3,121.17): UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED without the shift.
        obs["economics"]["counterfactual_old_cost"] = str(round(float(obs["economics"]["observed_new_cost"]) + 2000 * 28 / 365, 6))
        e = classify(obs)
        self.assertEqual((e["realization_state"], e["classification_detail"], e["reoptimization_recommended"]),
                         ("MONITORING", "WORKLOAD_SHIFT", False))
        obs["measurement"]["volume_ratio"] = "1"
        self.assertEqual(classify(obs)["classification_detail"], "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED")

    def test_inside_the_band_nothing_changes(self):
        e = classify(self.in_band)
        self.assertEqual(self.in_band["measurement"]["volume_ratio"], "0.7857")
        self.assertEqual((e["annualized_realized_savings"], e["realization_state"], e["classification_detail"]),
                         ("9823.06", "REALIZED", "REALIZED"))

    def test_the_realization_package_reports_and_claims_it(self):
        snapshot = run(PRO, self.half)
        self.assertEqual((snapshot["current_state"]["realization_state"], snapshot["current_state"]["validation_state"]),
                         ("MONITORING", "PASSED"))
        self.assertEqual(snapshot["economics"]["classification_detail"], "WORKLOAD_SHIFT")
        self.assertEqual((snapshot["attribution"]["attribution_claim_status"], snapshot["attribution"]["recommendation_attributed_realized_savings"]),
                         ("CLAIMABLE", "479.53"))
        self.assertEqual(snapshot["regeneration"]["status"], "NOT_REQUIRED")


class GuardJudgedStepUnderAShiftTests(unittest.TestCase):
    """A capacity add is judged on queueing: at twice the traffic the comparison is not like for like, so it waits."""

    @classmethod
    def setUpClass(cls):
        cls.observation = project_observation(ADD, world.measure(ADD, world.world_spec("C08", {"max": 5}, extra=shifted_work("C08", 2)), 8))

    def test_it_waits_and_states_the_shift(self):
        self.assertIs(self.observation["representative_regime"], False)
        check = readiness(self.observation, RealizerPolicy())["checks"]["representative_regime"]
        self.assertEqual((check["pass"], check["reason"]), (False, "WORKLOAD_SHIFT"))
        self.assertEqual(check["volume_ratio"], self.observation["measurement"]["volume_ratio"])
        snapshot = run(ADD, self.observation)
        self.assertEqual(snapshot["current_state"]["validation_state"], "NOT_READY")

    def test_the_wait_is_logged_with_its_reason(self):
        with self.assertLogs("realizer", level="INFO") as logs:
            run(ADD, self.observation)
        self.assertTrue(any("realizer.not_ready" in line and "WORKLOAD_SHIFT" in line for line in logs.output), logs.output)


class ReadinessReasonTests(unittest.TestCase):
    def test_no_reason_without_a_measured_shift(self):
        obs = copy.deepcopy(measured(1))
        check = readiness(obs, RealizerPolicy())["checks"]["representative_regime"]
        self.assertEqual((check["pass"], check.get("reason")), (True, None))
        obs["representative_regime"] = False
        obs["measurement"] = None  # a caller's flag without a measured volume: not ready, no stated shift
        check = readiness(obs, RealizerPolicy())["checks"]["representative_regime"]
        self.assertEqual((check["pass"], check.get("reason")), (False, None))


if __name__ == "__main__":
    unittest.main()
