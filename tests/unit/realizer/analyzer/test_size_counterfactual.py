"""The size counterfactual (``realizer.analyzer.size_counterfactual``): what a size change realized.

The post-change days are priced at the old size: the running cluster-minutes the old size would have run (its runtime
ratio measured on statement sources seen before and after the change, applied to the busy share of running time) at the
pre-change window's billed DBU cost per running cluster-minute and the old size's modeled AWS cluster-hour price. Two
bounds are published: elastic (slower or faster queries change running time in proportion to their busy share) and
inelastic (running time unchanged, as on a warehouse whose queries fill idle gaps); the bound with the smaller saving is
claimed. Worlds are simulated demo-fleet warehouses (``tests/realizer_world``): wh-demo-006 (Sales Analytics, Pro)
LARGE to MEDIUM, applied 2026-10-08 14:32 UTC and measured 2026-11-07 12:00 UTC (28 settled days).
"""
import copy
import dataclasses
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer.analyzer import size_counterfactual as sc
from realizer.analyzer.collector import demo_world
from realizer.analyzer.measurement import plan_measurement
from realizer.analyzer.projector import project_observation
from realizer.config.loader import validate_measurement_evidence, validate_observation
from realizer.realizer import realize

SIZE = world.package("inplace-size-canary")  # Pro wh-demo-006 LARGE -> MEDIUM, step 1 (a canary)
ROLLBACK = world.package("canary-rollback")  # Pro wh-demo-040 MEDIUM -> LARGE (an increase)
CHANGE = demo_world.change_at(SIZE)


def size_world(package, index, size, runtime=None):
    """Evidence of *package*'s warehouse with its size set to *size* at the demo's change time."""
    spec = demo_world.world_spec(index, [{"at": CHANGE.isoformat(), "set": {"size": size}}], runtime=runtime)
    return world.measure(package, spec, index)


CUT = size_world(SIZE, 6, "MEDIUM")
SLOWER = size_world(SIZE, 6, "MEDIUM", {"MEDIUM": 1.2})


def project(package, evidence):
    obs = project_observation(package, evidence)
    validate_observation(obs, package)
    return obs


def counterfactual(package, evidence, **plan_changes):
    plan = plan_measurement(package, evidence["epochs"], evidence["billing"]["settled_through"])
    if plan_changes:
        plan = dataclasses.replace(plan, **plan_changes)
    return sc.size_counterfactual(package, plan, evidence)


