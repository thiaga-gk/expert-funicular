"""Evidence a configuration change cannot affect carries across that change (inside the last 30 days).

Demand and per-query evidence (query counts and mix, spill, execution latency, failures, weekly counts, recent and
busy-start concurrency) carry across auto-stop, min- and max-cluster changes; capacity queueing carries across
auto-stop changes only; nothing carries across a size or type change; cold-start wait, running time, idle time and
billing never carry. Findings read across a change are labelled CARRIED_ACROSS_CHANGE.
"""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402
from tests.test_readiness import changed  # noqa: E402


class CarryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_floor_decision_returns_soon_after_an_auto_stop_change(self):
        # C11 three days after auto-stop 30 -> 10: the floor cut needs 20 busy periods, which only the carried 30 days
        # hold (fresh evidence alone took about four weeks).
        e, by = changed("C11", "auto_stop_minutes", 10, 3)
        self.assertEqual(sorted(e.current_epoch.query["carried"]), ["capacity", "demand"])
        self.assertEqual((by["min_clusters"].status, by["min_clusters"].recommended_config["min_clusters"]), ("TEST_REQUIRED", 1))
        self.assertEqual((by["warehouse_size"].status, by["warehouse_size"].recommended_config["warehouse_size"]), ("TEST_REQUIRED", "SMALL"))

    def test_carried_findings_are_labelled(self):
        e, _ = changed("C11", "auto_stop_minutes", 10, 3)
        pkg = p.build_recommendation_packages([e], p.Thresholds())[0]
        carried = {x.metric: x for x in pkg.evidence.values() if x.epoch_scope == "CARRIED_ACROSS_CHANGE"}
        self.assertIn("busy_periods_recent", carried)
        self.assertIn("queued_query_pct_30", carried)
        self.assertIn("carried across a configuration change", carried["busy_periods_recent"].observation_window)
        fresh = {x.metric: x.epoch_scope for x in pkg.evidence.values()}
        self.assertEqual(fresh.get("time_at_min_pct"), "CURRENT")  # running-time shares never carry

    def test_nothing_carries_across_a_size_change(self):
        e, _ = changed("C06", "warehouse_size", "MEDIUM", 3)
        self.assertNotIn("carried", e.current_epoch.query)

    def test_queueing_does_not_carry_across_a_ceiling_change(self):
        e, _ = changed("C06", "max_clusters", 2, 3)
        self.assertEqual(sorted(e.current_epoch.query["carried"]), ["demand"])

    def test_view_keeps_fresh_cold_start_wait_and_epoch_histogram(self):
        query = {"queries_30": 10, "p95_wait_compute_ms_when_waited_30": 4000, "queued_query_pct_30": 9.0,
                 "concurrency_histogram": {"EPOCH": {"1": 5}, "RECENT": {"1": 5}, "BUSY_START": {"1": 1}},
                 "carried": {"demand": {"period_start": "2026-09-01T00:00:00+00:00", "queries_30": 900, "p95_wait_compute_ms_when_waited_30": 1,
                                        "queued_query_pct_30": 1.0, "concurrency_histogram": {"RECENT": {"2": 40}}}}}
        view, periods = p.carried_query_view(query)
        self.assertEqual(view["queries_30"], 900)
        self.assertEqual(view["p95_wait_compute_ms_when_waited_30"], 4000)  # cold-start wait never carries
        self.assertEqual(view["queued_query_pct_30"], 9.0)  # no capacity period: queueing stays current
        self.assertEqual(view["concurrency_histogram"], {"EPOCH": {"1": 5}, "RECENT": {"2": 40}, "BUSY_START": {}})
        self.assertEqual(periods, {"demand": "2026-09-01T00:00:00+00:00", "capacity": None})

    def test_carried_collectors_render(self):
        t = p.Thresholds()
        for family in ("demand", "capacity"):
            sql = p.build_carried_query_metrics_sql(family)
            self.assertIn(f"c.{p.CARRY_PERIOD_COLUMNS[family]} < c.cur_start", sql)
            self.assertIn("epoch_start AS period_start", sql)
        self.assertIn("demand_period_start < cur_start", p.build_carried_spill_source_sql(t))
        self.assertIn("'BUSY_START' AS scope", p.build_carried_concurrency_sql(t))

    def test_carry_survives_a_new_epoch_without_queries(self):
        def drop_current_query(rows):
            cur = rows["query_metrics_carried_demand"][0]["cur_start"]
            rows["query_metrics"] = [r for r in rows["query_metrics"] if r["epoch_start"] != cur]
            rows["concurrency"] = [r for r in rows["concurrency"] if r["epoch_start"] != cur]
        e, by = changed("C11", "auto_stop_minutes", 10, 3, rows_hook=drop_current_query)
        self.assertIn("carried", e.current_epoch.query)
        self.assertNotIn("INSUFFICIENT_BUSY_PERIOD_SAMPLE", by["min_clusters"].blockers)

    def test_source_and_mix_findings_are_labelled_carried(self):
        e, _ = changed("C11", "auto_stop_minutes", 10, 3)
        pkg = p.build_recommendation_packages([e], p.Thresholds())[0]
        scopes = {x.metric: x.epoch_scope for x in pkg.evidence.values()}
        for metric in ("dashboard_queries_30", "power_bi_queries_30"):
            if metric in scopes:
                self.assertEqual(scopes[metric], "CARRIED_ACROSS_CHANGE", metric)

    def test_a_change_older_than_thirty_days_carries_nothing(self):
        e, _ = changed("C11", "auto_stop_minutes", 10, 45)
        self.assertNotIn("carried", e.current_epoch.query)

    def test_weekly_counts_never_reach_across_the_change(self):
        query = {"queries_week_5": None, "carried": {"demand": {"period_start": "2026-08-01T00:00:00+00:00", "queries_week_5": 900}}}
        view, _ = p.carried_query_view(query)
        self.assertIsNone(view["queries_week_5"])

    def test_a_channel_change_breaks_the_carry(self):
        sql = p.build_carried_query_metrics_sql("demand")
        self.assertIn("LAG(warehouse_channel) OVER w <=> warehouse_channel", sql)
        self.assertIn("c.cur_start > CURRENT_TIMESTAMP() - INTERVAL 30 DAYS", sql)

    def test_carried_concurrency_clips_at_the_period_end(self):
        sql = p.build_carried_concurrency_sql(p.Thresholds())
        self.assertIn("CASE WHEN x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 1 DAY THEN CURRENT_TIMESTAMP() ELSE x.start_time END), CURRENT_TIMESTAMP()) AS t", sql)

    def test_recent_window_covering_the_epoch_is_not_a_seasonal_peak(self):
        e, by = changed("C11", "auto_stop_minutes", 10, 3)
        f = p.Analyzer(p.Thresholds()).analyze(e)["max_clusters"].facts
        self.assertFalse(f.get("epoch_peak_outside_recent"))

    def test_assessment_query_sample_reads_carried_evidence(self):
        e, _ = changed("C11", "auto_stop_minutes", 10, 3)
        pkg = p.build_recommendation_packages([e], p.Thresholds())[0]
        self.assertTrue(pkg.assessment.evidence_quality.query_sample_sufficient)

    def test_older_epoch_inside_the_carried_window_does_not_hold(self):
        # An older epoch that lies wholly inside the carried 30 days is part of the recent window, not "older".
        import copy
        e, _ = changed("C11", "auto_stop_minutes", 10, 3)
        cur = e.current_epoch
        recent = cur.query["carried"]["demand"]["concurrency_histogram"]["RECENT"]
        top = max(int(k) for k in recent)
        start = p.parse_datetime(e.analysis_time) - p.dt.timedelta(days=10)
        inside = copy.deepcopy(next(x for x in e.epochs if not x.is_current))
        inside.epoch_id, inside.epoch_start, inside.epoch_end = "inside", start.isoformat(), cur.epoch_start
        inside.query = {"concurrency_histogram": {"EPOCH": {str(top): 400}}}
        e.epochs.append(inside)
        f = p.Analyzer(p.Thresholds()).analyze(e)["min_clusters"].facts
        self.assertFalse(f["older_epoch_demand_higher"])

    def test_workload_profile_and_attribution_read_the_carried_view(self):
        def drop_current_query(rows):
            cur = rows["query_metrics_carried_demand"][0]["cur_start"]
            rows["query_metrics"] = [r for r in rows["query_metrics"] if r["epoch_start"] != cur]
        e, _ = changed("C11", "auto_stop_minutes", 10, 3, rows_hook=drop_current_query)
        pkg = p.build_recommendation_packages([e], p.Thresholds())[0]
        self.assertGreater(pkg.workload_profile.activity.queries_30d, 1000)
        self.assertEqual(getattr(pkg.workload_profile.primary_query_source, "value", pkg.workload_profile.primary_query_source), "DASHBOARD")


if __name__ == "__main__":
    unittest.main()
