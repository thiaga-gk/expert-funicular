"""A warehouse-type migration is observed, never claimed (``realizer.analyzer.migration_observation``).

Worked example (a simulated world): wh-demo-001 (Finance Planning, Pro MEDIUM, auto-stop 0) moved to Serverless on
2026-10-08 14:32 UTC and was measured on 2026-11-07. Per 28 days its billed DBU cost rose from 8,844.97 to 11,330.60
(+2,485.63) while its modeled AWS cost, 40,320 cluster-minutes at 7.488 per cluster-hour (5,031.94), fell to a true zero
(Serverless has no customer cloud compute): the observed reduction is 2,546.30 per 28 days. It is reported against the
package's informational migration range and attributed to nobody.
"""
import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer.analyzer import measurement as m
from realizer.analyzer.migration_observation import (
    MIGRATION_METHOD, NO_REVERSE_MIGRATION_MODEL, is_migration_observed, window_aws_hourly,
)
from realizer.analyzer.projector import billed_change, project_observation, volume_ratio, window_summary
from realizer.config.loader import validate_observation
from realizer.contracts import realization as rc
from realizer.contracts import report
from realizer.realizer import realize

PACKAGE = world.package("migration-priced")
MIGRATED = {"type": "SERVERLESS"}


def evidence(after=MIGRATED):
    return world.measure(PACKAGE, world.world_spec("C01", after), 1)


def realized(obs):
    with tempfile.TemporaryDirectory() as tmp:
        return realize(PACKAGE, obs, Path(tmp)).to_dict()


class AwsPriceTests(unittest.TestCase):
    def test_a_serverless_window_is_a_true_zero_and_an_unknown_price_stays_unknown(self):
        self.assertEqual(window_aws_hourly({"status": "MODELED", "cluster_hourly_price": 7.488}), Decimal("7.488"))
        self.assertEqual(window_aws_hourly({"status": "NOT_CUSTOMER_BILLED", "cluster_hourly_price": 0.0}), Decimal("0"))
        self.assertEqual(window_aws_hourly({"status": "NOT_CUSTOMER_BILLED", "cluster_hourly_price": None}), Decimal("0"))
        self.assertIsNone(window_aws_hourly({"status": "UNAVAILABLE", "cluster_hourly_price": None, "blocker": "NO_PRICE"}))
        self.assertIsNone(window_aws_hourly({"status": "MODELED", "cluster_hourly_price": None}))
        self.assertIsNone(window_aws_hourly(None))