class WorkedExampleTests(unittest.TestCase):
    """LARGE to MEDIUM at the same query speed (runtime factor 1.0)."""

    def test_the_old_size_prices_the_post_change_days(self):
        validate_measurement_evidence(CUT)
        obs = project(SIZE, CUT)
        meas, econ, size = obs["measurement"], obs["economics"], obs["measurement"]["size"]
        self.assertEqual((meas["method"], meas["not_measured_reason"], meas["holds_other_knobs"], meas["cost_basis"]),
                         ("SIZE_RUNTIME", None, True, "DBU_AND_MODELED_AWS"))
        self.assertIsNone(meas["replay"])
        # rho = 1: matched sources ran as long after the change; one source, all 32,303 post-change executed queries.
        self.assertEqual((size["runtime_ratio"], size["matched_sources"], size["matched_post_queries"], size["post_executed_queries"]),
                         ("1", 1, 32303, 32303))
        self.assertEqual(size["matched_query_coverage"], "1")
        # b = 3,600 busy wall-minutes of 12,286 running cluster-minutes (one floor cluster).
        self.assertEqual((size["busy_share"], size["observed_running_cluster_minutes"], size["post_busy_wall_minutes"]),
                         ("0.293016", "12286", "3600"))
        # Old DBU rate: 4,491.46 billed over 12,286 pre-change running cluster-minutes; the new one 2,691.78 over 12,286.
        self.assertEqual((size["old_dbu_cost_per_cluster_minute"], size["new_dbu_cost_per_cluster_minute"]), ("0.365576", "0.219093"))
        self.assertEqual((size["measured_rate_ratio"], size["published_rate_ratio"]), ("0.59931", "0.6"))  # 24 / 40 DBU per cluster-hour
        self.assertIs(size["rate_ratio_within_published_band"], True)  # within 25 percent of the published ratio
        self.assertEqual((size["old_aws_cluster_hourly_price"], size["new_aws_cluster_hourly_price"]), ("12.48", "7.488"))
        self.assertEqual(size["published_runtime_multiplier"],
                         {"source": "PUBLISHER_THRESHOLDS", "direction": "DOWNSIZE", "optimistic": "1.1", "base": "1.3", "conservative": "2"})
        self.assertIs(size["runtime_ratio_in_published_range"], False)  # 1.0 is faster than the optimistic 1.1
        # Both bounds run 12,286 cluster-minutes at rho = 1; the elastic one is claimed.
        self.assertEqual(size["elastic"], size["inelastic"])
        self.assertEqual(size["bound_used"], "ELASTIC")
        self.assertEqual(size["elastic"], {"running_cluster_minutes": "12286", "dbu_cost": "4491.46", "aws_cost": "2555.49",
                                           "counterfactual_cost": "7046.95", "realized": "2821.88"})
        # Counterfactual 4,491.46 DBU + 2,555.49 AWS = 7,046.95; observed 2,691.78 + 12,286 / 60 x 7.488 = 4,225.07.
        self.assertEqual((econ["counterfactual_old_cost"], econ["observed_new_cost"]), ("7046.95", "4225.07"))
        self.assertEqual((econ["dbu_realized"], econ["aws_economic_realized"]), ("1799.68", "1022.20"))
        self.assertEqual((econ["counterfactual_method"], econ["aws_evidence_type"], econ["currency"], econ["period_days"]),
                         ("CALIBRATED_PRIOR_STATE", "MODELED", "USD", 28))
        self.assertIs(obs["financial_quality"]["material_cost_component_available"], True)

    def test_the_billed_cross_check_agrees_and_the_billed_change_stays_a_diagnostic(self):
        meas = project(SIZE, CUT)["measurement"]
        self.assertEqual((meas["cross_check"]["outcome"], meas["cross_check"]["replay_dbu_saving"]), ("AGREES", "1799.68"))
        self.assertEqual(meas["billed_change"]["basis"], "BILLED_PRE_POST_AT_PRE_MIX")
        self.assertLess(Decimal(meas["billed_change"]["dbu"]), 0)

    def test_the_realization_is_classified_against_the_published_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = demo_world.measure_demo(SIZE, Path(tmp))
            snapshot = realize(SIZE, obs, Path(tmp)).to_dict()
        econ = snapshot["economics"]
        self.assertEqual(obs["measurement"]["size"]["elastic"]["realized"], "2821.88")
        # 2,821.88 x 365 / 28 = 36,785.22 a year against the published total 20,493.89 / 31,792.93 / 35,021.21: above the high,
        # under 1.25 x the high.
        self.assertEqual(econ["annualized_realized_savings"], "36785.22")
        self.assertEqual(econ["expected_range"], {"component": "total", "low": "20493.89", "base": "31792.93", "high": "35021.21"})
        self.assertEqual((econ["realization_state"], econ["classification_detail"]), ("REALIZED", "REALIZED"))
        self.assertEqual(econ["measurement"]["method"], "SIZE_RUNTIME")
        self.assertEqual(econ["measurement"]["size"]["bound_used"], "ELASTIC")


    def test_a_modified_size_is_reported_without_a_denominator(self):
        # The owner applies SMALL instead of MEDIUM: realized against LARGE is measured, but no published range prices SMALL.
        with tempfile.TemporaryDirectory() as tmp:
            obs = demo_world.measure_demo(SIZE, Path(tmp), modified=True)
            snapshot = realize(SIZE, obs, Path(tmp)).to_dict()
        econ = snapshot["economics"]
        self.assertEqual((obs["measurement"]["applied_value"], obs["measurement"]["method"]), ("SMALL", "SIZE_RUNTIME"))
        self.assertEqual(obs["measurement"]["size"]["published_rate_ratio"], "0.3")  # 12 / 40 DBU per cluster-hour
        self.assertEqual(snapshot["application"]["implementation_conformance"], "MODIFIED")
        self.assertEqual((econ["realization_state"], econ["classification_detail"], econ["denominator_source"]),
                         ("MONITORING", "VALIDATED_VALUE_UNCLASSIFIED", "UNAVAILABLE"))
        size = obs["measurement"]["size"]
        self.assertEqual(econ["realized_savings_period"], size[size["bound_used"].lower()]["realized"])


