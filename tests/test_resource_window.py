"""Resource time (cluster-minutes, time at min/max) is read on the same recent window as the DBU forecast.

Whole-epoch totals describe a different year than the recent-weighted DBU forecast when the workload changed during
the epoch; AWS cost and capacity facts would then disagree with the DBU side.
"""
import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


def day_rows(start, days, cluster_minutes, *, at_min=0.0, at_max=0.0, recycle=0.0, peak=1, uptime=None):
    d0 = dt.date.fromisoformat(start)
    return [{
        "usage_day": (d0 + dt.timedelta(days=i)).isoformat(),
        "uptime_minutes": cluster_minutes if uptime is None else uptime,
        "cluster_minutes": cluster_minutes,
        "minutes_at_min": at_min, "minutes_at_max": at_max, "recycle_minutes": recycle,
        "max_cluster_count": peak, "starts": 1,
    } for i in range(days)]


class ResourceWindowTests(unittest.TestCase):
    def setUp(self):
        self.t = p.Thresholds()
        self.e = copy.deepcopy(p.demo_evidence()[0])  # PRO LARGE, epoch 2026-01-15, analysis 2026-10-01
        self.e.forecast_metadata = dict(self.e.forecast_metadata, forecast_basis_end="2026-09-29", trend_adjustment_factor=1.0)

    def regime_shift(self):
        # 300-day history: 600 cluster-minutes/day, then 150/day for the last 84 days (a team moved off).
        return day_rows("2025-12-04", 216, 600.0) + day_rows("2026-07-08", 84, 150.0)

    def test_window_is_the_last_84_basis_days_zero_filled(self):
        self.e.current_epoch.resource_daily = day_rows("2026-09-01", 29, 100.0)[::2]  # every other day only
        w = p.resource_window(self.e, self.t)
        self.assertEqual((w["start"], w["end"], w["days"]), ("2026-07-08", "2026-09-29", 84))
        self.assertEqual(len(w["cluster_minutes"]), 84)
        self.assertEqual(sum(1 for x in w["cluster_minutes"] if x == 0), 84 - 15)

    def test_window_is_clipped_to_the_epoch_start(self):
        self.e.current_epoch.epoch_start = "2026-09-10T00:00:00+00:00"
        self.e.current_epoch.resource_daily = day_rows("2026-09-10", 20, 100.0)
        w = p.resource_window(self.e, self.t)
        self.assertEqual((w["start"], w["days"]), ("2026-09-10", 20))

    def test_aws_baseline_follows_the_recent_regime_not_the_epoch_average(self):
        self.e.current_epoch.resource_daily = self.regime_shift()
        registry = json.loads((ROOT / "pricing" / "aws_ec2_on_demand.json").read_text())
        cost, meta = p.model_aws_baseline_cost(self.e.current_epoch, "us-east-1", self.e.analysis_time, self.e.forecast_metadata, registry, self.t)
        self.assertAlmostEqual(meta["annualized_cluster_minutes"], 150.0 * 365, places=3)  # not (216*600+84*150)/300*365 = 173,010
        self.assertEqual(meta["resource_window_days"], 84)
        self.assertAlmostEqual(cost, round(150.0 * 365 / 60.0 * meta["cluster_hourly_price"], 2), places=2)

    def test_old_evidence_without_the_daily_series_fails_closed(self):
        self.e.current_epoch.resource_daily = None
        registry = json.loads((ROOT / "pricing" / "aws_ec2_on_demand.json").read_text())
        cost, meta = p.model_aws_baseline_cost(self.e.current_epoch, "us-east-1", self.e.analysis_time, self.e.forecast_metadata, registry, self.t)
        self.assertIsNone(cost)
        self.assertEqual(meta["blocker"], "RECENT_RESOURCE_WINDOW_UNAVAILABLE")
        by = {c.knob: c for c in p.evaluate_warehouses([self.e], self.t)[0].candidates}
        for knob in ("max_clusters", "min_clusters"):
            self.assertEqual(by[knob].recommended_config, by[knob].current_config, knob)
            self.assertIn("RECENT_RESOURCE_WINDOW_UNAVAILABLE", by[knob].blockers, knob)

    def test_capacity_facts_use_the_last_30_days(self):
        # 54 days pinned at the floor, then 30 days never at the floor: the min-cluster rule must see the recent 0%.
        self.e.current_epoch.resource_daily = (day_rows("2026-07-08", 54, 200.0, at_min=100.0, uptime=100.0)
                                              + day_rows("2026-08-31", 30, 300.0, at_min=0.0, uptime=100.0, peak=3))
        analyses = p.Analyzer(self.t).analyze(self.e)
        self.assertEqual(analyses["min_clusters"].facts["at_min_pct"], 0.0)
        self.assertEqual(analyses["max_clusters"].facts["max_observed"], 3)

    def test_recycle_transients_are_not_peak_demand(self):
        self.e.current_epoch.resource_daily = day_rows("2026-07-08", 84, 200.0, peak=2, recycle=5.0, uptime=100.0)
        analyses = p.Analyzer(self.t).analyze(self.e)
        self.assertEqual(analyses["max_clusters"].facts["max_observed"], 2)

    def test_migration_resource_bounds_use_the_window(self):
        self.e.current_epoch.resource_daily = self.regime_shift()
        bounds, meta = p.Estimator(self.t)._annualized_resource_bounds(self.e)
        self.assertAlmostEqual(bounds.base, 150.0 * 365, places=3)
        self.assertEqual(meta["resource_window_days"], 84)

    def test_null_and_empty_series_survive_replay(self):
        for value in (None, []):
            with self.subTest(value=value):
                e = copy.deepcopy(self.e)
                e.current_epoch.resource_daily = value
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "e.json"
                    p.save_evidence(path, [e])
                    back = p.load_evidence_files([str(path)])[0]
                self.assertEqual(back.current_epoch.resource_daily, value)

    def test_daily_resource_sql_splits_at_midnight_and_excludes_recycle_from_peak(self):
        sql = p.RESOURCE_DAILY_SQL
        self.assertIn(p.CONFIG_EPOCHS_CTE, sql)
        self.assertIn("explode(sequence(", sql)
        self.assertIn("recycle_minutes", sql)
        self.assertIn("MAX(CASE WHEN cluster_count <= max_clusters THEN cluster_count END) AS max_cluster_count", sql)
        self.assertIn("ORDER BY event_time, event_type, cluster_count", sql)

    def test_collector_attaches_the_daily_series_to_the_current_epoch(self):
        epoch = {"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                 "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 1, "max_clusters": 2,
                 "auto_stop_minutes": 10, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}
        rows = [dict(r, workspace_id="1", warehouse_id="w", epoch_start="2026-09-01T00:00:00+00:00") for r in day_rows("2026-09-01", 5, 60.0)]

        class Client:
            def query(self, statement, *, label="collector_sql"):
                return [epoch] if label == "config_epochs" else rows if label == "resource_daily" else []

        e = p.extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-10-01T12:00:00+00:00")[0]
        self.assertEqual([r["usage_day"] for r in e.current_epoch.resource_daily], [r["usage_day"] for r in rows])
        self.assertNotIn("workspace_id", e.current_epoch.resource_daily[0])


    # --- review round 1 ----------------------------------------------------------------------------------------
    def test_days_before_the_collected_range_are_unknown_not_zero(self):
        # The query collected 2026-07-10 onwards; a lagging basis end of 09-25 must not invent zeros for 07-04..07-09.
        self.e.current_epoch.events["resource_daily_from"] = "2026-07-10"
        self.e.current_epoch.resource_daily = day_rows("2026-07-10", 80, 100.0)
        self.e.forecast_metadata["forecast_basis_end"] = "2026-09-25"
        w = p.resource_window(self.e, self.t)
        self.assertEqual(w["start"], "2026-07-10")
        self.assertNotIn(0.0, w["cluster_minutes"])
        self.assertAlmostEqual(p.blended_daily_rate(w["cluster_minutes"], self.t) * 365, 36500.0, places=3)

    def test_collector_widens_collection_and_records_its_start(self):
        self.assertIn("DATE_SUB(CURRENT_DATE(), 120)", p.RESOURCE_DAILY_SQL)
        self.assertIn("s.seg_end > s.seg_start", p.RESOURCE_DAILY_SQL)  # zero-length segments add nothing

    def test_max_increase_is_priced_on_the_window_it_is_decided_on(self):
        # Whole-epoch minutes at max are 0, but 40% of recent uptime is at the ceiling with heavy queueing.
        e = self.e
        e.current_epoch.events["minutes_at_max"] = 0.0
        e.current_epoch.resource_daily = day_rows("2026-07-08", 84, 300.0, at_max=40.0, peak=4, uptime=100.0)
        e.current_epoch.query.update(queued_query_pct_30=20.0, queued_queries_30=500, p95_wait_capacity_ms_when_queued_30=90000)
        mx = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]
        self.assertEqual(mx.recommended_config["max_clusters"], mx.current_config["max_clusters"] + 1)
        self.assertGreater(mx.metadata["raw_uplift_fraction"], 0.0)

    def test_missing_series_holds_max_clusters_even_when_min_equals_max(self):
        import dataclasses
        e = self.e
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, min_clusters=2, max_clusters=2)
        e.current_epoch.resource_daily = None
        e.current_epoch.query.update(queued_query_pct_30=10.0, queued_queries_30=600, p95_wait_capacity_ms_when_queued_30=60000)
        mx = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]
        self.assertIn("RECENT_RESOURCE_WINDOW_UNAVAILABLE", mx.blockers)

    def test_collected_but_idle_window_is_not_reported_as_missing(self):
        self.e.current_epoch.resource_daily = day_rows("2026-07-08", 54, 200.0, at_min=100.0, uptime=100.0, peak=2) + day_rows("2026-08-31", 30, 0.0, peak=0, uptime=0.0)
        by = {c.knob: c for c in p.evaluate_warehouses([self.e], self.t)[0].candidates}
        for knob in ("max_clusters", "min_clusters"):
            self.assertNotIn("RECENT_RESOURCE_WINDOW_UNAVAILABLE", by[knob].blockers, knob)
            self.assertIn("NO_RECENT_RUNNING_TIME", by[knob].blockers, knob)

    def test_collected_range_comes_from_the_query_not_the_analysis_clock(self):
        self.assertIn("DATE_SUB(CURRENT_DATE(), 120) AS collected_from", p.RESOURCE_DAILY_SQL)
        epoch = {"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                 "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 1, "max_clusters": 2,
                 "auto_stop_minutes": 10, "epoch_start": "2026-05-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}
        rows = [dict(r, workspace_id="1", warehouse_id="w", epoch_start="2026-05-01T00:00:00+00:00", collected_from="2026-06-14")
                for r in day_rows("2026-06-14", 5, 60.0)]

        class Client:
            def query(self, statement, *, label="collector_sql"):
                return [epoch] if label == "config_epochs" else rows if label == "resource_daily" else []

        # A fixed analysis time earlier than the run must not widen the collected range.
        e = p.extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-09-01T12:00:00+00:00")[0]
        self.assertEqual(e.current_epoch.events["resource_daily_from"], "2026-06-14")
        self.assertNotIn("collected_from", e.current_epoch.resource_daily[0])

if __name__ == "__main__":
    unittest.main()
