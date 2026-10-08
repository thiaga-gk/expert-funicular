"""Blocker codes that gate a decision, each asserted under its documented condition (docs/reference/publisher/blocker-codes.md).

The Analyzer's facts of a golden demo warehouse are patched to put one gate just over (and, as a control, just under) its
threshold, then the Optimizer decides: this tests the rule, not the evidence collector.
"""
import copy
import dataclasses
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.findings import Analyzer  # noqa: E402
from publisher.config.defaults import SIZE_ORDER, MAX_CLUSTERS_SUPPORTED_LIMIT  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.optimizer.estimator.cost import resolve_target_sku_price  # noqa: E402
from publisher.optimizer.optimizer import Optimizer  # noqa: E402


class BlockerCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.by = {e.current_epoch.tags["golden_scenario"]: e for e in synthetic_demo_evidence()}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def decide(self, scenario, facts=None, **config):
        """Decisions by knob for a golden warehouse after patching facts per knob (``{knob: {fact: value}}``) and config fields."""
        e = copy.deepcopy(self.by[scenario])
        analyses = Analyzer(self.t).analyze(e)
        for knob, patch in (facts or {}).items():
            analyses[knob].facts.update(patch)
        if config:
            e.current_epoch.config = dataclasses.replace(e.current_epoch.config, **config)
        return {d.knob: d for d in Optimizer(self.t).optimize(e, analyses)}

    # --- size -----------------------------------------------------------------------------------------------------

    def test_p99_latency_too_high_holds_a_size_cut(self):
        base = self.decide("C04")["warehouse_size"]
        self.assertEqual(base.status, "TEST_REQUIRED")  # the control: this warehouse would be cut one size
        over = self.decide("C04", {"warehouse_size": {"p99_exec": self.t.p99_execution_ms_downsize_safe + 1}})["warehouse_size"]
        self.assertEqual(over.status, "NO_CHANGE")
        self.assertIn("P99_LATENCY_TOO_HIGH", over.blockers)
        at = self.decide("C04", {"warehouse_size": {"p99_exec": self.t.p99_execution_ms_downsize_safe}})["warehouse_size"]
        self.assertNotIn("P99_LATENCY_TOO_HIGH", at.blockers)  # the threshold itself is still safe

    def test_already_at_max_known_size_blocks_an_upsize(self):
        spill = {"warehouse_size": {"spill_pct": self.t.spill_query_pct_upsize, "spill_p95": self.t.spill_p95_bytes_upsize}}
        top = self.decide("C04", spill, warehouse_size=SIZE_ORDER[-1])["warehouse_size"]
        self.assertEqual((top.status, top.blockers), ("BLOCKED", ["ALREADY_AT_MAX_KNOWN_SIZE"]))
        below = self.decide("C04", spill, warehouse_size=SIZE_ORDER[-2])["warehouse_size"]
        self.assertNotIn("ALREADY_AT_MAX_KNOWN_SIZE", below.blockers)  # a larger size exists
        quiet = self.decide("C04", {"warehouse_size": {"spill_pct": 0.0, "spill_p95": 0}}, warehouse_size=SIZE_ORDER[-1])["warehouse_size"]
        self.assertNotIn("ALREADY_AT_MAX_KNOWN_SIZE", quiet.blockers)  # no spill pressure, no upsize

    # --- max clusters ---------------------------------------------------------------------------------------------

    def test_max_cluster_limit_reached_blocks_an_increase_at_the_ceiling(self):
        self.assertEqual(self.decide("C08")["max_clusters"].status, "ACTIONABLE")  # control: saturated and queueing
        at_limit = self.decide("C08", max_clusters=MAX_CLUSTERS_SUPPORTED_LIMIT)["max_clusters"]
        self.assertEqual((at_limit.status, at_limit.blockers), ("BLOCKED", ["MAX_CLUSTER_LIMIT_REACHED"]))
        below = self.decide("C08", max_clusters=MAX_CLUSTERS_SUPPORTED_LIMIT - 1)["max_clusters"]
        self.assertNotIn("MAX_CLUSTER_LIMIT_REACHED", below.blockers)

    def test_a_thin_busy_minute_sample_holds_a_ceiling_increase_and_a_cut(self):
        thin = {"max_clusters": {"busy_minutes_recent": self.t.concurrency_min_busy_minutes - 1}}
        increase = self.decide("C08", thin)["max_clusters"]
        self.assertEqual((increase.status, increase.blockers), ("INSUFFICIENT_EVIDENCE", ["INSUFFICIENT_BUSY_MINUTE_SAMPLE"]))
        cut = self.decide("C09", thin)["max_clusters"]
        self.assertEqual(cut.status, "NO_CHANGE")
        self.assertIn("INSUFFICIENT_BUSY_MINUTE_SAMPLE", cut.blockers)
        enough = {"max_clusters": {"busy_minutes_recent": self.t.concurrency_min_busy_minutes}}
        self.assertEqual(self.decide("C09", enough)["max_clusters"].status, "ACTIONABLE")  # the threshold itself is enough

    def test_missing_concurrency_evidence_holds_ceiling_changes(self):
        gone = {"max_clusters": {"concurrency_available": False}}
        increase = self.decide("C08", gone)["max_clusters"]
        self.assertEqual((increase.status, increase.blockers), ("INSUFFICIENT_EVIDENCE", ["CONCURRENCY_EVIDENCE_UNAVAILABLE"]))
        cut = self.decide("C09", gone)["max_clusters"]
        self.assertEqual(cut.status, "NO_CHANGE")
        self.assertIn("CONCURRENCY_EVIDENCE_UNAVAILABLE", cut.blockers)

    # --- min clusters ---------------------------------------------------------------------------------------------

    def test_missing_concurrency_evidence_holds_a_floor_cut_for_every_type(self):
        for scenario in ("C10", "C11"):  # Serverless and Pro
            with self.subTest(scenario):
                self.assertEqual(self.decide(scenario)["min_clusters"].status, "TEST_REQUIRED")  # control
                held = self.decide(scenario, {"min_clusters": {"concurrency_available": False}})["min_clusters"]
                self.assertEqual(held.status, "NO_CHANGE")
                self.assertIn("CONCURRENCY_EVIDENCE_UNAVAILABLE", held.blockers)

    def test_sustained_scale_pressure_holds_a_floor_cut(self):
        pressure = self.t.time_at_max_pct_block_min_reduce
        held = self.decide("C10", {"min_clusters": {"at_max_pct": pressure}})["min_clusters"]
        self.assertEqual(held.status, "NO_CHANGE")
        self.assertIn("SUSTAINED_SCALE_PRESSURE", held.blockers)
        below = self.decide("C10", {"min_clusters": {"at_max_pct": pressure - 0.1}})["min_clusters"]
        self.assertNotIn("SUSTAINED_SCALE_PRESSURE", below.blockers)

    def test_a_pinned_warehouse_is_exempt_from_sustained_scale_pressure(self):
        pinned = self.decide("C11", {"min_clusters": {"at_max_pct": 100.0}})["min_clusters"]  # C11 runs 3/3
        self.assertNotIn("SUSTAINED_SCALE_PRESSURE", pinned.blockers)

    def test_a_warehouse_rarely_at_its_minimum_has_no_floor_to_remove(self):
        rare = self.decide("C10", {"min_clusters": {"at_min_pct": self.t.time_at_min_pct_reduce - 0.1}})["min_clusters"]
        self.assertEqual(rare.status, "NO_CHANGE")
        self.assertIn("WAREHOUSE_RARELY_AT_MINIMUM", rare.blockers)
        enough = self.decide("C10", {"min_clusters": {"at_min_pct": self.t.time_at_min_pct_reduce}})["min_clusters"]
        self.assertNotIn("WAREHOUSE_RARELY_AT_MINIMUM", enough.blockers)

    # --- warehouse type -------------------------------------------------------------------------------------------

    def test_a_serverless_to_pro_experiment_needs_an_observed_pro_or_classic_warehouse(self):
        self.assertEqual(self.by["C02"].current_epoch.config.warehouse_type, "SERVERLESS")
        unconfirmed = self.decide("C02", {"warehouse_type": {"pro_classic_observed": False}})["warehouse_type"]
        self.assertEqual(unconfirmed.status, "TEST_REQUIRED")
        self.assertEqual(unconfirmed.blockers, ["A_B_COST_PERFORMANCE_VALIDATION_REQUIRED", "PRO_CLASSIC_WORKSPACE_CAPABILITY_NOT_CONFIRMED_FROM_SYSTEM_TABLES"])
        confirmed = self.decide("C02", {"warehouse_type": {"pro_classic_observed": True}})["warehouse_type"]
        self.assertEqual(confirmed.blockers, ["A_B_COST_PERFORMANCE_VALIDATION_REQUIRED"])

    # --- estimator ------------------------------------------------------------------------------------------------

    def test_several_different_target_sku_prices_are_ambiguous_never_averaged(self):
        prices = {"PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST": {"effective_list_price": 0.70},
                  "ENTERPRISE_SERVERLESS_SQL_COMPUTE_US_EAST": {"effective_list_price": 0.75}}
        price, info = resolve_target_sku_price(prices, "SERVERLESS")
        self.assertIsNone(price)
        self.assertEqual((info["status"], info["blocker"]), ("UNAVAILABLE", "AMBIGUOUS_TARGET_SQL_SKU_PRICE"))
        same = dict(prices, ENTERPRISE_SERVERLESS_SQL_COMPUTE_US_EAST={"effective_list_price": 0.70})
        self.assertEqual(resolve_target_sku_price(same, "SERVERLESS")[0], 0.70)  # equal prices are not ambiguous
        self.assertEqual(resolve_target_sku_price({}, "SERVERLESS")[1]["blocker"], "TARGET_SQL_SKU_PRICE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
