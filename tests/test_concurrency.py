"""Per-minute concurrency (running + queued queries) and the cluster targets derived from it.

``publisher.concurrency_histograms`` is the reference implementation of the concurrency collector; the demo simulator
computes its evidence with it. Pro/Classic ceilings go in one move to peak demand (P99.5 of busy minutes, one cluster
per 10 queries) plus one cluster; floors go to the P95 of busy-period starts, before the autoscaler can add clusters.
"""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


class ConcurrencyOracleTests(unittest.TestCase):
    def test_minutes_are_time_weighted_and_rounded_up(self):
        # 3 queries for 10 minutes, a 20-query burst for one minute inside them, one query 100-103.
        h = p.concurrency_histograms([(0, 10, 3), (5, 6, 20), (100, 103, 1)], (0, 200), 0, 5)
        self.assertEqual(h["EPOCH"], {"3": 9.0, "23": 1.0, "1": 3.0})
        self.assertEqual(h["RECENT"], h["EPOCH"])
        # Two busy periods (idle more than 5 minutes between them); peaks in their first 5 minutes are 3 and 1.
        self.assertEqual(h["BUSY_START"], {"3": 1.0, "1": 1.0})

    def test_sub_minute_queries_count_as_a_fraction_of_the_minute(self):
        h = p.concurrency_histograms([(0.0, 0.1, 1), (0.5, 0.6, 1)], (0, 60), None, 5)
        self.assertEqual(h, {"EPOCH": {"1": 1.0}})  # 0.2 query-minutes -> level 1; no recent window for older epochs

    def test_percentile_reads_the_histogram(self):
        self.assertEqual(p.histogram_percentile({"1": 990, "20": 10}, 99.5), 20)
        self.assertEqual(p.histogram_percentile({"1": 996, "20": 4}, 99.5), 1)
        self.assertIsNone(p.histogram_percentile({}, 99.5))
        self.assertIsNone(p.histogram_percentile(None, 99.5))

    def test_sql_shape(self):
        sql = p.build_concurrency_sql(p.Thresholds())
        for scope in p.CONCURRENCY_SCOPES:
            self.assertIn(f"'{scope}' AS scope", sql)
        self.assertIn("TIMESTAMPDIFF(MILLISECOND", sql)
        self.assertIn("INTERVAL 30 DAYS", sql)
        self.assertIn("WHEN warehouse_type = 'SERVERLESS' THEN 1 ELSE 5 END", sql)


class ClusterTargetDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.by = {}
        for e in p.synthetic_demo_evidence():
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


if __name__ == "__main__":
    unittest.main()
