"""Cluster-count counterfactuals (src/realizer/analyzer/cluster_counterfactual.py).

The running minutes by cluster count of the post-change window (``levels``) say what a min- or max-cluster change did:

- a floor cut: the counterfactual runs every level at least at the old floor; the removed cluster-minutes are priced at
  the post window's billed DBU cost per running cluster-minute and the modeled AWS cluster-hour price (method
  ``FLOOR_LEVELS``, ``CALIBRATED_PRIOR_STATE``), without levels not measured (``LEVELS_UNAVAILABLE``), a raise never
  measured (``FLOOR_RAISED``);
- a capacity add: the measured cost is the cluster-minutes above the old cap (at most up to the new cap), the cost
  change the performance verdict judges, the billed change reported next to it;
- a zero-priced cap cut: the minutes at the new cap and the upper bound of what the old cap could have added, as
  evidence; no saving.

Worlds are the simulated demo-fleet warehouses changed after their package (28-day post window, us-east-1).
"""
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer.analyzer import cluster_counterfactual as cc
from realizer.analyzer.measurement import MeasurementPlan
from realizer.analyzer.projector import project_observation
from realizer.config.loader import validate_observation
from realizer.realizer import realize

MIN_CUT = world.package("min-cut-scoped")  # wh-demo-011: PRO MEDIUM, min 3 -> 1, total 56,614.08 / 84,921.13 / 113,228.17
ADD = world.package("capacity-add-scoped")  # wh-demo-008: max 2 -> 5, PERFORMANCE, modeled -15,628.48/yr
CUT = world.package("capacity-cut-scoped")  # wh-demo-006: max 4 -> 2, COST_GUARDRAIL, priced 0


def run(package, observation):
    with tempfile.TemporaryDirectory() as tmp:
        return realize(package, observation, Path(tmp) / "out").to_dict()


def without_levels(evidence, value):
    held = copy.deepcopy(evidence)
    held["windows"]["post"]["levels"] = value
    return held


