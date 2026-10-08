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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.queries.events import RUNNING_IDLE_TARGET_FIELDS, RUNNING_IDLE_WINDOW_FIELDS, build_running_idle_sql, build_running_idle_targets_sql, running_idle_windows_from_statement  # noqa: E402
from publisher.analyzer.findings import Analyzer  # noqa: E402
from publisher.analyzer.metrics import auto_stop_candidates, auto_stop_targets, running_idle_basis, running_idle_blend, running_idle_metrics, running_idle_rates  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.reviewer.reviewer import Reviewer  # noqa: E402

DAY = 1440.0


def hm(day, hhmm):
    h, m = hhmm.split(":")
    return day * DAY + int(h) * 60 + int(m)


class RunningIdleOracleTests(unittest.TestCase):
    def metrics(self, busy, segments, window, auto_stop, targets=(10, 30), windows=None, label="w", floor_clusters=1):
        """One window (the whole evidence period unless ``windows`` are given), flattened for readable assertions."""
        m = running_idle_metrics(busy, segments, window, windows or {"w": window}, auto_stop, targets, floor_clusters)[label]
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
        self.assertAlmostEqual(m["added_idle_cluster_minutes_if_16"], 4 * 4.0, places=3)
        self.assertEqual(m["removable_running_wall_minutes_if_16"], 0.0)

    def test_raising_prices_added_idle_at_the_cluster_count_it_stopped_at(self):
        # Floor 1, but the warehouse was still at 3 clusters when it stopped 10 minutes after each query: staying up
        # longer keeps those 3 clusters, so the added idle costs 3 cluster-minutes per minute, not the floor's 1.
        busy = [(k * 15.0, k * 15.0 + 1.0) for k in range(5)]
        running = [(k * 15.0, k * 15.0 + 11.0, 3) for k in range(5)]
        m = self.metrics(busy, running, (0.0, 61.0), 10, targets=(16,))
        self.assertAlmostEqual(m["added_idle_wall_minutes_if_16"], 16.0, places=3)
        self.assertAlmostEqual(m["added_idle_cluster_minutes_if_16"], 48.0, places=3)

    def test_a_window_counts_its_own_time_and_the_restarts_whose_query_starts_in_it(self):
        # Warehouse never stops; queries at minute 0, 100 and 200: gaps [10, 100) and [110, 200). Window [50, 150) holds
        # [50, 100) of the first gap and [110, 150) of the second, and only the first gap's restart (its query at 100).
        busy = [(0.0, 10.0), (100.0, 110.0), (200.0, 210.0)]
        running = [(0.0, 210.0, 1)]
        windows = {"a": (0.0, 50.0), "b": (50.0, 150.0), "c": (150.0, 210.0)}
        parts = {k: self.metrics(busy, running, (0.0, 210.0), 0, targets=(30,), windows=windows, label=k) for k in windows}
        self.assertAlmostEqual(parts["b"]["removable_running_wall_minutes_if_30"], (100.0 - 50.0) + (150.0 - 140.0), places=3)
        self.assertEqual(parts["b"]["additional_starts_if_30"], 1)
        self.assertAlmostEqual(parts["b"]["observed_running_cluster_minutes"], 100.0, places=3)
        # Adjacent windows add up to the whole period.
        whole = self.metrics(busy, running, (0.0, 210.0), 0, targets=(30,))
        for key in ("removable_running_wall_minutes_if_30", "additional_starts_if_30", "observed_running_cluster_minutes"):
            self.assertAlmostEqual(sum(x[key] for x in parts.values()), whole[key], places=3, msg=key)

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
    WINDOWS = [{"workspace_id": "1", "warehouse_id": "w", "label": "long", "start": "2026-07-08", "end": "2026-09-29"},
               {"workspace_id": "1", "warehouse_id": "w", "label": "recent", "start": "2026-09-02", "end": "2026-09-29"}]

    def setUp(self):
        self.t = Thresholds()
        self.sql = build_running_idle_sql(self.t, self.WINDOWS)
        self.targets_sql = build_running_idle_targets_sql(self.t, self.WINDOWS)

    def test_every_auto_stop_step_is_evaluated_in_one_row_per_window_and_value(self):
        self.assertEqual(auto_stop_targets(self.t)[:3], [5, 10, 15])
        self.assertEqual(auto_stop_targets(self.t)[-1], 1440)
        self.assertIn("EXPLODE(SEQUENCE(5, 1440, 5))", self.targets_sql)
        for key in RUNNING_IDLE_TARGET_FIELDS:
            self.assertIn(f"AS {key}", self.targets_sql)
        self.assertIn("1.1 * m.gap_min", self.targets_sql)  # a value covers a gap only with the 10% margin
        self.assertIn("m.label AS window_label", self.targets_sql)
        for key in RUNNING_IDLE_WINDOW_FIELDS:
            self.assertIn(key, self.sql)
        self.assertNotIn("LEFT JOIN running", self.sql)  # pieces come from an inner join: no NULL bounds in GREATEST/LEAST
        self.assertIn("CASE WHEN x.start_time >= c.w_end - INTERVAL 1 DAY THEN c.w_end ELSE x.start_time END", self.sql)  # only a recent open query runs until now
        self.assertIn("MAX_BY(p.cluster_count, p.p0)", self.sql)  # a raise keeps the cluster count it stopped at

    def test_windows_are_utc_day_bounds_and_round_trip_through_the_marker(self):
        self.assertIn("('1', 'w', 'long', TIMESTAMP'2026-07-08T00:00:00Z', TIMESTAMP'2026-09-30T00:00:00Z')", self.sql)
        self.assertEqual(running_idle_windows_from_statement(self.sql), self.WINDOWS)
        epoch = build_running_idle_sql(self.t, [{"workspace_id": "1", "warehouse_id": "w", "label": "epoch", "start": None, "end": None}])
        self.assertIn("('1', 'w', 'epoch', CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP))", epoch)

    def test_a_collected_value_cannot_change_the_sql(self):
        for bad in ({"warehouse_id": "w'; DROP TABLE x; --"}, {"label": "everything"}, {"start": "2026-07-08' OR '1'='1"}):
            with self.assertRaises((PublisherError, ValueError)):
                build_running_idle_sql(self.t, [dict(self.WINDOWS[0], **bad)])

    def test_candidates_start_at_the_type_floor(self):
        self.assertEqual(auto_stop_candidates(self.t, "PRO")[0], 10)
        self.assertEqual(auto_stop_candidates(self.t, "SERVERLESS")[0], 5)

    def test_same_period_as_event_totals_and_millisecond_precision(self):
        self.assertEqual(self.sql.split("\n, seg_stats")[0], self.targets_sql.split("\nSELECT * FROM (")[0])  # shared CTEs
        self.assertIn("INTERVAL 365 DAYS", self.sql)
        self.assertIn("first_event_time", self.sql)
        self.assertIn("TIMESTAMPDIFF(MILLISECOND", self.sql)
        self.assertNotIn("TIMESTAMPDIFF(SECOND", self.sql)
        self.assertIn("ORDER BY event_time, event_type, cluster_count", self.sql)  # deterministic tie-break
        self.assertIn("epoch_end IS NULL", self.sql)  # current epoch only


