"""Per-minute concurrency (running + queued queries) and the cluster targets derived from it.

``publisher.concurrency_histograms`` is the reference implementation of the concurrency collector; the demo simulator
computes its evidence with it. Pro/Classic ceilings go in one move to peak demand (P99.5 of busy minutes, one cluster
per 10 queries) plus one cluster; floors go to the P95 of busy-period starts, before the autoscaler can add clusters.
"""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.queries.query_history import CONCURRENCY_SCOPES, build_concurrency_sql  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.analyzer.metrics import concurrency_histograms, histogram_percentile  # noqa: E402
from publisher.common.time import parse_datetime  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from tests.golden import load_case  # noqa: E402


class ConcurrencyOracleTests(unittest.TestCase):
    def test_minutes_are_time_weighted_and_rounded_up(self):
        # 3 queries for 10 minutes, a 20-query burst for one minute inside them, one query 100-103.
        h = concurrency_histograms([(0, 10, 3), (5, 6, 20), (100, 103, 1)], (0, 200), 0, 5)
        self.assertEqual(h["EPOCH"], {"3": 9.0, "23": 1.0, "1": 3.0})
        self.assertEqual(h["RECENT"], h["EPOCH"])
        # Two busy periods (idle more than 5 minutes between them); peaks in their first 5 minutes are 3 and 1.
        self.assertEqual(h["BUSY_START"], {"3": 1.0, "1": 1.0})

    def test_sub_minute_queries_count_as_a_fraction_of_the_minute(self):
        h = concurrency_histograms([(0.0, 0.1, 1), (0.5, 0.6, 1)], (0, 60), None, 5)
        self.assertEqual(h, {"EPOCH": {"1": 1.0}})  # 0.2 query-minutes -> level 1; no recent window for older epochs

    def test_percentile_reads_the_histogram(self):
        self.assertEqual(histogram_percentile({"1": 990, "20": 10}, 99.5), 20)
        self.assertEqual(histogram_percentile({"1": 996, "20": 4}, 99.5), 1)
        self.assertIsNone(histogram_percentile({}, 99.5))
        self.assertIsNone(histogram_percentile(None, 99.5))

    def test_sql_shape(self):
        sql = build_concurrency_sql(Thresholds())
        for scope in CONCURRENCY_SCOPES:
            self.assertIn(f"'{scope}' AS scope", sql)
        self.assertIn("TIMESTAMPDIFF(MILLISECOND", sql)
        self.assertIn("INTERVAL 30 DAYS", sql)
        self.assertIn("WHEN warehouse_type = 'SERVERLESS' THEN 1 ELSE 5 END", sql)


class ClusterTargetDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.by = {}
        for e in synthetic_demo_evidence():
            cls.by[e.current_epoch.tags["golden_scenario"]] = {c.knob: c for c in p.evaluate_warehouses([e], cls.t)[0].candidates}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def move(self, scenario, knob):
        c = self.by[scenario][knob]
        return c.status, c.current_config[knob], c.recommended_config[knob]

    def test_c08_concurrency_constrained_ceiling_moves_once_to_peak_demand(self):
        # 18 queries all day plus 12 for two hours (~33 at P99.5): 4 clusters + 1.
        self.assertEqual(self.move("C08", "max_clusters"), ("ACTIONABLE", 2, 5))

    def test_c09_unused_ceiling_drops_to_peak_demand(self):
        self.assertEqual(self.move("C09", "max_clusters"), ("ACTIONABLE", 10, 3))

    def test_c42_queue_flood_ceiling_from_concurrency_capped_at_forty(self):
        self.assertEqual(self.move("C42", "max_clusters"), ("ACTIONABLE", 2, 40))

    def test_c10_serverless_floor_goes_to_one(self):
        self.assertEqual(self.move("C10", "min_clusters"), ("TEST_REQUIRED", 4, 1))

    def test_c11_pinned_warehouse_floor_from_busy_start_demand(self):
        # Pinned 3/3 with ~10 concurrent queries: the floor drops to 1 (pressure judged on queueing only); the ceiling
        # cannot go below the current floor until the floor change is applied.
        self.assertEqual(self.move("C11", "min_clusters"), ("TEST_REQUIRED", 3, 1))
        self.assertEqual(self.move("C11", "max_clusters"), ("NO_CHANGE", 3, 3))

    def test_c45_steady_load_keeps_its_floor(self):
        self.assertEqual(self.move("C45", "min_clusters")[0], "NO_CHANGE")


