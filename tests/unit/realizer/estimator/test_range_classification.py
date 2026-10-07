"""Realization classified against the package's published range (src/realizer/estimator/economics.py).

A replay counterfactual measures the full mechanical effect of a change, the track of the published high, so comparing
it with the base always reads high. The realized run-rate is classified against the step's published range: at or
above its low REALIZED, below it UNDER_REALIZED (re-optimization below half the low), far above the high (more than
1.25 x) flagged for review; the ratio against the base is still reported. The comparison is like for like: the total
range when the measurement has a modeled AWS price, the DBU range when it is DBU only.
"""
import copy
import unittest
from decimal import Decimal

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.projector import project_observation
from realizer.estimator.economics import calculate_economics, published_range
from realizer.governor.policy import RealizerPolicy

PRO = world.package("autostop-scoped-pro")
STEP = next(s for sc in PRO["scenarios"] if sc["scenario_id"] == PRO["recommended_plan"]["scenario_id"] for s in sc["execution_plan"] if s["step_type"] == "CANDIDATE")
APP = {"implementation_conformance": "EXACT", "application_scope": "CANDIDATE", "step": STEP["step"], "scenario_id": PRO["recommended_plan"]["scenario_id"]}


def measured(aws=True):
    evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13, aws_registry=None if aws else {})
    return project_observation(PRO, evidence)


def with_realized(observation, annual):
    """The observation with its 28-day realized saving set to reach *annual* a year."""
    o = copy.deepcopy(observation)
    period = Decimal(o["economics"]["period_days"])
    o["economics"]["counterfactual_old_cost"] = str((Decimal(o["economics"]["observed_new_cost"]) + Decimal(annual) * period / 365).quantize(Decimal("0.000001")))
    return o


def classify(observation):
    return calculate_economics(PRO, APP, observation, RealizerPolicy(), True, True)


class RangeClassificationTests(unittest.TestCase):
    def test_the_package_range(self):
        savings = next(c for c in PRO["candidates"] if c["candidate_id"] == STEP["candidate_id"])["independent_economics"]["savings_365d"]
        self.assertEqual(published_range(savings["dbu"]), (Decimal("3972.44"), Decimal("5958.66"), Decimal("7944.88")))
        self.assertEqual(published_range(savings["total"]), (Decimal("6242.34"), Decimal("9363.51"), Decimal("12484.69")))

    def test_a_measured_change_is_classified_against_the_total_range(self):
        e = classify(measured())
        # 959.06 in 28 days (DBU 609.61 + modeled AWS 349.44, costs in cents): 12,501.90 a year, at the published high 12,484.69.
        self.assertEqual(e["annualized_realized_savings"], "12501.90")
        self.assertEqual(e["expected_range"], {"component": "total", "low": "6242.34", "base": "9363.51", "high": "12484.69"})
        self.assertEqual((e["realization_state"], e["classification_detail"], e["classification_basis"], e["review_required"]),
                         ("REALIZED", "REALIZED", "PUBLISHED_RANGE", False))
        self.assertEqual(Decimal(e["realization_ratio"]).quantize(Decimal("0.01")), Decimal("1.34"))  # against the base, reported

    def test_dbu_only_is_classified_against_the_dbu_range(self):
        e = classify(measured(aws=False))
        self.assertEqual(e["expected_range"]["component"], "dbu")
        self.assertEqual(e["expected_same_scope_annual_savings"], "5958.66")
        self.assertEqual(e["annualized_realized_savings"], "7946.70")  # 609.62 (costs in cents) x 365 / 28
        self.assertEqual(e["realization_state"], "REALIZED")

    def test_low_half_low_and_far_above_high(self):
        base = measured()
        # low 6,242.34; half the low 3,121.17; 1.25 x high 15,605.86
        cases = {"6243": ("REALIZED", "REALIZED"), "6200": ("UNDER_REALIZED", "UNDER_REALIZED"), "3200": ("UNDER_REALIZED", "UNDER_REALIZED"),
                 "3000": ("UNDER_REALIZED", "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED"), "15600": ("REALIZED", "REALIZED"),
                 "15700": ("REALIZED", "REALIZED_ABOVE_RANGE_REVIEW")}
        for annual, expected in cases.items():
            with self.subTest(annual=annual):
                e = classify(with_realized(base, annual))
                self.assertEqual((e["realization_state"], e["classification_detail"]), expected)
                self.assertEqual(e["review_required"], expected[1] == "REALIZED_ABOVE_RANGE_REVIEW")

    def test_without_a_range_the_ratio_policy_still_applies(self):
        o = with_realized(measured(), "9000")
        o["economics"]["expected_annual_savings_actual_scope"] = "10000"  # a re-estimate: one number, no range
        e = classify(o)
        self.assertEqual((e["classification_basis"], e["denominator_source"], e["realization_state"]), ("RATIO_POLICY", "REESTIMATED_APPLIED_SCOPE", "REALIZED"))
        self.assertIsNone(e["expected_range"])


if __name__ == "__main__":
    unittest.main()
