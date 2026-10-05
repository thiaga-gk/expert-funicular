"""Running-idle evidence: idle gaps intersected with the warehouse's observed RUNNING time.

A lower auto-stop removes only idle time the warehouse actually spent running (an ops job, a scheduler or the
warehouse's own auto-stop may already have stopped it), and adds a restart only where the warehouse kept running up
to the next query. ``publisher.running_idle_metrics`` is the reference implementation of the collector's interval
arithmetic; the demo simulator computes its evidence with it, and these cases pin its semantics.
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
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.queries.events import RUNNING_IDLE_HEADER_FIELDS, RUNNING_IDLE_TARGET_FIELDS, build_running_idle_sql, build_running_idle_targets_sql  # noqa: E402
from publisher.analyzer.metrics import auto_stop_candidates, auto_stop_targets, running_idle_metrics  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.reviewer.reviewer import Reviewer  # noqa: E402

DAY = 1440.0


def hm(day, hhmm):
    h, m = hhmm.split(":")
    return day * DAY + int(h) * 60 + int(m)


class RunningIdleOracleTests(unittest.TestCase):
    def metrics(self, busy, segments, window, auto_stop, targets=(10, 30)):
        m = running_idle_metrics(busy, segments, window, auto_stop, targets)
        zero = dict.fromkeys(RUNNING_IDLE_TARGET_FIELDS, 0)
        for t in targets:  # flatten the sparse per-value rows (absent = 0) for readable assertions
            for k, v in (m["running_idle_by_target"].get(str(t)) or zero).items():
                m[f"{k}_if_{t}"] = v
        return m

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
        # Auto-stop 30 but the warehouse ran the whole 120-minute gap: something other than queries kept it alive, and
        # it would keep it alive past a lower value too, so nothing is removable and no restart is added.
        busy = [(0.0, 10.0), (130.0, 140.0)]
        running = [(0.0, 140.0, 1)]
        m = self.metrics(busy, running, (0.0, 140.0), 30)
        self.assertEqual(m["removable_running_wall_minutes_if_10"], 0.0)
        self.assertEqual(m["additional_starts_if_10"], 0)
        self.assertAlmostEqual(m["kept_alive_wall_minutes"], 90.0, places=3)
        self.assertEqual(m["kept_alive_gap_count"], 1)

    def test_raising_auto_stop_avoids_restarts_and_adds_idle(self):
        # Auto-stop 10, a query every 14 minutes: the warehouse stops 10 minutes after each query and restarts 4
        # minutes later. At 15 it would stay up: one restart avoided and 4 idle minutes added per gap.
        busy = [(k * 15.0, k * 15.0 + 1.0) for k in range(5)]
        running = [(k * 15.0, k * 15.0 + 11.0, 1) for k in range(5)]
        m = self.metrics(busy, running, (0.0, 61.0), 10, targets=(15, 16, 30))
        # 15 covers a 14-minute gap with less than the 10% margin: no restart counted as saved; 16 covers it.
        self.assertEqual(m["avoided_starts_if_15"], 0)
        self.assertEqual(m["avoided_starts_if_16"], 4)
        self.assertAlmostEqual(m["added_idle_wall_minutes_if_16"], 4 * 4.0, places=3)
        self.assertEqual(m["removable_running_wall_minutes_if_16"], 0.0)

    def test_a_value_just_above_the_gap_still_counts_the_restart_without_the_margin(self):
        # Warehouse never stops (auto-stop 0), 87-minute gaps: 90 is under 1.1 x 87, so its restarts still count.
        busy = [(k * 90.0, k * 90.0 + 3.0) for k in range(5)]
        m = self.metrics(busy, [(0.0, 363.0, 1)], (0.0, 363.0), 0, targets=(90, 100))
        self.assertEqual(m["additional_starts_if_90"], 4)
        self.assertEqual(m["additional_starts_if_100"], 0)

    def test_a_stop_event_trailing_the_auto_stop_is_not_kept_alive(self):
        # Auto-stop 30; the stop event lands 1.5 minutes late (within the 2-minute tolerance): still removable.
        busy = [(0.0, 10.0), (200.0, 210.0)]
        m = self.metrics(busy, [(0.0, 41.5, 1), (200.0, 210.0, 1)], (0.0, 210.0), 30)
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_10"], 20.0, places=3)
        self.assertEqual(m["kept_alive_gap_count"], 0)
        self.assertEqual(m["restarts_for_queries"], 1)

    def test_a_late_stop_event_in_a_stopped_gap_keeps_its_savings(self):
        # Auto-stop 30; the stop event lands 5 minutes late but the warehouse did stop before the next query: removable
        # [10, 30) stays, and the gap is not kept alive (only gaps run through to the next query can be).
        busy = [(0.0, 10.0), (200.0, 210.0)]
        m = self.metrics(busy, [(0.0, 45.0, 1), (200.0, 210.0, 1)], (0.0, 210.0), 30)
        self.assertAlmostEqual(m["removable_running_wall_minutes_if_10"], 20.0, places=3)
        self.assertEqual((m["kept_alive_gap_count"], m["kept_alive_wall_minutes"]), (0, 0.0))

    def test_a_session_holding_the_warehouse_hours_past_auto_stop_is_kept_alive_even_if_it_stops(self):
        # Auto-stop 30; queries 08:00-18:00; a session keeps the warehouse up until 02:00, then it stops; the next query
        # is at 08:00. The overnight gap is kept alive (7.5 hours past the auto-stop), so nothing there is removable.
        days = 5
        busy = [(hm(d, "08:00"), hm(d, "18:00")) for d in range(days)]
        running = [(hm(d, "08:00"), hm(d + 1, "02:00"), 1) for d in range(days)]
        m = self.metrics(busy, running, (hm(0, "08:00"), hm(days - 1, "18:00")), 30)
        self.assertEqual(m["kept_alive_gap_count"], days - 1)
        self.assertAlmostEqual(m["kept_alive_wall_minutes"], (days - 1) * 450.0, places=3)
        self.assertEqual(m["removable_running_wall_minutes_if_10"], 0.0)

    def test_segments_are_clipped_to_the_window(self):
        m = self.metrics([], [(-100.0, 50.0, 2), (200.0, 300.0, 1)], (0.0, 100.0), 0)
        self.assertEqual((m["observed_running_cluster_minutes"], m["running_segment_count"]), (100.0, 1))

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
        self.t = Thresholds()
        self.sql = build_running_idle_sql(self.t)
        self.targets_sql = build_running_idle_targets_sql(self.t)

    def test_every_auto_stop_step_is_evaluated_in_one_row_per_value(self):
        self.assertEqual(auto_stop_targets(self.t)[:3], [5, 10, 15])
        self.assertEqual(auto_stop_targets(self.t)[-1], 1440)
        self.assertIn("EXPLODE(SEQUENCE(5, 1440, 5))", self.targets_sql)
        for key in RUNNING_IDLE_TARGET_FIELDS:
            self.assertIn(f"AS {key}", self.targets_sql)
        self.assertIn("1.1 * m.gap_min", self.targets_sql)  # a value covers a gap only with the 10% margin
        for key in RUNNING_IDLE_HEADER_FIELDS:
            self.assertIn(key, self.sql)
        self.assertNotIn("LEFT JOIN running", self.sql)  # pieces come from an inner join: no NULL bounds in GREATEST/LEAST
        self.assertIn("CASE WHEN x.start_time >= c.w_end - INTERVAL 1 DAY THEN c.w_end ELSE x.start_time END", self.sql)  # only a recent open query runs until now

    def test_candidates_start_at_the_type_floor(self):
        self.assertEqual(auto_stop_candidates(self.t, "PRO")[0], 10)
        self.assertEqual(auto_stop_candidates(self.t, "SERVERLESS")[0], 5)

    def test_same_period_as_event_totals_and_millisecond_precision(self):
        self.assertEqual(self.sql.split("\n, gap_stats")[0], self.targets_sql.split("\nSELECT c.workspace_id")[0])  # shared CTEs
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
        cls.t = Thresholds()
        cls.fleet = synthetic_demo_evidence()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_demo_running_idle_covers_the_same_period_as_event_totals(self):
        for e in self.fleet:
            ep = e.current_epoch
            if "cluster_minutes" not in ep.events:  # no retained warehouse event: running time unknown
                self.assertNotIn("observed_running_cluster_minutes", ep.gaps, e.warehouse_name)
                continue
            self.assertAlmostEqual(ep.gaps["observed_running_cluster_minutes"], ep.events["cluster_minutes"], delta=0.01, msg=e.warehouse_name)
            for row in ep.gaps["running_idle_by_target"].values():
                self.assertLessEqual(row["removable_running_cluster_minutes"], ep.events["cluster_minutes"] + 0.01, e.warehouse_name)
            self.assertIsNotNone(ep.gaps.get("running_idle_queries"), e.warehouse_name)

    def test_collector_merges_running_idle_rows_into_the_current_epoch(self):
        class Client:
            def query(self, statement, *, label="collector_sql"):
                if label == "config_epochs":
                    return [{"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                             "warehouse_channel": "CURRENT", "warehouse_size": "SMALL", "min_clusters": 1, "max_clusters": 1,
                             "auto_stop_minutes": 0, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}]
                if label == "running_idle":
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "running_idle_queries": 7}]
                if label == "running_idle_targets":
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "target_min": 10,
                             "removable_running_wall_minutes": 42.0, "removable_running_cluster_minutes": 42.0, "additional_starts": 1,
                             "avoided_starts": 0, "added_idle_wall_minutes": 0.0}]
                return []
        e = extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-10-01T12:00:00+00:00")[0]
        self.assertEqual(e.current_epoch.gaps["running_idle_by_target"]["10"]["removable_running_wall_minutes"], 42.0)
        self.assertEqual(e.current_epoch.gaps["running_idle_queries"], 7)

    def test_core_demo_evidence_carries_running_idle_fields(self):
        ep = demo_evidence()[0].current_epoch
        self.assertEqual(ep.gaps["running_idle_by_target"]["10"]["removable_running_cluster_minutes"], 25000.0)
        self.assertNotIn("120", ep.gaps["running_idle_by_target"])  # nothing at or above the current auto-stop


class AutoStopDecisionTests(unittest.TestCase):
    """Decision paths on running-idle evidence (core demo warehouse: Pro LARGE, min 2, auto-stop 120)."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Thresholds()

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def auto_stop(self, e):
        return next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "auto_stop_minutes")

    def test_sparse_warehouse_is_priced_from_measured_idle_tails_without_sample_gates(self):
        e = demo_evidence()[0]
        e.current_epoch.query.update(queries_30=40, decision_queries_30=40)
        e.current_epoch.gaps.update(running_idle_queries=40, gap_count=12)
        for row in e.current_epoch.gaps["running_idle_by_target"].values():
            row["additional_starts"] = 0
        c = self.auto_stop(e)
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertGreater(c.estimated_savings_365, 0)

    def test_zero_queries_with_running_time_is_a_review_item_at_the_floor(self):
        e = demo_evidence()[0]
        e.current_epoch.gaps["running_idle_queries"] = 0
        c = self.auto_stop(e)
        self.assertEqual((c.status, c.recommended_config, c.blockers), ("REVIEW", {"auto_stop_minutes": 10}, ["NO_QUERIES_WHILE_RUNNING"]))

    def test_restart_cost_moves_the_choice_away_from_the_floor(self):
        # 10 removes the most idle time, but with enough added restarts a larger value nets more.
        e = demo_evidence()[0]
        rows = e.current_epoch.gaps["running_idle_by_target"]
        rows["10"]["additional_starts"] = 200  # 200 x 4 min x 2 clusters = 1,600 of 25,000 removed
        rows["5"]["additional_starts"] = 200
        base = self.auto_stop(e).recommended_config
        rows["10"]["additional_starts"] = 1000  # 8,000: 10 now nets 17,000, below 15's 23,000 - 1,360
        self.assertEqual(base, {"auto_stop_minutes": 10})
        self.assertEqual(self.auto_stop(e).recommended_config, {"auto_stop_minutes": 15})

    def test_running_after_the_auto_stop_without_queries_is_flagged_not_priced(self):
        e = demo_evidence()[0]
        e.current_epoch.gaps["kept_alive_wall_minutes"] = 3000.0  # 15% of the 20,000 running minutes
        c = self.auto_stop(e)
        self.assertEqual(c.status, "ACTIONABLE")  # the idle tails within the auto-stop are still removable
        self.assertIn("ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING", c.blockers)
        e.current_epoch.gaps["kept_alive_wall_minutes"] = 1000.0  # 5%: below the flag
        self.assertNotIn("ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING", self.auto_stop(e).blockers)

    def test_round_the_clock_polling_is_flagged_with_its_client(self):
        fleet = {x.current_epoch.tags["golden_scenario"]: x for x in synthetic_demo_evidence()}
        c = self.auto_stop(fleet["C04"])  # Tableau polls every 5 minutes, all day and night
        self.assertEqual((c.status, c.blockers), ("NO_CHANGE", ["SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING"]))
        self.assertIn("Tableau", c.metadata["rationale"])
        for scenario in ("C45", "C16", "C01", "C47"):  # real 24x7 work, auto-stop fixes it, or daytime BI
            self.assertNotIn("SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING", self.auto_stop(fleet[scenario]).blockers, scenario)

    def test_reviewer_rejects_a_pro_classic_auto_stop_below_ten(self):
        e = demo_evidence()[0]
        w = p.evaluate_warehouses([e], self.t)[0]
        bad = [dataclasses.replace(c, recommended_config={"auto_stop_minutes": 5}) if c.knob == "auto_stop_minutes" else c for c in w.candidates]
        with self.assertRaises(ValueError):
            Reviewer(self.t).review(e, bad)


