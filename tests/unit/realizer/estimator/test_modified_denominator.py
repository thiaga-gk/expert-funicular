"""The denominator of a MODIFIED auto-stop (src/realizer/estimator/economics.py ``option_grid_savings``).

The package holds the Publisher's annual net cluster-hours for every auto-stop value it evaluated
(``auto_stop_annual_cluster_hours_by_value``). The applied value's expected savings are the step's published range
scaled by the applied value's net cluster-hours over the recommended value's, per component; a value between two grid
values is interpolated linearly, a value outside the grid has no denominator. Classification is then against that range,
exactly as for an exact step.
"""
import unittest
from decimal import Decimal

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.collector.collector import demo_observation
from realizer.estimator.economics import calculate_economics, option_grid_savings
from realizer.governor.policy import RealizerPolicy
from realizer.analyzer.analysis import candidate_steps

PRO = world.package("autostop-scoped-pro")  # wh-demo-013: auto-stop 30 -> 10, total 6,242.34 / 9,363.51 / 12,484.69
STEP = candidate_steps(PRO)[0]


def modified_app(value):
    return {"application_scope": "CANDIDATE", "scenario_id": PRO["recommended_plan"]["scenario_id"], "step": STEP["step"],
            "knob": "auto_stop_minutes", "step_role": "SAVINGS", "implementation_conformance": "MODIFIED",
            "modification_relation": "LESS_AGGRESSIVE", "observed_value": value}


def observation(annual):
    obs = demo_observation(PRO)
    saving = (Decimal(annual) * 30 / Decimal(365)).quantize(Decimal("0.01"))
    obs["economics"].update({"counterfactual_old_cost": "100000.00", "observed_new_cost": str(Decimal("100000.00") - saving),
                             "period_days": 30, "expected_annual_savings_actual_scope": None})
    return obs


class GridTests(unittest.TestCase):
    def test_a_grid_value_scales_the_published_range(self):
        # 15 minutes saves 909.4 of the 1,212.6 net cluster-hours 10 minutes saves.
        total = option_grid_savings(PRO, STEP, 15)["total"]
        self.assertEqual({k: round(total[k], 2) for k in ("low", "base", "high")},
                         {"low": Decimal("4681.50"), "base": Decimal("7022.25"), "high": Decimal("9363.00")})

    def test_a_value_between_grid_values_is_interpolated(self):
        # 12 minutes: 1,212.6 - 2/5 x (1,212.6 - 909.4) = 1,091.32 net cluster-hours.
        self.assertEqual(round(option_grid_savings(PRO, STEP, 12)["total"]["base"], 2), Decimal("8427.00"))

    def test_a_value_outside_the_grid_has_none(self):
        self.assertIsNone(option_grid_savings(PRO, STEP, 7))

    def test_a_step_without_a_recommended_value_has_none(self):
        self.assertIsNone(option_grid_savings(PRO, {**STEP, "recommended_value": None}, 15))


class ClassificationTests(unittest.TestCase):
    def test_ge05_modified_15_is_realized_against_the_scaled_range(self):
        econ = calculate_economics(PRO, modified_app(15), observation(9376), RealizerPolicy(), True)
        self.assertEqual((econ["denominator_source"], econ["denominator_method"]), ("REESTIMATED_APPLIED_SCOPE", "PACKAGE_OPTION_GRID"))
        self.assertEqual(econ["expected_range"], {"component": "total", "low": "4681.50", "base": "7022.25", "high": "9363.00"})
        self.assertEqual((econ["realization_state"], econ["classification_basis"]), ("REALIZED", "PUBLISHED_RANGE"))

    def test_outside_the_grid_stays_monitoring(self):
        econ = calculate_economics(PRO, modified_app(7), observation(9376), RealizerPolicy(), True)
        self.assertEqual((econ["denominator_source"], econ["realization_state"]), ("UNAVAILABLE", "MONITORING"))


if __name__ == "__main__":
    unittest.main()