class SlowerQueriesTests(unittest.TestCase):
    """LARGE to MEDIUM with queries 1.2 times as long on MEDIUM: idle gaps absorb the slower queries."""

    def test_both_bounds_are_published_and_the_elastic_one_is_claimed(self):
        obs = project(SIZE, SLOWER)
        size, econ = obs["measurement"]["size"], obs["economics"]
        self.assertEqual(size["runtime_ratio"], "1.2")
        self.assertIs(size["runtime_ratio_in_published_range"], True)
        # Running time barely moved (12,287.2 against 12,286 at LARGE): the queries filled idle gaps.
        self.assertEqual((size["observed_running_cluster_minutes"], size["busy_share"]), ("12287.2", "0.351585"))
        # Elastic: 12,287.2 x (1 - b + b / 1.2), 720 cluster-minutes fewer at the old size; inelastic: 12,287.2.
        self.assertEqual(size["elastic"]["running_cluster_minutes"], "11567.200798")
        self.assertEqual(size["inelastic"]["running_cluster_minutes"], "12287.2")
        self.assertEqual((size["elastic"]["counterfactual_cost"], size["inelastic"]["counterfactual_cost"]), ("6634.67", "7047.64"))
        self.assertEqual(size["bound_used"], "ELASTIC")  # the smaller saving
        self.assertEqual((econ["counterfactual_old_cost"], econ["observed_new_cost"]), ("6634.67", "4225.49"))
        self.assertEqual(size["elastic"]["realized"], "2409.18")


class IncreaseTests(unittest.TestCase):
    """MEDIUM to LARGE (wh-demo-040 rolled back to LARGE) with queries 1.2 times as long on MEDIUM: faster after the change."""

    def test_an_increase_claims_the_inelastic_bound(self):
        evidence = size_world(ROLLBACK, 40, "LARGE", {"MEDIUM": 1.2})
        size = project(ROLLBACK, evidence)["measurement"]["size"]
        self.assertEqual(Decimal(size["runtime_ratio"]).quantize(Decimal("0.0001")), Decimal("0.8333"))
        self.assertEqual(size["bound_used"], "INELASTIC")
        self.assertEqual(size["published_runtime_multiplier"]["direction"], "UPSIZE")
        # The old (smaller) size would have run longer on the elastic bound: the inelastic bound costs less, the smaller saving.
        self.assertLess(Decimal(size["inelastic"]["counterfactual_cost"]), Decimal(size["elastic"]["counterfactual_cost"]))
        self.assertEqual(size["inelastic"]["running_cluster_minutes"], size["observed_running_cluster_minutes"])


