"""Size pricing and the value gate for cost-objective changes.

A size change is priced with the workspace's billed DBU per cluster-hour only when those rates rise with size and the
pair ratio stays within a band of the published size ratio; the speed-up multipliers follow the decided direction; no
historical-epoch calibration enters the price. A change made to save money ships only when its own modeled savings
are positive and at least the materiality floor.
"""
import copy
import dataclasses
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


def candidates_by_scenario(evidence, t):
    out = {}
    for w in p.evaluate_warehouses(evidence, t):
        e = next(x for x in evidence if x.warehouse_id == w.warehouse_id and x.workspace_id == w.workspace_id)
        out[e.current_epoch.tags["golden_scenario"]] = {c.knob: c for c in w.candidates}
    return out


class SizeRateValidationTests(unittest.TestCase):
    RATES = {"SMALL": 12.1, "MEDIUM": 23.8, "LARGE": 40.5}

    def test_validated_ratio_uses_observed_rates(self):
        ratio, reason = p.validated_size_rate_ratio(self.RATES, "LARGE", "MEDIUM", 0.25)
        self.assertEqual(reason, "VALIDATED")
        self.assertAlmostEqual(ratio, 23.8 / 40.5, places=6)

    def test_rates_that_do_not_rise_with_size_are_rejected(self):
        # Billing-observed MEDIUM 245 above LARGE 198: a downsize would be priced as a bigger cluster.
        ratio, reason = p.validated_size_rate_ratio({"SMALL": 164.0, "MEDIUM": 245.0, "LARGE": 198.0}, "LARGE", "MEDIUM", 0.25)
        self.assertIsNone(ratio)
        self.assertEqual(reason, "RATES_NOT_RISING_WITH_SIZE")

    def test_unobserved_target_size_is_rejected(self):
        self.assertEqual(p.validated_size_rate_ratio(self.RATES, "SMALL", "X_SMALL", 0.25), (None, "SIZE_RATE_NOT_OBSERVED"))

    def test_pair_outside_the_band_of_the_published_ratio_is_rejected(self):
        # Published MEDIUM/SMALL = 0.5; observed 16/24 = 0.667 is 33% off.
        self.assertEqual(p.validated_size_rate_ratio({"SMALL": 16.0, "MEDIUM": 24.0}, "MEDIUM", "SMALL", 0.25), (None, "OUTSIDE_PUBLISHED_RATIO_BAND"))
        ratio, reason = p.validated_size_rate_ratio({"SMALL": 14.0, "MEDIUM": 24.0}, "MEDIUM", "SMALL", 0.25)  # 17% off: inside
        self.assertEqual(reason, "VALIDATED")


class SizePricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.fleet = p.synthetic_demo_evidence()
        cls.by = candidates_by_scenario(cls.fleet, cls.t)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_workspace_rate_is_measured_on_the_event_covered_window(self):
        # Demo billing follows cluster-hours at the published rate (+-3%), so measured rates match the table.
        for e in self.fleet:
            for size, rate in (e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}).items():
                self.assertLess(abs(rate / p.SQL_DBU_PER_CLUSTER_HOUR[size] - 1.0), 0.03, (e.workspace_id, size))

    def test_downsize_uses_downsize_multipliers_and_the_validated_ratio(self):
        size = self.by["C06"]["warehouse_size"]  # LARGE -> MEDIUM, both sizes run in the workspace
        self.assertEqual(size.recommended_config["warehouse_size"], "MEDIUM")
        m = size.metadata
        self.assertEqual(m["capacity_ratio_source"], "WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR_RATIO")
        self.assertLess(abs(m["capacity_ratio"] / (24.0 / 40.0) - 1.0), 0.05)
        busy = {"low": self.t.downsize_runtime_multiplier_optimistic, "base": self.t.downsize_runtime_multiplier_base, "high": self.t.downsize_runtime_multiplier_conservative}
        self.assertEqual(m["busy_runtime_multiplier_range"], busy)
        share = m["busy_share"]  # the slow-down stretches busy running time only
        self.assertTrue(0 < share < 1)
        for k, v in busy.items():
            self.assertAlmostEqual(m["runtime_multiplier_range"][k], share * v + (1 - share), places=5)
        self.assertNotIn("historical_epoch_calibration", m)

    def test_unverified_size_rate_withholds_the_estimate(self):
        size = self.by["C30"]["warehouse_size"]  # SMALL -> X_SMALL: no X_SMALL warehouse runs in the workspace
        self.assertEqual(size.recommended_config["warehouse_size"], "X_SMALL")
        self.assertIsNone(size.estimated_savings_365)
        self.assertEqual(size.metadata["estimate_blocker"], "PRO_CLASSIC_SIZE_RATE_UNVERIFIED")
        self.assertEqual(size.metadata["size_rate_check"], "SIZE_RATE_NOT_OBSERVED")
        self.assertEqual(size.metadata["bundle_effect"], {"type": "requires_canary"})

    def test_non_monotone_rates_withhold_instead_of_pricing_a_downsize_as_an_upsize(self):
        fleet = copy.deepcopy(self.fleet)
        c06 = next(e for e in fleet if e.current_epoch.tags["golden_scenario"] == "C06")
        for e in fleet:  # bill the workspace's LARGE warehouses at half their rate: LARGE now looks cheaper than MEDIUM
            if e.workspace_id == c06.workspace_id and e.current_epoch.config.warehouse_size == "LARGE":
                for r in e.daily_cost_365:
                    r["dbu_quantity"] = r["dbu_quantity"] / 2.0
        size = candidates_by_scenario(fleet, self.t)["C06"]["warehouse_size"]
        self.assertIsNone(size.estimated_savings_365)
        self.assertEqual(size.metadata["size_rate_check"], "RATES_NOT_RISING_WITH_SIZE")

    def test_size_price_ignores_historical_epochs(self):
        # The core fixture has a matched prior MEDIUM epoch; its cost per 1,000 queries no longer enters the price.
        size = {c.knob: c for c in p.evaluate_warehouses(p.demo_evidence(), self.t)[0].candidates}["warehouse_size"]
        self.assertNotIn("historical_epoch_calibration", size.metadata)
        self.assertFalse(hasattr(p.Estimator, "_matched_epoch_factor"))


class CostValueGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.fleet = p.synthetic_demo_evidence()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_cost_change_with_negative_savings_does_not_ship(self):
        # A downsize that runs so much slower it costs more: the change is held, the modeled loss stays visible.
        # (Busy time is about 30% of C06's running time, so queries must run 5x slower to outweigh the 0.6 price ratio.)
        t = dataclasses.replace(p.Thresholds(), downsize_runtime_multiplier_optimistic=5.0, downsize_runtime_multiplier_base=5.0, downsize_runtime_multiplier_conservative=5.0)
        size = candidates_by_scenario(self.fleet, t)["C06"]["warehouse_size"]
        self.assertEqual(size.status, "NO_CHANGE")
        self.assertEqual(size.recommended_config, size.current_config)
        self.assertIn("COST_CHANGE_NOT_COST_EFFECTIVE", size.blockers)
        self.assertLess(size.metadata["value_gate"]["modeled_savings_365"], 0)
        self.assertIn(size.estimated_savings_365, (None, 0, 0.0))

    def test_cost_change_below_the_materiality_floor_does_not_ship(self):
        t = dataclasses.replace(p.Thresholds(), cost_change_min_annual_savings=10 ** 9)
        by = candidates_by_scenario(self.fleet, t)
        held = [(s, k) for s, cands in by.items() for k, c in cands.items() if "COST_SAVINGS_BELOW_MATERIALITY" in c.blockers]
        self.assertTrue(held)
        for s, cands in by.items():
            for c in cands.values():
                if c.objective == "COST":
                    self.assertNotIn(c.status, {"ACTIONABLE", "TEST_REQUIRED"} if c.estimated_savings_365 is not None else set(), (s, c.knob))

    def test_gate_leaves_non_cost_and_unpriced_changes_alone(self):
        t = dataclasses.replace(p.Thresholds(), cost_change_min_annual_savings=10 ** 9)
        by = candidates_by_scenario(self.fleet, t)
        self.assertEqual(by["C09"]["max_clusters"].status, "ACTIONABLE")  # ceiling cut: guardrail, no priced saving
        self.assertEqual(by["C30"]["warehouse_size"].status, "TEST_REQUIRED")  # unpriced canary: nothing to gate on

    def test_default_floor_is_500(self):
        self.assertEqual(p.Thresholds().cost_change_min_annual_savings, 500.0)
        self.assertEqual(p.Thresholds().size_rate_published_band, 0.25)