class OlderEpochHoldTests(unittest.TestCase):
    """Core demo warehouse: recent peak 20 queries (2 clusters); max widened to 8 so a cut to 3 is on the table."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Thresholds()

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def evidence(self, older_epoch_hist=None, current_epoch_hist=None):
        import dataclasses
        e = demo_evidence()[0]
        for ep in e.epochs:
            ep.config = dataclasses.replace(ep.config, max_clusters=8)
        if older_epoch_hist is not None:
            e.epochs[0].query["concurrency_histogram"] = {"EPOCH": older_epoch_hist}
        if current_epoch_hist is not None:
            e.current_epoch.query["concurrency_histogram"]["EPOCH"] = current_epoch_hist
        return e

    def candidates(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}

    def test_older_epoch_with_more_clusters_of_demand_holds_every_capacity_cut(self):
        by = self.candidates(self.evidence({"1": 5000.0, "45": 60.0}))  # 45 queries for an hour: 5 clusters vs 2
        self.assertEqual(by["max_clusters"].status, "NO_CHANGE")
        self.assertIn("OLDER_EPOCH_PEAK_DEMAND_HIGHER", by["max_clusters"].blockers)
        self.assertIn("OLDER_EPOCH_PEAK_DEMAND_HIGHER", by["min_clusters"].blockers)
        self.assertIn("OLDER_EPOCH_PEAK_DEMAND_HIGHER", by["warehouse_size"].blockers)
        self.assertEqual(by["auto_stop_minutes"].status, "ACTIONABLE")  # not a capacity cut

    def test_holds_compare_clusters_not_queries(self):
        # Recent peak 20 queries = 2 clusters. An older 19 is also 2 clusters: no hold. An older 21 needs 3: held.
        by = self.candidates(self.evidence({"1": 5000.0, "19": 60.0}))
        self.assertEqual(by["max_clusters"].recommended_config["max_clusters"], 3)
        by = self.candidates(self.evidence({"1": 5000.0, "21": 60.0}))
        self.assertIn("OLDER_EPOCH_PEAK_DEMAND_HIGHER", by["max_clusters"].blockers)

    def test_a_short_older_spike_is_not_a_peak(self):
        by = self.candidates(self.evidence({"1": 5000.0, "80": 10.0}))  # 10 minutes only
        self.assertEqual(by["max_clusters"].recommended_config["max_clusters"], 3)

    def test_same_spike_rate_over_a_longer_older_epoch_does_not_hold(self):
        # Spikes to 45 queries for 20 minutes a month both now and in a 106-day older epoch (~70 minutes there): the
        # older 30-minute peak (45) is compared with the recent peak read at the same rate (~8 minutes: also 45).
        e = self.evidence({"1": 140000.0, "15": 2800.0, "25": 640.0, "45": 70.0})
        e.current_epoch.query["concurrency_histogram"]["RECENT"] = {"1": 39000.0, "15": 800.0, "25": 180.0, "45": 20.0}
        by = self.candidates(e)
        self.assertNotIn("OLDER_EPOCH_PEAK_DEMAND_HIGHER", by["max_clusters"].blockers)

    def test_a_quiet_recent_window_does_not_hide_a_busier_past(self):
        e = self.evidence({"1": 5000.0, "45": 600.0})
        e.current_epoch.query["concurrency_histogram"]["RECENT"] = {"1": 25.0}
        self.assertIn("OLDER_EPOCH_PEAK_DEMAND_HIGHER", self.candidates(e)["min_clusters"].blockers)

    def test_a_young_current_epoch_reads_its_recent_peak_over_its_own_age(self):
        # 15-day current epoch whose recent window and epoch are the same 15 days: the epoch peak is not "outside".
        import datetime as dt
        e = self.evidence()
        start = (parse_datetime(e.analysis_time) - dt.timedelta(days=15)).isoformat()
        e.current_epoch.epoch_start = start
        e.epochs[0].epoch_end = start
        e.epochs[0].query.pop("concurrency_histogram", None)
        same = {"1": 20000.0, "15": 800.0, "25": 150.0, "35": 50.0}
        e.current_epoch.query["concurrency_histogram"].update(RECENT=dict(same), EPOCH=dict(same))
        self.assertNotIn("PEAK_OUTSIDE_RECENT_WINDOW", self.candidates(e)["max_clusters"].blockers)

    def test_quarter_end_peak_in_the_current_epoch_keeps_the_ceiling(self):
        # 2 hours at 72 queries out of 150,000 busy minutes (0.08%): P99.5 misses it, the 30-busy-minute peak does not.
        by = self.candidates(self.evidence(current_epoch_hist={"1": 149880.0, "72": 120.0}))
        self.assertEqual(by["max_clusters"].status, "NO_CHANGE")
        self.assertIn("PEAK_OUTSIDE_RECENT_WINDOW", by["max_clusters"].blockers)


class ServerlessCeilingConfidenceTests(unittest.TestCase):
    def test_serverless_queue_based_increase_is_low_confidence(self):
        import json
        logging.disable(logging.CRITICAL)
        try:
            payload = load_case("concurrency-constrained")
            e = evidence_from_dict(payload["evidence"][0])
            import dataclasses
            for ep in e.epochs:
                ep.config = dataclasses.replace(ep.config, warehouse_type="SERVERLESS")
            c = next(c for c in p.evaluate_warehouses([e], Thresholds())[0].candidates if c.knob == "max_clusters")
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertEqual(c.recommended_config["max_clusters"], c.current_config["max_clusters"] + 1)
        self.assertEqual(c.confidence["level"], "LOW")


if __name__ == "__main__":
    unittest.main()