class RunningIdleBasisTests(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_run_rate_windows_are_the_forecasts_whole_weeks(self):
        meta = {"forecast_model": "RUN_RATE", "forecast_basis_end": "2026-09-29"}
        long_epoch = running_idle_basis("2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00", meta, self.t)
        self.assertEqual(long_epoch, {"model": "RUN_RATE", "windows": {"long": ["2026-07-08", "2026-09-29"], "recent": ["2026-09-02", "2026-09-29"]}})
        # 40 days since the change at 10:00 (first whole day the 21st): 39 whole days, trimmed to 35 (five weeks).
        young = running_idle_basis("2026-08-20T10:00:00+00:00", "2025-01-01T00:00:00+00:00", meta, self.t)
        self.assertEqual(young["windows"], {"long": ["2026-08-26", "2026-09-29"], "recent": ["2026-09-02", "2026-09-29"]})

    def test_cadence_windows_are_whole_cycles_and_no_basis_falls_back_to_the_epoch(self):
        meta = {"forecast_model": "CADENCE", "forecast_basis_end": "2026-09-29", "cadence_run_starts": ["2026-06-28", "2026-07-28", "2026-08-28", "2026-09-28"]}
        self.assertEqual(running_idle_basis("2025-01-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00", meta, self.t),
                         {"model": "CADENCE", "windows": {"cycles": ["2026-07-28", "2026-09-27"], "last_cycle": ["2026-08-28", "2026-09-27"]}})
        events_too_late = running_idle_basis("2025-01-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", meta, self.t)
        self.assertEqual(events_too_late, {"model": "EPOCH", "windows": {"epoch": None}})
        self.assertEqual(running_idle_basis("2025-01-01T00:00:00+00:00", None, {}, self.t)["model"], "EPOCH")

    def test_blend_matches_the_forecast_weights(self):
        w = lambda a, b: {"window_start": a + "T00:00:00+00:00", "window_end": b + "T00:00:00+00:00"}
        weights, check = running_idle_blend("RUN_RATE", {"long": w("2026-07-08", "2026-09-30"), "recent": w("2026-09-02", "2026-09-30")}, self.t)
        self.assertEqual(check, "recent")
        self.assertAlmostEqual(dict(weights)["recent"], 0.65 / 28, places=9)
        self.assertAlmostEqual(dict(weights)["long"], 0.35 / 84, places=9)
        weights, _ = running_idle_blend("RUN_RATE", {"long": w("2026-09-16", "2026-09-30"), "recent": w("2026-09-16", "2026-09-30")}, self.t)
        self.assertEqual(weights, [("long", 1.0 / 14)])


class RunningIdleEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.fleet = synthetic_demo_evidence()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_demo_running_idle_is_measured_on_the_forecast_days(self):
        for e in self.fleet:
            ep = e.current_epoch
            if "cluster_minutes" not in ep.events:  # no retained warehouse event: running time unknown
                self.assertNotIn("running_idle_windows", ep.gaps, e.warehouse_name)
                continue
            self.assertIsNotNone(running_idle_rates(e, self.t), e.warehouse_name)  # the windows the forecast basis chooses
            self.assertLessEqual(sum(w["observed_running_cluster_minutes"] for w in ep.gaps["running_idle_windows"].values() if w is not None),
                                 2 * ep.events["cluster_minutes"] + 0.01, e.warehouse_name)
            for window in ep.gaps["running_idle_windows"].values():
                for row in window["running_idle_by_target"].values():
                    self.assertLessEqual(row["removable_running_cluster_minutes"], window["observed_running_cluster_minutes"] + 0.01, e.warehouse_name)

    def test_windows_collected_for_other_days_are_not_collected(self):
        e = copy.deepcopy(next(x for x in self.fleet if x.current_epoch.gaps.get("running_idle_model") == "RUN_RATE"))
        e.forecast_metadata["forecast_basis_end"] = "2026-09-20"  # the forecast now chooses other days
        self.assertIsNone(running_idle_rates(e, self.t))
        c = next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "auto_stop_minutes")
        self.assertEqual((c.status, c.blockers[:1]), ("INSUFFICIENT_EVIDENCE", ["RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE"]))

    def test_collector_asks_for_the_forecast_windows_and_merges_their_rows(self):
        asked = []

        class Client:
            def query(self, statement, *, label="collector_sql"):
                if label == "config_epochs":
                    return [{"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                             "warehouse_channel": "CURRENT", "warehouse_size": "SMALL", "min_clusters": 1, "max_clusters": 1,
                             "auto_stop_minutes": 0, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}]
                if label == "warehouse_events":
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "event_coverage_start": "2026-09-01T00:00:00+00:00",
                             "uptime_minutes": 600.0, "cluster_minutes": 600.0}]
                if label == "billing_daily_cost":
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "usage_date": f"2026-09-{d:02d}",
                             "currency_code": "USD", "list_cost": 10.0, "dbu_quantity": 18.0, "unpriced_dbu_quantity": 0.0} for d in range(1, 30)]
                windows = running_idle_windows_from_statement(statement)
                if label == "running_idle":
                    asked.extend(windows)
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "window_label": w["label"],
                             "window_start": f"{w['start']}T00:00:00+00:00", "window_end": f"{w['end']}T23:59:59+00:00",
                             **dict.fromkeys(RUNNING_IDLE_WINDOW_FIELDS, 7)} for w in windows]
                if label == "running_idle_targets":
                    return [{"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "window_label": w["label"], "target_min": 10,
                             "removable_running_wall_minutes": 42.0, "removable_running_cluster_minutes": 42.0, "additional_starts": 1,
                             "avoided_starts": 0, "added_idle_wall_minutes": 0.0, "added_idle_cluster_minutes": 0.0} for w in windows]
                return []
        e = extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-10-01T12:00:00+00:00")[0]
        self.assertEqual(sorted(w["label"] for w in asked), ["long", "recent"])
        gaps = e.current_epoch.gaps
        self.assertEqual(gaps["running_idle_model"], "RUN_RATE")
        self.assertEqual(gaps["running_idle_windows"]["long"]["running_idle_by_target"]["10"]["removable_running_wall_minutes"], 42.0)
        self.assertEqual(gaps["running_idle_windows"]["recent"]["queries"], 7)

    def test_core_demo_evidence_carries_running_idle_windows(self):
        ep = demo_evidence()[0].current_epoch
        long = ep.gaps["running_idle_windows"]["long"]
        self.assertGreater(long["running_idle_by_target"]["10"]["removable_running_cluster_minutes"], 0)
        self.assertNotIn("120", long["running_idle_by_target"])  # nothing at or above the current auto-stop