class UpsizeCanaryAndBusyTimeTests(unittest.TestCase):
    """An upsize is a canary, and the size speed-up (or slow-down) applies to busy running time only."""

    def setUp(self):
        self.t = p.Thresholds()

    def spill_serverless(self, busy_minutes):
        # Core fixture as Serverless LARGE with spill: one size up (X_LARGE) at the published 2.0 DBU ratio.
        e = p.demo_evidence()[0]
        for ep in e.epochs:
            ep.config = dataclasses.replace(ep.config, warehouse_type="SERVERLESS", min_clusters=1)
        q = e.current_epoch.query
        q.update({"spill_query_pct_30": 6.0, "spill_queries_30": 300, "p95_spill_bytes_when_spilled_30": 4 * p.GIB})
        if busy_minutes is None:
            e.current_epoch.gaps.pop("busy_minutes", None)
        else:
            e.current_epoch.gaps["busy_minutes"] = busy_minutes
        return e

    def size(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["warehouse_size"]

    def test_busy_share_of_cluster_time(self):
        # Single cluster, 20,000 running minutes of which 10,000 busy: half the cluster time is busy.
        self.assertAlmostEqual(p.size_busy_share(10000.0, 20000.0, 20000.0, 1), 0.5)
        # Two-cluster floor: idle time runs min clusters, so 10,000 idle wall minutes are 20,000 idle cluster-minutes.
        self.assertAlmostEqual(p.size_busy_share(10000.0, 20000.0, 50000.0, 2), 0.6)
        # Busy time above running time (inconsistent sources) counts as all busy; unknown stays unknown.
        self.assertEqual(p.size_busy_share(25000.0, 20000.0, 20000.0, 1), 1.0)
        self.assertIsNone(p.size_busy_share(None, 20000.0, 20000.0, 1))
        self.assertIsNone(p.size_busy_share(100.0, 0.0, 0.0, 1))
        # Cluster count below the floor (sources disagree): busy cluster-minutes are never below busy wall-minutes.
        self.assertAlmostEqual(p.size_busy_share(100.0, 1000.0, 1500.0, 2), 100.0 / 1500.0, places=6)

    def test_upsize_is_a_canary_not_actionable(self):
        size = self.size(self.spill_serverless(5000.0))
        self.assertEqual(size.recommended_config, {"warehouse_size": "X_LARGE"})
        self.assertEqual(size.status, "TEST_REQUIRED")
        self.assertIn("A_B_PERFORMANCE_VALIDATION_REQUIRED", size.blockers)

    def test_upsize_speed_up_applies_to_busy_time_only(self):
        # uptime 20,000, cluster 30,000, busy 5,000: idle cluster-minutes 15,000, busy share 0.5.
        # Base cost factor 2.0 x (0.5 x 0.70 + 0.5) = 1.70, not 2.0 x 0.70 = 1.40.
        m = self.size(self.spill_serverless(5000.0)).metadata
        self.assertAlmostEqual(m["busy_share"], 0.5)
        self.assertAlmostEqual(m["runtime_multiplier_range"]["base"], 0.85)
        self.assertAlmostEqual(m["runtime_multiplier_range"]["low"], 0.75)
        self.assertAlmostEqual(m["runtime_multiplier_range"]["high"], 1.0)
        self.assertEqual(m["busy_runtime_multiplier_range"], {"low": 0.5, "base": 0.7, "high": 1.0})

    def test_unknown_busy_time_treats_all_running_time_as_busy(self):
        m = self.size(self.spill_serverless(None)).metadata
        self.assertIsNone(m["busy_share"])
        self.assertAlmostEqual(m["runtime_multiplier_range"]["base"], 0.70)

    def test_downsize_slow_down_applies_to_busy_time_only(self):
        e = p.demo_evidence()[0]  # LARGE -> MEDIUM canary, min 2 clusters
        e.current_epoch.gaps["busy_minutes"] = 12000.0
        m = self.size(e).metadata
        share = m["busy_share"]
        self.assertGreater(share, 0.0); self.assertLess(share, 1.0)
        self.assertAlmostEqual(m["runtime_multiplier_range"]["base"], share * 1.30 + (1 - share), places=6)

    def test_gap_collector_reports_busy_wall_minutes_for_every_epoch_with_queries(self):
        sql = p.build_epoch_gap_metrics_sql(self.t)
        self.assertIn("AS busy_minutes", sql)
        self.assertIn("LEFT JOIN gap_stats", sql)
        # Whole-second TIMESTAMPDIFF would drop most of every sub-second query's contribution.
        busy = sql[sql.index("busy AS ("):sql.index("), gaps AS (")]
        self.assertIn("TIMESTAMPDIFF(MILLISECOND,", busy)
        self.assertNotIn("TIMESTAMPDIFF(SECOND,", busy)


class DownsizeScaleOutTests(unittest.TestCase):
    def test_downsize_on_a_busy_multi_cluster_warehouse_prices_the_extra_scale_out(self):
        # Core demo: Pro LARGE -> MEDIUM (price ratio 0.6), min 2 / max 4, now fully busy. Slower queries raise
        # concurrency, so the autoscaler runs proportionally more cluster-minutes: the slowdown applies to all
        # (cluster-weighted) busy time. Base saving is 1 - 0.6 x 1.3 = 22%, not the 40% the price ratio alone suggests
        # (and the conservative bound 1 - 0.6 x 1.55 = 7%).
        logging.disable(logging.CRITICAL)
        try:
            e = p.demo_evidence()[0]
            e.current_epoch.gaps["busy_minutes"] = e.current_epoch.events["uptime_minutes"]
            c = next(c for c in p.evaluate_warehouses([e], p.Thresholds())[0].candidates if c.knob == "warehouse_size")
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(c.metadata["busy_share"], 1.0)
        f = c.metadata["cost_factor_range"]
        self.assertAlmostEqual(f["base"], 0.78, places=6)
        self.assertAlmostEqual(f["high"], 0.93, places=6)


if __name__ == "__main__":
    unittest.main()
