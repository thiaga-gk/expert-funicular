"""Running-idle evidence: idle gaps intersected with the warehouse's observed RUNNING time.

A lower auto-stop removes only idle time the warehouse actually spent running (an ops job, a scheduler or the
warehouse's own auto-stop may already have stopped it), and adds a restart only where the warehouse kept running up
to the next query. ``publisher.running_idle_metrics`` is the reference implementation of the collector's interval
arithmetic; the demo simulator computes its evidence with it, and these cases pin its semantics.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

DAY = 1440.0


def hm(day, hhmm):
    h, m = hhmm.split(":")
    return day * DAY + int(h) * 60 + int(m)


class RunningIdleOracleTests(unittest.TestCase):
    def metrics(self, busy, segments, window, auto_stop, targets=(10, 30)):
        return p.running_idle_metrics(busy, segments, window, auto_stop, targets)

    def test_manually_stopped_always_on_warehouse_credits_only_running_idle(self):
        # Auto-stop 0; queries 08:00-18:00; an ops job stops the warehouse at 19:00; it restarts at 08:00. Target 30:
        # per night only [18:30, 19:00) is running idle (30 minutes), not the 810 minutes the query gap suggests.
        days = 20
        busy = [(hm(d, "08:00"), hm(d, "18:00")) for d in range(days)]
        running = [(hm(d, "08:00"), hm(d, "19:00"), 1) for d in range(days)]
        m = self.metrics(busy, running, (hm(0, "08:00"), hm(days, "08:00")), 0)
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_30"], 30.0 * days, places=3)
        self.assertAlmostEqual(m["removable_running_cluster_minutes_if_30"], 30.0 * days, places=3)
        # The warehouse was already stopped before every next query: a 30-minute auto-stop adds no restart.
        self.assertEqual(m["additional_starts_if_30"], 0)
        self.assertAlmostEqual(m["observed_running_cluster_minutes"], 660.0 * days, places=3)

    def test_never_stopped_warehouse_removes_the_whole_tail_and_adds_one_start_per_gap(self):
        busy = [(hm(d, "08:00"), hm(d, "18:00")) for d in range(3)]
        running = [(hm(0, "08:00"), hm(3, "08:00"), 1)]
        m = self.metrics(busy, running, (hm(0, "08:00"), hm(3, "08:00")), 0)
        # Two overnight gaps (840 min) with a next query, plus the trailing gap after the last query (840 min).
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_30"], 3 * 810.0, places=3)
        self.assertEqual(m["additional_starts_if_30"], 2)  # the trailing gap has no next query to restart for

    def test_gap_when_the_warehouse_was_stopped_is_a_known_zero(self):
        busy = [(0.0, 10.0), (500.0, 510.0)]
        running = [(0.0, 10.0, 1), (500.0, 510.0, 1)]
        m = self.metrics(busy, running, (0.0, 510.0), 30)
        self.assertEqual(m["removable_running_wall_minutes_if_10"], 0.0)
        self.assertEqual(m["additional_starts_if_10"], 0)

    def test_idle_tail_at_two_clusters_counts_twice_the_cluster_minutes(self):
        busy = [(0.0, 10.0), (200.0, 210.0)]
        running = [(0.0, 40.0, 2), (200.0, 210.0, 2)]
        m = self.metrics(busy, running, (0.0, 210.0), 30)
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_10"], 20.0, places=3)
        self.assertAlmostEqual(m["removable_running_cluster_minutes_if_10"], 40.0, places=3)

    def test_overlapping_queries_leave_no_gap(self):
        busy = [(0.0, 50.0), (10.0, 20.0), (40.0, 100.0)]
        running = [(0.0, 100.0, 1)]
        m = self.metrics(busy, running, (0.0, 100.0), 0)
        self.assertEqual(m["removable_running_wall_minutes_if_10"], 0.0)

    def test_running_past_the_current_auto_stop_is_kept_alive_not_removable(self):
        # Auto-stop 30 but the warehouse ran the whole 120-minute gap: something other than queries kept it alive,
        # so a lower auto-stop removes only [10, 30) after the query and the rest is reported as kept alive.
        busy = [(0.0, 10.0), (130.0, 140.0)]
        running = [(0.0, 140.0, 1)]
        m = self.metrics(busy, running, (0.0, 140.0), 30)
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_10"], 20.0, places=3)
        self.assertEqual(m["additional_starts_if_10"], 0)
        self.assertAlmostEqual(m["kept_alive_wall_minutes"], 90.0, places=3)
        self.assertEqual(m["kept_alive_gap_count"], 1)

    def test_raising_auto_stop_avoids_restarts_and_adds_idle(self):
        # Auto-stop 10, a query every 14 minutes: the warehouse stops 10 minutes after each query and restarts 4
        # minutes later. At 15 it would stay up: one restart avoided and 4 idle minutes added per gap.
        busy = [(k * 15.0, k * 15.0 + 1.0) for k in range(5)]
        running = [(k * 15.0, k * 15.0 + 11.0, 1) for k in range(5)]
        m = self.metrics(busy, running, (0.0, 61.0), 10, targets=(15, 30))
        self.assertEqual(m["avoided_starts_if_15"], 4)
        self.assertAlmostEqual(m["added_idle_wall_minutes_if_15"], 4 * 4.0, places=3)
        self.assertEqual(m["removable_running_wall_minutes_if_15"], 0.0)

    def test_zero_query_window_is_one_trailing_gap_from_the_window_start(self):
        m = self.metrics([], [(0.0, 600.0, 1)], (0.0, 600.0), 0)
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_10"], 590.0, places=3)
        self.assertEqual(m["additional_starts_if_10"], 0)

    def test_gaps_are_clipped_to_the_evidence_window(self):
        busy = [(-100.0, -90.0), (50.0, 60.0)]
        running = [(0.0, 60.0, 1)]
        m = self.metrics(busy, running, (0.0, 60.0), 0)
        # The gap [-90, 50) only counts from the window start: removable [0, 50) at T=10 starts at -80 -> [0, 50).
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_10"], 50.0, places=3)


class RunningIdleSqlTests(unittest.TestCase):
    def setUp(self):
        self.t = p.Thresholds()
        self.sql = p.build_running_idle_sql(self.t)

    def test_every_grid_target_is_rendered(self):
        for target in p.auto_stop_targets(self.t):
            for key in ("removable_running_wall_minutes_if_", "removable_running_cluster_minutes_if_", "additional_starts_if_",
                        "avoided_starts_if_", "added_idle_wall_minutes_if_"):
                self.assertIn(f"{key}{target}", self.sql)

    def test_same_period_as_event_totals_and_millisecond_precision(self):
        self.assertIn("INTERVAL 365 DAYS", self.sql)
        self.assertIn("first_event_time", self.sql)
        self.assertIn("TIMESTAMPDIFF(MILLISECOND", self.sql)
        self.assertNotIn("TIMESTAMPDIFF(SECOND", self.sql)
        self.assertIn("ORDER BY event_time, event_type, cluster_count", self.sql)  # deterministic tie-break
        self.assertIn("epoch_end IS NULL", self.sql)  # current epoch only


class RunningIdleEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.fleet = p.synthetic_demo_evidence()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_demo_running_idle_covers_the_same_period_as_event_totals(self):
        targets = p.auto_stop_targets(self.t)
        for e in self.fleet:
            ep = e.current_epoch
            if "cluster_minutes" not in ep.events:  # no retained warehouse event: running time unknown
                self.assertNotIn("observed_running_cluster_minutes", ep.gaps, e.warehouse_name)
                continue
            self.assertAlmostEqual(ep.gaps["observed_running_cluster_minutes"], ep.events["cluster_minutes"], delta=0.01, msg=e.warehouse_name)
            for target in targets:
                self.assertLessEqual(ep.gaps[f"removable_running_cluster_minutes_if_{target}"], ep.events["cluster_minutes"] + 0.01, e.warehouse_name)
            self.assertIsNotNone(ep.gaps.get("running_idle_queries"), e.warehouse_name)

    def test_collector_merges_running_idle_rows_into_the_current_epoch(self):
        class Client:
            def query(self, statement, *, label="collector_sql"):
                if label == "config_epochs":
                    return [{"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                             "warehouse_channel": "CURRENT", "warehouse_size": "SMALL", "min_clusters": 1, "max_clusters": 1,
                             "auto_stop_minutes": 0, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}]
                if label == "running_idle":
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00",
                             "removable_running_wall_minutes_if_10": 42.0, "running_idle_queries": 7}]
                return []
        e = p.extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-10-01T12:00:00+00:00")[0]
        self.assertEqual(e.current_epoch.gaps["removable_running_wall_minutes_if_10"], 42.0)
        self.assertEqual(e.current_epoch.gaps["running_idle_queries"], 7)

    def test_core_demo_evidence_carries_running_idle_fields(self):
        ep = p.demo_evidence()[0].current_epoch
        for target in p.auto_stop_targets(self.t):
            self.assertIn(f"removable_running_cluster_minutes_if_{target}", ep.gaps)


if __name__ == "__main__":
    unittest.main()