class FloorCutTests(unittest.TestCase):
    """wh-demo-011 min 3 -> 1 on 2026-10-08 14:32, measured 2026-11-07T12:00Z: one cluster ran 12,600 minutes."""

    @classmethod
    def setUpClass(cls):
        cls.evidence = world.measure(MIN_CUT, world.world_spec("C11", {"min": 1}), 11)
        cls.observation = project_observation(MIN_CUT, cls.evidence)

    def test_the_old_floor_is_replayed_on_the_post_change_levels(self):
        obs = self.observation
        validate_observation(obs, MIN_CUT)
        meas, levels = obs["measurement"], obs["measurement"]["levels"]
        self.assertEqual((meas["knob"], meas["method"], meas["not_measured_reason"], meas["holds_other_knobs"], meas["cost_basis"]),
                         ("min_clusters", "FLOOR_LEVELS", None, True, "DBU_AND_MODELED_AWS"))
        self.assertEqual(levels["post_levels"], [{"cluster_count": 1, "running_minutes": "12600"}])
        self.assertEqual(levels["pre_levels"], [{"cluster_count": 3, "running_minutes": "12600"}])
        self.assertEqual((levels["basis"], levels["observed_cluster_minutes"], levels["counterfactual_cluster_minutes"], levels["cluster_minutes"]),
                         ("FLOOR_CUT_REMOVED", "12600", "37800", "25200"))
        self.assertIsNone(meas["replay"])

    def test_the_removed_cluster_minutes_are_priced_like_the_replay(self):
        econ, levels = self.observation["economics"], self.observation["measurement"]["levels"]
        # 2,776.50 billed over 12,600 running cluster-minutes; AWS 7.488 per cluster-hour.
        self.assertEqual(levels["dbu_cost_per_cluster_minute"], "0.220357")
        self.assertEqual(levels["aws_cluster_hourly_price"], "7.488")
        self.assertEqual((levels["dbu_cost"], levels["aws_cost"], levels["total_cost"]), ("5553.00", "3144.96", "8697.96"))
        self.assertEqual((econ["observed_new_cost"], econ["counterfactual_old_cost"]), ("4348.98", "13046.94"))  # 2,776.50 + 1,572.48
        self.assertEqual((econ["dbu_realized"], econ["aws_economic_realized"], econ["aws_evidence_type"]), ("5553.00", "3144.96", "MODELED"))
        self.assertEqual((econ["counterfactual_method"], econ["period_days"]), ("CALIBRATED_PRIOR_STATE", 28))
        self.assertIs(self.observation["financial_quality"]["material_cost_component_available"], True)

    def test_the_billed_cross_check_agrees(self):
        check = self.observation["measurement"]["cross_check"]
        self.assertEqual((check["outcome"], check["replay_dbu_saving"]), ("AGREES", "5553.00"))
        self.assertEqual(check["billed_dbu_reduction"], "5483.43")  # within 25 percent of 5,553.00

    def test_it_realizes_the_published_range(self):
        snapshot = run(MIN_CUT, self.observation)
        econ = snapshot["economics"]
        self.assertEqual((econ["realization_state"], econ["classification_detail"]), ("REALIZED", "REALIZED"))
        self.assertEqual(econ["realized_savings_period"], "8697.96")
        self.assertEqual(econ["annualized_realized_savings"], "113384.12")  # 8,697.96 x 365 / 28
        self.assertEqual(econ["expected_range"], {"component": "total", "low": "56614.08", "base": "84921.13", "high": "113228.17"})
        self.assertEqual(econ["measurement"]["levels"]["basis"], "FLOOR_CUT_REMOVED")

    def test_without_levels_the_floor_cut_is_not_measured(self):
        for value in (None, []):
            obs = project_observation(MIN_CUT, without_levels(self.evidence, value))
            validate_observation(obs, MIN_CUT)
            meas = obs["measurement"]
            self.assertEqual((meas["method"], meas["not_measured_reason"], meas["holds_other_knobs"], meas["levels"]),
                             (None, "LEVELS_UNAVAILABLE", False, None))
            self.assertIsNone(obs["economics"])
            self.assertIsNone(meas["cross_check"])
            self.assertIsNotNone(meas["billed_change"])  # still observed

    def test_without_an_aws_price_the_counterfactual_is_dbu_only(self):
        obs = project_observation(MIN_CUT, world.measure(MIN_CUT, world.world_spec("C11", {"min": 1}), 11, aws_registry={}))
        levels = obs["measurement"]["levels"]
        self.assertEqual(obs["measurement"]["cost_basis"], "DBU_ONLY")
        self.assertEqual((levels["dbu_cost"], levels["aws_cost"], levels["total_cost"]), ("5553.00", None, None))
        self.assertIsNone(obs["economics"]["aws_economic_realized"])

    def test_before_a_whole_settled_week_a_floor_cut_is_not_settled(self):
        obs = project_observation(MIN_CUT, world.measure(MIN_CUT, world.world_spec("C11", {"min": 1}), 11, as_of="2026-10-14T12:00:00+00:00"))
        self.assertEqual((obs["measurement"]["not_measured_reason"], obs["measurement"]["levels"]), ("NOT_SETTLED", None))


