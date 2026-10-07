"""The expected-savings denominator of an application (src/realizer/estimator/economics.py ``denominator``).

- A candidate-scope EXACT step: the candidate's isolated economics (``PUBLISHER_CANDIDATE``).
- A scenario-scope application of one step: that step's candidate economics; of several steps that form a prefix of the
  plan (its first authority-eligible steps): the running savings of the last applied step (``PUBLISHER_SCENARIO``),
  never the whole scenario's total; of steps that are not a prefix: no denominator (``UNAVAILABLE``).
- The component compared is the measurement's cost basis (DBU only, or DBU and modeled AWS); a caller-supplied
  observation is compared with the total (with the DBU range when it gives a DBU component and no AWS component,
  ``tests/unit/realizer/config/test_observation_components.py``), and a missing total is ``UNAVAILABLE``, never the DBU
  range in its place.
- The expected savings per component (DBU, AWS, total) are reported separately.
- A warehouse-type migration's range is informational: the realization is reported, never classified against it.
"""
import copy
import unittest
from decimal import Decimal

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.analysis import candidate_steps
from realizer.analyzer.collector.collector import demo_observation
from realizer.estimator.economics import calculate_economics
from realizer.governor.policy import RealizerPolicy

TWO = world.package("two-ready-steps")
PRO = world.package("autostop-scoped-pro")
FULL = world.package("inplace-size-canary")
POLICY = RealizerPolicy()


def observation(package, annual):
    """A caller-supplied observation whose 30-day realization annualizes to *annual*."""
    obs = demo_observation(package)
    period = Decimal(30)
    saving = (Decimal(annual) * period / Decimal(365)).quantize(Decimal("0.01"))
    obs["economics"].update({"counterfactual_old_cost": "100000.00", "observed_new_cost": str(Decimal("100000.00") - saving),
                             "period_days": 30, "expected_annual_savings_actual_scope": None})
    return obs


def scenario_app(package, steps, scenario_id=None, conformance="EXACT"):
    sid = scenario_id or package["recommended_plan"]["scenario_id"]
    return {"application_scope": "SCENARIO", "scenario_id": sid, "step": None, "implementation_conformance": conformance,
            "applied_step_refs": [{"scenario_id": sid, "step": s, "conformance": conformance} for s in steps]}


def candidate_app(package, step):
    sid = package["recommended_plan"]["scenario_id"]
    return {"application_scope": "CANDIDATE", "scenario_id": sid, "step": step, "implementation_conformance": "EXACT"}


def three_ready(package):
    """*package* with its third (pending) step made READY and priced, so the scenario total exceeds the two-step prefix."""
    p = copy.deepcopy(package)
    scenario = next(s for s in p["scenarios"] if s["scenario_id"] == p["recommended_plan"]["scenario_id"])
    third = [s for s in scenario["execution_plan"] if s["step_type"] == "CANDIDATE"][2]
    third["execution_state"] = "READY"
    third["projected_economics"]["running_savings_365d"]["total"] = {"low": 2000.0, "base": 60000.0, "high": 70000.0}
    scenario["portfolio_economics"]["savings_365d"]["total"] = {"low": 2000.0, "base": 60000.0, "high": 70000.0}
    return p


class ScenarioDenominatorTests(unittest.TestCase):
    def test_a_prefix_uses_the_running_savings_of_its_last_step(self):
        package = three_ready(TWO)
        econ = calculate_economics(package, scenario_app(package, [1, 2]), observation(package, 40000), POLICY, True)
        self.assertEqual((econ["expected_same_scope_annual_savings"], econ["denominator_source"]), ("38863.07", "PUBLISHER_SCENARIO"))
        self.assertEqual(econ["expected_range"], {"component": "total", "low": "1288.37", "base": "38863.07", "high": "49678.72"})
        self.assertEqual(econ["realization_state"], "REALIZED")

    def test_steps_that_are_not_a_prefix_have_no_denominator(self):
        package = three_ready(TWO)
        econ = calculate_economics(package, scenario_app(package, [1, 3]), observation(package, 40000), POLICY, True)
        self.assertEqual((econ["denominator_source"], econ["realization_state"]), ("UNAVAILABLE", "MONITORING"))

    def test_one_step_uses_its_candidate_economics(self):
        econ = calculate_economics(TWO, scenario_app(TWO, [2]), observation(TWO, 700), POLICY, True)
        self.assertEqual((econ["expected_same_scope_annual_savings"], econ["denominator_source"]), ("672.84", "PUBLISHER_CANDIDATE"))


class ComponentTests(unittest.TestCase):
    def test_expected_savings_are_reported_per_component(self):
        econ = calculate_economics(PRO, candidate_app(PRO, 1), observation(PRO, 9000), POLICY, True)
        self.assertEqual(econ["expected_components"], {"dbu": "5958.66", "aws": "3404.85", "total": "9363.51"})

    def test_a_missing_total_is_unavailable_not_the_dbu_range(self):
        package = copy.deepcopy(PRO)
        step = candidate_steps(package)[0]
        candidate = next(c for c in package["candidates"] if c["candidate_id"] == step["candidate_id"])
        candidate["independent_economics"]["savings_365d"]["total"] = None
        econ = calculate_economics(package, candidate_app(package, 1), observation(package, 9000), POLICY, True)
        self.assertEqual((econ["denominator_source"], econ["realization_state"]), ("UNAVAILABLE", "MONITORING"))
        live = observation(package, 9000)
        live["measurement"] = {"cost_basis": "DBU_ONLY"}
        econ = calculate_economics(package, candidate_app(package, 1), live, POLICY, True)
        self.assertEqual((econ["expected_same_scope_annual_savings"], econ["expected_range"]["component"]), ("5958.66", "dbu"))


class MigrationTests(unittest.TestCase):
    def test_a_migration_range_is_informational(self):
        migration = next(s["scenario_id"] for s in FULL["scenarios"] if s["scenario_type"] == "WAREHOUSE_TYPE_MIGRATION")
        econ = calculate_economics(FULL, scenario_app(FULL, [2], scenario_id=migration), observation(FULL, 30000), POLICY, True)
        self.assertEqual((econ["realization_state"], econ["classification_basis"], econ["classification_detail"]),
                         ("MONITORING", "INFORMATIONAL", "MIGRATION_RANGE_INFORMATIONAL"))
        self.assertEqual(econ["expected_same_scope_annual_savings"], "18901.04")
        self.assertIsNotNone(econ["realization_ratio"])


if __name__ == "__main__":
    unittest.main()