if __name__ == "__main__":
    unittest.main()


class ZeroQueryBilledWithoutEventsTests(unittest.TestCase):
    """Billing alone flags a warehouse that ran every settled day with no query; a billing gap keeps it unpriced."""
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.e = next(e for e in synthetic_demo_evidence() if e.current_epoch.tags["golden_scenario"] == "C03")

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def candidates(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}

    def test_a_settled_day_without_billing_keeps_insufficient_evidence(self):
        e = copy.deepcopy(self.e)
        last = max(r["usage_date"] for r in e.daily_cost_365 if r["usage_date"] <= e.forecast_metadata["forecast_basis_end"])
        e.daily_cost_365 = [r for r in e.daily_cost_365 if r["usage_date"] != last]
        by = self.candidates(e)
        self.assertEqual((by["auto_stop_minutes"].status, by["auto_stop_minutes"].blockers), ("INSUFFICIENT_EVIDENCE", ["RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE"]))
        self.assertEqual(by["warehouse_type"].status, "TEST_REQUIRED")

    def test_queries_in_the_window_keep_insufficient_evidence(self):
        e = copy.deepcopy(self.e)
        e.current_epoch.query["queries_30"] = 12
        self.assertEqual(self.candidates(e)["auto_stop_minutes"].status, "INSUFFICIENT_EVIDENCE")

    def test_auto_stop_off_is_flagged_too(self):
        e = copy.deepcopy(self.e)
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, auto_stop_minutes=0)
        by = self.candidates(e)
        self.assertEqual((by["auto_stop_minutes"].status, by["auto_stop_minutes"].blockers), ("REVIEW", ["NO_QUERIES_WHILE_RUNNING"]))
        self.assertEqual(by["warehouse_type"].status, "NO_CHANGE")
