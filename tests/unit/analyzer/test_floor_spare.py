"""Spare floor capacity (the price of a lower min-cluster floor) is measured on the DBU forecast's own days.

A minute at the floor whose demand needed fewer clusters than the floor holds clusters nobody used; a lower floor removes
at most those. Whole-epoch time at the floor overstates growing and steady-busy warehouses and understates mostly idle
ones, so the measure is per day and blended like the forecast.
"""
import copy
import datetime as dt
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.queries.events import RESOURCE_DAILY_SQL  # noqa: E402
from publisher.analyzer.metrics import FLOOR_DEMAND_FIELD, floor_demand_map, floor_spare_shares  # noqa: E402
from publisher.contracts.scenario import PlanStep  # noqa: E402
from publisher.contracts.source import WarehouseConfig  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.packager.scenarios import ScenarioPlanner  # noqa: E402


def days(start, n, cluster_minutes, floor_map):
    d0 = dt.date.fromisoformat(start)
    return [{"usage_day": (d0 + dt.timedelta(days=i)).isoformat(), "uptime_minutes": cluster_minutes / 2, "cluster_minutes": cluster_minutes,
             "minutes_at_min": sum(floor_map.values()) if floor_map else 0.0, "minutes_at_max": 0.0, "recycle_minutes": 0.0,
             "max_cluster_count": 2, "starts": 1, FLOOR_DEMAND_FIELD: dict(floor_map)} for i in range(n)]