class AutoStopDecisionTests(unittest.TestCase):
    """Decision paths on running-idle evidence (core demo warehouse: Pro LARGE, min 2, auto-stop 120)."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Thresholds()

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def auto_stop(self, e):
        return next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "auto_stop_minutes")

    @staticmethod
    def windows(e):
        return e.current_epoch.gaps["running_idle_windows"].values()

    def test_sparse_warehouse_is_priced_from_measured_idle_tails_without_sample_gates(self):
        e = demo_evidence()[0]
        e.current_epoch.query.update(queries_30=40, decision_queries_30=40)
        for window in self.windows(e):
            window["queries"] = 40
            for row in window["running_idle_by_target"].values():
                row["additional_starts"] = 0
        c = self.auto_stop(e)
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertGreater(c.estimated_savings_365, 0)

    def test_zero_queries_with_running_time_is_a_review_item_at_the_floor(self):
        e = demo_evidence()[0]
        for window in self.windows(e):
            window["queries"] = 0
        c = self.auto_stop(e)
        self.assertEqual((c.status, c.recommended_config, c.blockers), ("REVIEW", {"auto_stop_minutes": 10}, ["NO_QUERIES_WHILE_RUNNING"]))

    def test_restart_cost_moves_the_choice_away_from_the_floor(self):
        # 10 removes the most idle time, but with enough added restarts a larger value nets more (per window, in the
        # same proportion to the idle 10 removes: 200 and 1,000 restarts per 25,000 cluster-minutes).
        e = demo_evidence()[0]

        def restarts(per_25000):
            for window in self.windows(e):
                rows = window["running_idle_by_target"]
                for key in ("5", "10"):
                    rows[key]["additional_starts"] = int(per_25000 * rows["10"]["removable_running_cluster_minutes"] / 25000.0)
        restarts(200)
        self.assertEqual(self.auto_stop(e).recommended_config, {"auto_stop_minutes": 10})
        restarts(1000)
        self.assertEqual(self.auto_stop(e).recommended_config, {"auto_stop_minutes": 15})

    def test_running_after_the_auto_stop_without_queries_is_flagged_not_priced(self):
        e = demo_evidence()[0]
        for window in self.windows(e):
            window["kept_alive_wall_minutes"] = 0.15 * window["observed_running_wall_minutes"]
        c = self.auto_stop(e)
        self.assertEqual(c.status, "ACTIONABLE")  # the idle tails within the auto-stop are still removable
        self.assertIn("ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING", c.blockers)
        for window in self.windows(e):
            window["kept_alive_wall_minutes"] = 0.05 * window["observed_running_wall_minutes"]  # below the flag
        self.assertNotIn("ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING", self.auto_stop(e).blockers)

    def test_a_value_over_the_budget_in_the_recent_window_alone_is_rejected(self):
        # Within the 2% budget on the blend, over it in the last 28 days: a recent change in demand holds the value.
        e = demo_evidence()[0]
        for label, window in e.current_epoch.gaps["running_idle_windows"].items():
            window["queries"] = 10000
            for key, row in window["running_idle_by_target"].items():
                row["additional_starts"] = (0 if label == "long" else 300) if int(key) <= 15 else 0
        c = self.auto_stop(e)
        self.assertNotIn(c.recommended_config["auto_stop_minutes"], (5, 10, 15))

    def test_round_the_clock_polling_is_flagged_with_its_client(self):
        fleet = {x.current_epoch.tags["golden_scenario"]: x for x in synthetic_demo_evidence()}
        c = self.auto_stop(fleet["C04"])  # Tableau polls every 5 minutes, all day and night
        self.assertEqual((c.status, c.blockers), ("NO_CHANGE", ["SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING"]))
        self.assertIn("Tableau", c.metadata["rationale"])
        for scenario in ("C45", "C16", "C01", "C47"):  # real 24x7 work, auto-stop fixes it, or daytime BI
            self.assertNotIn("SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING", self.auto_stop(fleet[scenario]).blockers, scenario)

    def test_reviewer_rejects_an_auto_stop_off_the_types_grid(self):
        e = demo_evidence()[0]
        w = p.evaluate_warehouses([e], self.t)[0]
        for value in (5, 0, 1445):  # below the Pro floor, never stop, above the maximum
            bad = [dataclasses.replace(c, recommended_config={"auto_stop_minutes": value}) if c.knob == "auto_stop_minutes" else c for c in w.candidates]
            with self.assertRaises(ValueError, msg=value):
                Reviewer(self.t).review(e, bad)

    def test_annual_reclaimable_hours_and_cost_per_hour_are_evidence(self):
        e = demo_evidence()[0]
        a = Analyzer(self.t).analyze(e)["auto_stop_minutes"]
        by_metric = {f.metric: f.value for f in a.findings}
        hours = by_metric["auto_stop_annual_cluster_hours_by_value"]["10"]
        o = next(o for o in a.facts["options"] if o["target"] == 10)
        trend = a.facts["trend_adjustment_factor"]
        self.assertAlmostEqual(hours["net_cluster_hours"], round(o["net_cluster_minutes_per_day"] * 365 * trend / 60, 1), places=6)
        running_hours = a.facts["running_cluster_minutes_per_day"] * 365 * trend / 60
        self.assertAlmostEqual(by_metric["cost_per_cluster_hour"], round(e.baseline_forecast_cost_365 / running_hours, 4), places=6)
        c = self.auto_stop(e)
        # Unrealized (optimistic) savings = annual net hours x cost per hour.
        self.assertAlmostEqual(c.dbu_365d_savings_range.high, hours["net_cluster_hours"] * by_metric["cost_per_cluster_hour"], delta=0.01 * c.dbu_365d_savings_range.high)


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
