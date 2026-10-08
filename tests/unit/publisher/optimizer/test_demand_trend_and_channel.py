import dataclasses
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.analyzer.collector.queries.events import EPOCH_EVENT_METRICS_SQL  # noqa: E402
from publisher.analyzer.collector.queries.query_history import EPOCH_QUERY_METRICS_SQL  # noqa: E402
from publisher.analyzer.metrics import query_volume_growth_ratio  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402


class SeasonalCeilingTests(unittest.TestCase):
    """A ceiling cut never goes below the peak demand of the whole current epoch (Pro/Classic: concurrency)."""

    def setUp(self):
        self.t = Thresholds()

    def max_candidate(self, epoch_peak_clusters):
        e = demo_evidence()[0]  # recent P99.5 = 20 queries (2 clusters) -> ceiling 3; widen the ceiling to 8
        for ep in e.epochs:
            ep.config = dataclasses.replace(ep.config, max_clusters=8)
        hist = e.current_epoch.query["concurrency_histogram"]
        hist["EPOCH"] = {} if epoch_peak_clusters is None else {"1": 9900.0, str(10 * epoch_peak_clusters): 100.0}
        e.current_epoch.events["max_observed_clusters_within_ceiling"] = epoch_peak_clusters
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]

    def test_epoch_peak_inside_the_recent_window_keeps_the_recent_cut(self):
        c = self.max_candidate(2)
        self.assertEqual((c.status, c.recommended_config["max_clusters"]), ("ACTIONABLE", 3))

    def test_epoch_peak_outside_the_recent_window_raises_the_cut_target(self):
        c = self.max_candidate(4)  # 40 queries at the epoch's P99.5: 4 clusters + 1 headroom
        self.assertEqual((c.status, c.recommended_config["max_clusters"]), ("ACTIONABLE", 5))

    def test_epoch_peak_at_the_ceiling_holds_the_cut(self):
        c = self.max_candidate(8)
        self.assertEqual((c.status, c.recommended_config["max_clusters"]), ("NO_CHANGE", 8))
        self.assertEqual(c.blockers, ["PEAK_OUTSIDE_RECENT_WINDOW"])

    def test_recycling_transients_above_the_ceiling_are_not_a_peak(self):
        # The epoch peak is measured within the ceiling, like the recent window's: counts above max are not demand.
        sql = EPOCH_EVENT_METRICS_SQL.replace(" ", "")
        self.assertIn("MAX(CASEWHENcluster_count<=max_clustersTHENcluster_countEND)ASmax_observed_clusters_within_ceiling", sql)
        e = demo_evidence()[0]
        for ep in e.epochs:
            ep.config = dataclasses.replace(ep.config, max_clusters=8)
        e.current_epoch.events["max_observed_clusters"] = 12
        e.current_epoch.events.pop("max_observed_clusters_within_ceiling", None)
        c = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]
        self.assertEqual((c.status, c.recommended_config["max_clusters"]), ("ACTIONABLE", 3))

    def test_held_cut_still_names_queue_evidence_gaps(self):
        e = demo_evidence()[0]
        for ep in e.epochs:
            ep.config = dataclasses.replace(ep.config, max_clusters=8)
        e.current_epoch.events["max_observed_clusters_within_ceiling"] = 8
        e.current_epoch.query["concurrency_histogram"]["EPOCH"] = {"1": 9900.0, "80": 100.0}
        e.current_epoch.query.pop("queued_query_pct_30", None)
        c = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertEqual(c.blockers, ["QUEUE_EVIDENCE_UNAVAILABLE", "PEAK_OUTSIDE_RECENT_WINDOW"])

    def test_unknown_epoch_peak_uses_the_recent_window(self):
        c = self.max_candidate(None)
        self.assertEqual((c.status, c.recommended_config["max_clusters"]), ("ACTIONABLE", 3))