class BilledChangeTests(unittest.TestCase):
    """A Serverless side is a zero AWS price in every billed change, a migration or not."""

    def change(self, mutate):
        ev = copy.deepcopy(evidence())
        mutate(ev["aws"])
        plan = m.plan_measurement(PACKAGE, ev["epochs"], ev["billing"]["settled_through"])
        pre = window_summary(ev["windows"]["pre"], ev["daily_cost"])
        post = window_summary(ev["windows"]["post"], ev["daily_cost"])
        return billed_change(plan, ev, volume_ratio(pre, post), "USD")

    def test_pro_to_serverless_has_the_aws_component_and_the_total(self):
        change = self.change(lambda aws: None)
        self.assertEqual(Decimal(change["dbu"]), Decimal("2485.63"))
        self.assertEqual(Decimal(change["aws_modeled"]), Decimal("-5031.94"))  # 0 - 40,320 / 60 x 7.488
        self.assertEqual(Decimal(change["total"]), Decimal("-2546.31"))  # the sum before rounding to cents
        self.assertEqual(Decimal(change["annualized_total"]), Decimal("-33192.95"))  # x 365 / 28

    def test_serverless_to_pro_has_the_total_too(self):
        def swap(aws):
            aws["pre"], aws["post"] = aws["post"], aws["pre"]
        change = self.change(swap)
        self.assertEqual(Decimal(change["aws_modeled"]), Decimal("5031.94"))  # 40,320 / 60 x 7.488 - 0
        self.assertEqual(Decimal(change["total"]), Decimal("7517.56"))

    def test_an_unavailable_price_leaves_the_aws_component_and_the_total_null(self):
        def lose(aws):
            aws["pre"] = {"status": "UNAVAILABLE", "cluster_hourly_price": None, "currency": "USD", "region": "us-east-1", "blocker": "NO_PRICE"}
        change = self.change(lose)
        self.assertEqual(Decimal(change["dbu"]), Decimal("2485.63"))
        self.assertEqual((change["aws_modeled"], change["total"], change["annualized_total"]), (None, None, None))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.obs = project_observation(PACKAGE, evidence())

    def test_the_observed_delta_is_the_economics_and_the_measurement_states_it(self):
        validate_observation(self.obs, PACKAGE)
        meas, econ = self.obs["measurement"], self.obs["economics"]
        self.assertEqual((meas["knob"], meas["source_value"], meas["applied_value"]), ("warehouse_type", "PRO", "SERVERLESS"))
        self.assertEqual((meas["method"], meas["not_measured_reason"], meas["holds_other_knobs"], meas["cost_basis"]),
                         (MIGRATION_METHOD, None, False, "DBU_AND_MODELED_AWS"))
        self.assertIsNone(meas["replay"])
        self.assertIsNone(meas["cross_check"])
        self.assertIsNotNone(meas["billed_change"])
        # Baseline: the pre window's 8,844.97 DBU cost + 40,320 / 60 x 7.488 AWS at the same mix; observed: 11,330.60 + 0.
        self.assertEqual(Decimal(econ["counterfactual_old_cost"]), Decimal("13876.90"))
        self.assertEqual(Decimal(econ["observed_new_cost"]), Decimal("11330.60"))
        self.assertEqual(Decimal(econ["dbu_realized"]), Decimal("-2485.63"))  # a signed reduction: DBU rose
        self.assertEqual(Decimal(econ["aws_economic_realized"]), Decimal("5031.94"))
        self.assertEqual((econ["period_days"], econ["currency"], econ["aws_evidence_type"], econ["economic_basis"]), (28, "USD", "MODELED", "ECONOMIC_COST"))
        self.assertEqual(econ["counterfactual_method"], "CORRELATED_ONLY")  # diagnostic: never claim-eligible
        self.assertEqual(Decimal(econ["counterfactual_old_cost"]) - Decimal(econ["observed_new_cost"]), Decimal("2546.30"))
        self.assertTrue(self.obs["financial_quality"]["material_cost_component_available"])
        self.assertTrue(is_migration_observed(meas))

    def test_the_delta_is_the_negative_of_the_billed_change(self):
        meas, econ = self.obs["measurement"], self.obs["economics"]
        change = meas["billed_change"]
        self.assertEqual(Decimal(econ["dbu_realized"]), -Decimal(change["dbu"]))
        self.assertEqual(Decimal(econ["aws_economic_realized"]), -Decimal(change["aws_modeled"]))
        delta = Decimal(econ["counterfactual_old_cost"]) - Decimal(econ["observed_new_cost"])
        self.assertLessEqual(abs(delta + Decimal(change["total"])), Decimal("0.01"))  # the same figure, rounded once per cost

    def test_a_replayed_knob_keeps_its_replay(self):
        pro = world.package("autostop-scoped-pro")
        obs = project_observation(pro, world.measure(pro, world.world_spec("C13", {"auto_stop": 10}), 13))
        self.assertEqual(obs["measurement"]["method"], "AUTO_STOP_REPLAY")
        self.assertFalse(is_migration_observed(obs["measurement"]))

    def test_without_an_aws_price_the_delta_is_dbu_only(self):
        obs = project_observation(PACKAGE, world.measure(PACKAGE, world.world_spec("C01", MIGRATED), 1, aws_registry={}))
        validate_observation(obs, PACKAGE)
        self.assertEqual(obs["measurement"]["cost_basis"], "DBU_ONLY")
        self.assertEqual((obs["economics"]["aws_economic_realized"], obs["economics"]["aws_evidence_type"]), (None, None))
        self.assertEqual(Decimal(obs["economics"]["counterfactual_old_cost"]), Decimal("8844.97"))
        self.assertFalse(obs["financial_quality"]["material_cost_component_available"])

    def test_without_both_windows_a_volume_ratio_or_one_currency_there_are_no_economics(self):
        before_a_week = project_observation(PACKAGE, world.measure(PACKAGE, world.world_spec("C01", MIGRATED), 1, as_of="2026-10-14T12:00:00+00:00"))
        self.assertIsNone(before_a_week["economics"])
        self.assertEqual((before_a_week["measurement"]["method"], before_a_week["measurement"]["not_measured_reason"]), (None, "KNOB_NOT_REPLAYED"))
        mixed = copy.deepcopy(evidence())
        mixed["daily_cost"][-2] = {**mixed["daily_cost"][-2], "currency_code": "EUR"}
        obs = project_observation(PACKAGE, mixed)
        self.assertIsNone(obs["economics"])
        self.assertEqual((obs["measurement"]["method"], obs["measurement"]["not_measured_reason"]), (None, "KNOB_NOT_REPLAYED"))