class FloorArithmeticTests(unittest.TestCase):
    def plan(self, source, applied, knob="min_clusters"):
        observed = {"epoch_start": "2026-10-08T14:32:00+00:00", "warehouse_type": "PRO", "warehouse_channel": "CURRENT", "warehouse_size": "SMALL",
                    "min_clusters": applied if knob == "min_clusters" else 1, "max_clusters": applied if knob == "max_clusters" else 4,
                    "auto_stop_minutes": 10}
        return MeasurementPlan(observed_epoch=observed, step={"knob": knob, "current_value": source},
                               post={"label": "post", "epoch_start": observed["epoch_start"], "first_day": "2026-10-09", "last_day": "2026-10-15"})

    def evidence(self, levels, cost=700.0, running=None):
        running = sum(c * m for c, m in levels) if running is None else running
        return {"windows": {"pre": None, "post": {"levels": [{"cluster_count": c, "running_minutes": m} for c, m in levels],
                                                  "running": {"observed_running_cluster_minutes": running}}},
                "aws": {"post": {"status": "MODELED", "cluster_hourly_price": 6.0}}}

    def bill(self, cost):
        return {"cost": Decimal(str(cost)), "currencies": ["USD"]}

    def test_levels_at_or_above_the_old_floor_are_unchanged(self):
        # Old floor 3, new 1: 100 min at 1 -> 300, 50 at 2 -> 150, 20 at 4 stays 80: counterfactual 530, observed 280.
        result = cc.cluster_levels(self.plan(3, 1), self.evidence([(1, 100), (2, 50), (4, 20)]), self.bill(280))
        self.assertEqual((result.block["observed_cluster_minutes"], result.block["counterfactual_cluster_minutes"], result.block["cluster_minutes"]),
                         ("280", "530", "250"))
        self.assertEqual(result.counterfactual["dbu_realized"], Decimal("250"))  # 1 per cluster-minute
        self.assertEqual(result.counterfactual["aws_realized"], Decimal("25"))  # 250 / 60 x 6
        self.assertIsNone(result.reason)

    def test_a_floor_raise_is_not_measured(self):
        # The levels at the new floor cannot say how many clusters the lower old floor would have run.
        result = cc.cluster_levels(self.plan(1, 3), self.evidence([(3, 100)]), self.bill(300))
        self.assertEqual((result.counterfactual, result.reason, result.block["basis"], result.block["cluster_minutes"]), (None, "FLOOR_RAISED", None, None))

    def test_an_unchanged_floor_is_not_called_a_raise(self):
        # A floor at the package's value has no counterfactual and no reason of its own (the plan never measures it).
        result = cc.cluster_levels(self.plan(2, 2), self.evidence([(2, 100)]), self.bill(200))
        self.assertEqual((result.counterfactual, result.reason, result.block["basis"]), (None, None, None))

    def test_unpriced_billing_keeps_the_quantities_without_a_counterfactual(self):
        result = cc.cluster_levels(self.plan(3, 1), self.evidence([(1, 100)]), {"cost": None, "currencies": ["USD"]})
        self.assertEqual((result.counterfactual, result.reason, result.block["cluster_minutes"], result.block["dbu_cost"]), (None, None, "200", None))

    def test_cluster_minutes_above_the_old_cap_count_at_most_up_to_the_new_cap(self):
        # Old cap 2, new 4: 805 at 3 -> 805, 1,495 at 4 -> 2,990, 10 at 5 (a recycling transient) -> 20.
        result = cc.cluster_levels(self.plan(2, 4, "max_clusters"), self.evidence([(1, 500), (2, 9600), (3, 805), (4, 1495), (5, 10)]), self.bill(1000))
        self.assertEqual((result.block["basis"], result.block["cluster_minutes"]), ("CLUSTER_LEVELS_ABOVE_OLD_CAP", "3815"))
        self.assertIsNone(result.counterfactual)

    def test_a_cap_cut_bounds_what_the_old_cap_could_have_added(self):
        # Old cap 4, new 2: 50 minutes at 2 and 10 above it count as at the cap: at most 60 x 2 = 120 cluster-minutes.
        result = cc.cluster_levels(self.plan(4, 2, "max_clusters"), self.evidence([(1, 100), (2, 50), (3, 10)]), self.bill(230))
        block = result.block
        self.assertEqual((block["basis"], block["minutes_at_new_cap"], block["cluster_minutes"]), ("OLD_CAP_UPPER_BOUND", "60", "120"))
        self.assertEqual((block["dbu_cost"], block["aws_cost"], block["total_cost"]), ("120.00", "12.00", "132.00"))
        self.assertEqual((block["annualized_dbu_cost"], block["annualized_total_cost"]), ("6257.14", "6882.86"))  # x 365 / 7

    def test_another_knob_has_no_levels(self):
        plan = self.plan(30, 10, "max_clusters")
        plan = MeasurementPlan(observed_epoch=plan.observed_epoch, step={"knob": "auto_stop_minutes", "current_value": 30}, post=plan.post)
        self.assertEqual(cc.cluster_levels(plan, self.evidence([(1, 100)]), self.bill(100)), cc.ClusterLevels(None, None, None))