class GrowthAndSeasonalHoldTests(unittest.TestCase):
    """Capacity cuts are held while query volume grows week over week, and a downsize is held for a seasonal peak."""

    def setUp(self):
        self.t = Thresholds()

    def evidence(self, weeks, max_clusters=None, epoch_peak=None):
        e = demo_evidence()[0]
        if max_clusters is not None:
            for ep in e.epochs:
                ep.config = dataclasses.replace(ep.config, max_clusters=max_clusters)
        for k in range(1, 9):
            e.current_epoch.query.pop(f"queries_week_{k}", None)
        if weeks is not None:
            for k, n in enumerate(weeks, start=1):  # week 1 = the most recent complete week
                e.current_epoch.query[f"queries_week_{k}"] = n
        if epoch_peak is not None:
            e.current_epoch.events["max_observed_clusters_within_ceiling"] = epoch_peak
        return e

    def candidates(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}

    def test_weekly_counts_are_collected_for_complete_weeks_in_the_epoch(self):
        sql = EPOCH_QUERY_METRICS_SQL
        for k in range(1, 9):
            self.assertIn(f"AS queries_week_{k}", sql)
        self.assertIn("WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 56 DAYS", sql)

    def test_growth_ratio(self):
        self.assertAlmostEqual(query_volume_growth_ratio([1200, 1300, 1250, 1400, 1000, 950, 1050, 1000]), 1.2)
        self.assertIsNone(query_volume_growth_ratio([1200, None, 1250, 1400, 1000, 950, 1050, 1000]))
        self.assertIsNone(query_volume_growth_ratio([5, 5, 5, 5, 0, 0, 0, 0]))  # no prior volume: unknown

    def test_growing_volume_holds_every_capacity_cut(self):
        # Every one of the last 4 weeks >= 1.10 x the prior 4-week median (1,000): the downsize and the ceiling cut wait.
        c = self.candidates(self.evidence([1200, 1300, 1250, 1400, 1000, 950, 1050, 1000]))
        for knob in ("warehouse_size", "max_clusters", "min_clusters"):
            self.assertIn("QUERY_VOLUME_GROWING", c[knob].blockers, knob)
            self.assertEqual(c[knob].status, "NO_CHANGE", knob)
        self.assertEqual(c["auto_stop_minutes"].status, "ACTIONABLE")  # not a capacity cut

    def test_one_burst_week_or_unknown_weeks_hold_nothing(self):
        for weeks in ([1000, 3000, 1000, 1000, 1000, 1000, 1000, 1000], None):
            c = self.candidates(self.evidence(weeks))
            self.assertEqual(c["warehouse_size"].status, "TEST_REQUIRED", weeks)
            self.assertNotIn("QUERY_VOLUME_GROWING", c["max_clusters"].blockers)

    def test_downsize_is_held_for_a_peak_outside_the_recent_window(self):
        c = self.candidates(self.evidence(None, max_clusters=8, epoch_peak=8))
        self.assertEqual(c["warehouse_size"].status, "NO_CHANGE")
        self.assertIn("PEAK_OUTSIDE_RECENT_WINDOW", c["warehouse_size"].blockers)
        c = self.candidates(self.evidence(None, max_clusters=8, epoch_peak=3))  # within the recent ceiling (2 + 1)
        self.assertEqual(c["warehouse_size"].status, "TEST_REQUIRED")

    def test_growth_threshold_default(self):
        self.assertEqual(self.t.growth_hold_weekly_ratio, 1.10)


class ChannelObservedOnlyTests(unittest.TestCase):
    """The warehouse channel is reported, never recommended: no candidate, plan step or invalidation."""

    def preview(self):
        e = demo_evidence()[0]
        for ep in e.epochs:
            ep.config = dataclasses.replace(ep.config, warehouse_channel="PREVIEW")
        return e

    def test_five_candidates_and_no_channel_knob(self):
        self.assertNotIn("warehouse_channel", ACTIVE_KNOBS)
        w = p.evaluate_warehouses([self.preview()], Thresholds())[0]
        self.assertEqual(len(w.candidates), 5)
        self.assertNotIn("warehouse_channel", {c.knob for c in w.candidates})
        for c in w.candidates:
            self.assertNotIn("warehouse_channel", c.dependencies)

    def test_preview_channel_blocks_nothing_and_is_reported(self):
        pkg = p.build_recommendation_packages([self.preview()], Thresholds())[0].to_dict()
        self.assertEqual(pkg["current_config"]["warehouse_channel"], "PREVIEW")
        plan_a = next(s for s in pkg["scenarios"] if s["status"] == "RECOMMENDED")
        self.assertNotIn("warehouse_channel", [s["knob"] for s in plan_a["execution_plan"]])
        size = next(s for s in plan_a["execution_plan"] if s["knob"] == "warehouse_size")
        self.assertNotEqual(size["execution_state"], "PENDING_REEVALUATION")
        evidence = pkg["evidence"].values() if isinstance(pkg["evidence"], dict) else pkg["evidence"]
        note = [x for x in evidence if x["metric"] == "warehouse_channel"]
        self.assertEqual(len(note), 1)
        self.assertEqual(note[0]["value"], "PREVIEW")

    def test_current_channel_has_no_note(self):
        pkg = p.build_recommendation_packages(demo_evidence(), Thresholds())[0].to_dict()
        evidence = pkg["evidence"].values() if isinstance(pkg["evidence"], dict) else pkg["evidence"]
        self.assertFalse([x for x in evidence if x["metric"] == "warehouse_channel"])