class RealizationTests(unittest.TestCase):
    def setUp(self):
        self.rpkg = realized(project_observation(PACKAGE, evidence()))

    def test_the_range_is_reported_never_classified(self):
        econ = self.rpkg["economics"]
        self.assertEqual((self.rpkg["current_state"]["realization_state"], econ["classification_detail"]), ("MONITORING", "MIGRATION_RANGE_INFORMATIONAL"))
        self.assertEqual(econ["classification_basis"], "INFORMATIONAL")
        self.assertEqual(econ["denominator_source"], "PUBLISHER_SCENARIO")
        self.assertEqual({k: econ["expected_range"][k] for k in ("component", "low", "base", "high")},
                         {"component": "total", "low": "34091.82", "base": "137330.79", "high": "170191.45"})
        self.assertEqual(econ["observed_economic_delta"], "2546.30")
        self.assertEqual(econ["measurement"]["method"], MIGRATION_METHOD)
        self.assertEqual(econ["annualized_realized_savings"], str((Decimal("2546.30") * 365 / 28).quantize(Decimal("0.01"))))  # about 33,194 a year

    def test_nothing_is_attributed_to_the_recommendation(self):
        attribution = self.rpkg["attribution"]
        self.assertEqual(attribution["attribution_claim_status"], "OBSERVABLE_ONLY")
        self.assertIn(NO_REVERSE_MIGRATION_MODEL, attribution["hard_disqualifiers"])
        self.assertNotIn("NO_DEFENSIBLE_COUNTERFACTUAL", attribution["hard_disqualifiers"])
        self.assertEqual(Decimal(attribution["recommendation_attributed_realized_savings"]), 0)

    def test_it_stays_observable_without_the_canary_window(self):
        later = realized(project_observation(PACKAGE, world.measure(PACKAGE, world.world_spec("C01", MIGRATED), 1, as_of="2026-11-14T12:00:00+00:00")))
        self.assertEqual(later["attribution"]["attribution_claim_status"], "OBSERVABLE_ONLY")
        self.assertEqual(later["current_state"]["realization_state"], "MONITORING")
        self.assertNotIn("CANARY_GUARD_WINDOW_OPEN", later["attribution"]["hard_disqualifiers"])
        self.assertIn(NO_REVERSE_MIGRATION_MODEL, later["attribution"]["hard_disqualifiers"])

    def test_a_migration_without_a_published_range_is_still_monitored_not_blocked(self):
        unpriced = world.package("scope-migration-only")  # the migration's range is withheld (no deterministic estimate)
        obs = project_observation(unpriced, world.measure(unpriced, world.world_spec("C01", MIGRATED), 1))
        self.assertEqual(obs["economics"]["counterfactual_method"], "CORRELATED_ONLY")
        with tempfile.TemporaryDirectory() as tmp:
            rpkg = realize(unpriced, obs, Path(tmp)).to_dict()
        self.assertEqual((rpkg["current_state"]["realization_state"], rpkg["economics"]["classification_detail"], rpkg["economics"]["denominator_source"]),
                         ("MONITORING", "VALIDATED_VALUE_UNCLASSIFIED", "UNAVAILABLE"))
        self.assertEqual(rpkg["attribution"]["attribution_claim_status"], "OBSERVABLE_ONLY")
        self.assertIn(NO_REVERSE_MIGRATION_MODEL, rpkg["attribution"]["hard_disqualifiers"])

    def test_another_diagnostic_counterfactual_is_still_blocked(self):
        obs = project_observation(PACKAGE, evidence())
        obs["measurement"] = {**obs["measurement"], "method": None}  # a caller's observation: the same economics, no migration method
        rpkg = realized(obs)
        self.assertEqual((rpkg["current_state"]["realization_state"], rpkg["economics"]["classification_detail"]), ("BLOCKED", "COUNTERFACTUAL_NOT_CLAIM_ELIGIBLE"))
        self.assertIn("NO_DEFENSIBLE_COUNTERFACTUAL", rpkg["attribution"]["hard_disqualifiers"])
        self.assertNotIn(NO_REVERSE_MIGRATION_MODEL, rpkg["attribution"]["hard_disqualifiers"])

    def test_the_next_action_says_the_migration_was_observed(self):
        r = rc.RealizationPackage(**{f.name: self.rpkg[f.name] for f in dataclasses.fields(rc.RealizationPackage)})
        self.assertEqual(report.next_action(r), "Keep monitoring: migration observed, savings not modeled")
        # An auto-stop realization in the same state keeps the plain label.
        plain = dataclasses.replace(r, economics={k: v for k, v in r.economics.items() if k != "measurement"})
        self.assertEqual(report.next_action(plain), "Keep monitoring")


if __name__ == "__main__":
    unittest.main()