class NotMeasuredTests(unittest.TestCase):
    def assert_unmeasured(self, package, evidence, reason, billed=True):
        obs = project(package, evidence)
        meas = obs["measurement"]
        self.assertEqual((meas["method"], meas["not_measured_reason"], meas["holds_other_knobs"]), (None, reason, False))
        self.assertIsNone(obs["economics"])
        self.assertIsNone(meas["cross_check"])
        self.assertEqual(meas["billed_change"] is not None, billed)  # still reported when billing is priced
        return meas

    def test_too_few_matched_queries_leave_the_runtime_unmeasured(self):
        few = copy.deepcopy(CUT)
        few["size_runtime"]["matched_post_queries"] = 299  # under the canary p95 sample of 300
        meas = self.assert_unmeasured(SIZE, few, "SIZE_RUNTIME_UNMEASURED")
        self.assertIsNone(meas["size"]["runtime_ratio"])
        self.assertIsNone(meas["size"]["bound_used"])
        thin = copy.deepcopy(CUT)
        thin["size_runtime"]["post_executed_queries"] = 70000  # 32,303 matched: under half of them
        self.assert_unmeasured(SIZE, thin, "SIZE_RUNTIME_UNMEASURED")
        empty = copy.deepcopy(CUT)
        empty["size_runtime"]["post_ms_at_pre_median"] = None
        self.assert_unmeasured(SIZE, empty, "SIZE_RUNTIME_UNMEASURED")

    def test_a_previous_epoch_at_another_size_has_no_source_rate(self):
        other = copy.deepcopy(CUT)
        other["epochs"][0]["warehouse_size"] = "X_LARGE"  # the step's source value is LARGE
        result = counterfactual(SIZE, other)
        self.assertEqual(result.reason, "SOURCE_RATE_UNAVAILABLE")
        self.assertIsNone(result.economics)
        self.assertIsNotNone(result.block["runtime_ratio"])

    def test_a_rate_ratio_outside_the_published_band_has_no_source_rate(self):
        # The pre-change days billed 1.5 times as much: MEDIUM costs 0.40 of LARGE per cluster-minute, not the published 0.60
        # (one third off it, outside the 25 percent band).
        dearer = copy.deepcopy(CUT)
        for row in dearer["daily_cost"]:
            if "2026-09-10" <= row["usage_date"] <= "2026-10-07" and row["list_cost"] is not None:
                row["list_cost"] = row["list_cost"] * 1.5
        meas = self.assert_unmeasured(SIZE, dearer, "SOURCE_RATE_UNAVAILABLE")
        self.assertEqual((meas["size"]["measured_rate_ratio"][:4], meas["size"]["published_rate_ratio"]), ("0.39", "0.6"))
        self.assertIs(meas["size"]["rate_ratio_within_published_band"], False)
        self.assertIsNotNone(meas["size"]["runtime_ratio"])

    def test_a_size_without_a_published_rate_has_no_source_rate(self):
        self.assertIsNone(sc.published_rate_ratio("4X_LARGE", "5X_LARGE"))
        self.assertIsNone(sc.within_published_band(Decimal("0.5"), None))
        self.assertEqual((sc.within_published_band(Decimal("0.75"), Decimal("0.6")), sc.within_published_band(Decimal("0.76"), Decimal("0.6"))),
                         (True, False))

    def test_unpriced_pre_change_billing_has_no_source_rate(self):
        unpriced = copy.deepcopy(CUT)
        day = next(r for r in unpriced["daily_cost"] if "2026-09-10" <= r["usage_date"] <= "2026-10-07")  # a pre-change day
        day["unpriced_dbu_quantity"] = day["dbu_quantity"]
        self.assert_unmeasured(SIZE, unpriced, "SOURCE_RATE_UNAVAILABLE", billed=False)

    def test_an_unknown_busy_share_is_not_measured(self):
        idle = copy.deepcopy(CUT)
        idle["windows"]["post"]["running"]["observed_running_wall_minutes"] = 0
        meas = self.assert_unmeasured(SIZE, idle, "BUSY_SHARE_UNKNOWN")
        self.assertIsNone(meas["size"]["busy_share"])

    def test_without_the_size_runtime_nothing_is_measured(self):
        unread = copy.deepcopy(CUT)
        unread["size_runtime"] = None
        meas = self.assert_unmeasured(SIZE, unread, "SIZE_RUNTIME_UNMEASURED")
        self.assertIsNone(meas["size"])

    def test_another_knob_has_no_size_block(self):
        obs = project_observation(world.package("autostop-scoped-pro"), world.measure(world.package("autostop-scoped-pro"),
                                                                                      world.world_spec("C13", {"auto_stop": 10}), 13))
        self.assertIsNone(obs["measurement"]["size"])


class BoundsTests(unittest.TestCase):
    def test_elastic_and_inelastic_running_time(self):
        # 1,000 running cluster-minutes, a quarter busy: twice as slow at the old size adds 250 elastic minutes.
        self.assertEqual(sc.counterfactual_running(Decimal("1000"), Decimal("0.25"), Decimal("0.5")),
                         {"ELASTIC": Decimal("1250"), "INELASTIC": Decimal("1000")})
        self.assertEqual(sc.counterfactual_running(Decimal("1000"), Decimal("0.25"), Decimal("2")),
                         {"ELASTIC": Decimal("875"), "INELASTIC": Decimal("1000")})
        self.assertEqual((sc.bound_used(Decimal("1")), sc.bound_used(Decimal("1.3")), sc.bound_used(Decimal("0.7"))),
                         ("ELASTIC", "ELASTIC", "INELASTIC"))


if __name__ == "__main__":
    unittest.main()