class FloorSpareShareTests(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()
        self.e = copy.deepcopy(demo_evidence()[0])
        self.ep = self.e.current_epoch
        self.ep.events.update({"resource_daily_from": "2026-06-03", "resource_coverage_from": None})
        self.meta = dict(self.e.forecast_metadata, forecast_model="RUN_RATE", forecast_basis_end="2026-09-29", trend_adjustment_factor=1.0)

    def test_share_counts_only_clusters_the_demand_did_not_need(self):
        # Floor 3, per day 1000 running cluster-minutes: 200 idle minutes at the floor, 100 at demand 1, 50 at demand 2.
        self.ep.resource_daily = days("2026-06-03", 119, 1000.0, {"0": 200.0, "1": 100.0, "2": 50.0})
        shares, info = floor_spare_shares(self.ep, self.meta, self.t, 3)
        # Floor 1: 200 x 2 + 100 x 2 + 50 x 1 = 650; floor 2: 200 + 100 + 50 = 350.
        self.assertEqual(shares, {"1": 0.65, "2": 0.35})
        self.assertEqual(info["spare_cluster_minutes_per_day"], {"1": 650.0, "2": 350.0})

    def test_share_follows_the_forecast_window_not_the_epoch(self):
        # 35 busy days with no spare capacity, then the last 84 days mostly idle at the floor: the forecast's own window
        # (84 days blended with the last 28) sees only the recent regime.
        self.ep.resource_daily = days("2026-06-03", 35, 1000.0, {}) + days("2026-07-08", 84, 1000.0, {"0": 400.0})
        shares, _ = floor_spare_shares(self.ep, self.meta, self.t, 2)
        self.assertEqual(shares, {"1": 0.4})

    def test_a_basis_day_without_the_map_is_unknown_not_zero(self):
        rows = days("2026-06-03", 119, 1000.0, {"0": 400.0})
        rows[-3].pop(FLOOR_DEMAND_FIELD)
        self.ep.resource_daily = rows
        shares, info = floor_spare_shares(self.ep, self.meta, self.t, 2)
        self.assertIsNone(shares)
        self.assertEqual((info["blocker"], info["reason"]), ("FLOOR_DEMAND_EVIDENCE_UNAVAILABLE", "FLOOR_DEMAND_NOT_COLLECTED"))

    def test_a_day_outside_the_window_without_the_map_does_not_matter(self):
        rows = days("2026-06-03", 119, 1000.0, {"0": 400.0})
        rows[0].pop(FLOOR_DEMAND_FIELD)  # 2026-06-03, before the 84-day window
        self.ep.resource_daily = rows
        shares, _ = floor_spare_shares(self.ep, self.meta, self.t, 2)
        self.assertEqual(shares, {"1": 0.4})

    def test_collector_sql_and_map_parsing(self):
        self.assertIn("AS floor_minutes_by_demand_clusters", RESOURCE_DAILY_SQL)
        self.assertIn("COALESCE(fd.floor_minutes_by_demand_clusters, '{}')", RESOURCE_DAILY_SQL)
        self.assertIn("/ 10) AS INT) AS need", RESOURCE_DAILY_SQL)
        self.assertEqual(floor_demand_map('{"2": 1.5, "0": 3}'), {"0": 3.0, "2": 1.5})
        self.assertEqual(floor_demand_map("{}"), {})
        self.assertIsNone(floor_demand_map("not json"))
        self.assertIsNone(floor_demand_map(None))

    def test_collector_parses_the_json_column(self):
        epoch = {"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                 "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 2, "max_clusters": 2,
                 "auto_stop_minutes": 10, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}
        rows = [dict(r, workspace_id="1", warehouse_id="w", epoch_start="2026-09-01T00:00:00+00:00", **{FLOOR_DEMAND_FIELD: '{"0": 12.5}'})
                for r in days("2026-09-01", 3, 60.0, {})]

        class Client:
            def query(self, statement, *, label="collector_sql"):
                return [epoch] if label == "config_epochs" else rows if label == "resource_daily" else []

        e = extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-10-01T12:00:00+00:00")[0]
        self.assertEqual(e.current_epoch.resource_daily[0][FLOOR_DEMAND_FIELD], {"0": 12.5})


class MaxCutPeakTests(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def cut(self, peak):
        e = copy.deepcopy(demo_evidence()[0])
        e.current_epoch.config = WarehouseConfig("PRO", "CURRENT", "LARGE", 2, 6, 120)
        for row in e.current_epoch.resource_daily:
            row["max_cluster_count"] = peak
        return next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "max_clusters")

    def test_pro_classic_cut_never_lands_below_the_recent_peak(self):
        self.assertEqual(self.cut(2).recommended_config, {"max_clusters": 3})  # demand target
        self.assertEqual(self.cut(5).recommended_config, {"max_clusters": 5})  # the warehouse used 5 clusters recently


class FloorCutMarginalHoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        # Pinned Capacity: Pro MEDIUM pinned at 3; auto-stop (approval-only) runs before the floor-cut canary.
        cls.e = next(e for e in synthetic_demo_evidence() if e.current_epoch.tags["golden_scenario"] == "C11")
        cls.by_knob = {c.knob: c for c in p.evaluate_warehouses([cls.e], cls.t)[0].candidates}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def steps(self):
        out = []
        for i, (knob, state) in enumerate((("auto_stop_minutes", "READY_FOR_HITL"), ("min_clusters", "READY_FOR_CANARY"))):
            c = self.by_knob[knob]
            out.append(PlanStep(i + 1, c.recommendation_id, knob, c.current_config, c.recommended_config, c.status, "GATE", 0, [], [], state))
        return out

    def test_a_floor_cut_below_materiality_after_auto_stop_is_held(self):
        by_id = {c.recommendation_id: c for c in self.by_knob.values()}
        planner = ScenarioPlanner(self.t)
        priced = self.steps()
        planner.bundle.estimate(self.e, priced, by_id)
        marginal = priced[1].step_dbu_savings_range_365.base + (priced[1].step_aws_savings_range_365.base if priced[1].step_aws_savings_range_365 else 0.0)
        standalone = self.by_knob["min_clusters"].estimated_savings_365
        self.assertLess(marginal, standalone)
        floor = (marginal + standalone) / 2  # cleared alone, not after auto-stop
        steps = self.steps()
        estimate = planner._estimate_with_marginal_hold(self.e, steps, by_id, floor)
        self.assertEqual((steps[1].execution_state, steps[1].validation_gate), ("BLOCKED", "HELD_MARGINAL_SAVINGS_BELOW_MATERIALITY"))
        # The plan is priced without it: its savings are the last applied step's running savings.
        self.assertAlmostEqual(estimate.estimated_savings_range_365.base, steps[0].running_estimated_savings_range_365.base, places=2)
        kept = self.steps()
        planner._estimate_with_marginal_hold(self.e, kept, by_id, marginal - 1.0)
        self.assertEqual(kept[1].execution_state, "READY_FOR_CANARY")


if __name__ == "__main__":
    unittest.main()