class CapacityAddCostTests(unittest.TestCase):
    """wh-demo-008 max 2 -> 5: post levels {1: 500, 2: 9,600, 3: 805, 4: 1,495}."""

    @classmethod
    def setUpClass(cls):
        cls.evidence = world.measure(ADD, world.world_spec("C08", {"max": 5}), 8)
        cls.observation = project_observation(ADD, cls.evidence)

    def test_the_cluster_minutes_above_the_old_cap_are_priced(self):
        validate_observation(self.observation, ADD)
        meas, levels = self.observation["measurement"], self.observation["measurement"]["levels"]
        self.assertEqual((meas["method"], meas["not_measured_reason"]), (None, "KNOB_NOT_REPLAYED"))
        self.assertEqual((levels["basis"], levels["cluster_minutes"], levels["observed_cluster_minutes"]), ("CLUSTER_LEVELS_ABOVE_OLD_CAP", "3795", "28095"))
        self.assertEqual((levels["dbu_cost"], levels["aws_cost"], levels["total_cost"]), ("832.83", "473.62", "1306.45"))
        self.assertEqual(levels["annualized_total_cost"], "17030.48")
        self.assertIsNone(self.observation["economics"])

    def test_the_measured_cost_is_judged_and_the_billed_change_reported_next_to_it(self):
        econ = run(ADD, self.observation)["economics"]
        self.assertEqual((econ["realization_state"], econ["classification_detail"]), ("PERFORMANCE_VALIDATED", "PERFORMANCE_VALIDATED"))
        self.assertEqual(econ["cost_change"], {"annualized_cost_increase": "17030.48", "basis": "CLUSTER_LEVELS_ABOVE_OLD_CAP", "component": "total",
                                               "billed_annualized_cost_increase": "12887.78",
                                               "modeled_annual_cost_increase": "15628.48", "review_above": "19535.60"})

    def test_without_levels_the_cost_change_is_the_billed_change(self):
        obs = project_observation(ADD, without_levels(self.evidence, None))
        self.assertIsNone(obs["measurement"]["levels"])
        econ = run(ADD, obs)["economics"]
        self.assertEqual((econ["cost_change"]["annualized_cost_increase"], econ["cost_change"]["basis"]), ("12887.78", "BILLED_PRE_POST_AT_PRE_MIX"))

    def test_without_an_aws_price_the_measured_cost_is_dbu(self):
        obs = project_observation(ADD, world.measure(ADD, world.world_spec("C08", {"max": 5}), 8, aws_registry={}))
        cost = run(ADD, obs)["economics"]["cost_change"]
        self.assertEqual((cost["basis"], cost["component"], cost["annualized_cost_increase"]), ("CLUSTER_LEVELS_ABOVE_OLD_CAP", "dbu", "10856.56"))


class CapCutEvidenceTests(unittest.TestCase):
    """wh-demo-006 max 4 -> 2: one cluster all window, no minute at the new cap."""

    def test_no_minute_at_the_new_cap_bounds_the_old_cap_at_zero_and_stays_capacity_validated(self):
        obs = project_observation(CUT, world.measure(CUT, world.world_spec("C06", {"max": 2}), 6))
        validate_observation(obs, CUT)
        levels = obs["measurement"]["levels"]
        self.assertEqual((levels["basis"], levels["minutes_at_new_cap"], levels["cluster_minutes"], levels["total_cost"]),
                         ("OLD_CAP_UPPER_BOUND", "0", "0", "0.00"))
        snapshot = run(CUT, obs)
        econ = snapshot["economics"]
        self.assertEqual(snapshot["current_state"]["realization_state"], "CAPACITY_VALIDATED")
        self.assertEqual(econ["performance"]["old_cap_upper_bound"], {"minutes_at_new_cap": "0", "cluster_minutes": "0", "annualized_cost": "0.00",
                                                                      "component": "total"})
        self.assertIsNone(econ["successful_realized_savings"])


if __name__ == "__main__":
    unittest.main()
