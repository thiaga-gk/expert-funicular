"""Collector SQL for the Realizer's size and cluster-count counterfactuals, and the demo simulator's answers to it.

A size change is measured with the size runtime statement (execution times on the statement sources of both periods and
the busy wall-clock minutes of each); a cluster-count change with the running minutes by cluster count of each window.
Neither statement is run by the Publisher itself. The simulator answers both, and a world can make queries slower or
faster by warehouse size (``runtime_factor_by_size``), which leaves the Publisher's demo fleet unchanged.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector import demo  # noqa: E402
from publisher.analyzer.collector.queries import events, query_history  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402

PERIOD = {"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-10-08T14:32:00+00:00",
          "previous": ["2026-09-10", "2026-10-07"], "current": ["2026-10-09", "2026-11-05"]}
POST = {"workspace_id": "1234", "warehouse_id": "wh-1", "label": "post", "start": "2026-10-09", "end": "2026-11-05",
        "epoch_start": "2026-10-08T14:32:00+00:00"}
ANALYSIS = "2026-11-07T12:00:00+00:00"


class SizeRuntimeSqlTests(unittest.TestCase):
    def test_matched_sources_at_the_post_change_mix(self):
        sql = query_history.build_size_runtime_sql([PERIOD], min_source_queries=5)
        self.assertEqual(query_history.size_runtime_request_from_statement(sql), {"min_source_queries": 5, "periods": [PERIOD]})
        self.assertIn("WHERE c.n >= 5 AND v.n >= 5", sql)
        self.assertIn("SUM(c.n * c.median_ms) AS post_ms_at_post_median, SUM(c.n * v.median_ms) AS post_ms_at_pre_median", sql)
        self.assertIn("executed AND execution_status = 'FINISHED'", sql)
        self.assertIn("SHA2(x.statement_text, 256)", sql)  # the canary guard's statement source, never the text itself
        for field in query_history.SIZE_RUNTIME_FIELDS:
            self.assertIn(f" AS {field}", sql)

    def test_requests_that_could_change_the_sql_are_refused(self):
        with self.assertRaises(PublisherError):
            query_history.build_size_runtime_sql([dict(PERIOD, warehouse_id="w'; DROP")], min_source_queries=5)
        with self.assertRaises(PublisherError):
            query_history.build_size_runtime_sql([dict(PERIOD, previous=["2026-10-01", "2026-10-10"])], min_source_queries=5)
        with self.assertRaises(PublisherError):
            query_history.build_size_runtime_sql([PERIOD], min_source_queries=0)
        with self.assertRaises(PublisherError):
            query_history.build_size_runtime_sql([], min_source_queries=5)


class ClusterLevelSqlTests(unittest.TestCase):
    def test_running_minutes_by_cluster_count_of_a_named_epoch(self):
        sql = events.build_cluster_levels_sql(Thresholds(), [POST], as_of="2026-11-07T12:00:00Z")
        self.assertIn("m.epoch_start = c.epoch_start", sql)
        self.assertIn("GROUP BY c.workspace_id, c.warehouse_id, c.epoch_start, w.label, s.cluster_count", sql)
        self.assertEqual(events.running_idle_windows_from_statement(sql), [POST])
        self.assertEqual(events.running_idle_request_from_statement(sql)["epochs"], {"1234/wh-1": "2026-10-08T14:32:00+00:00"})

    def test_windows_must_name_their_epoch(self):
        with self.assertRaises(PublisherError):
            events.build_cluster_levels_sql(Thresholds(), [dict(POST, epoch_start=None)])


def _world(scenario, index, after, prior, runtime=None):
    spec = dict(next(s for s in demo.DEMO_FLEET if s["scenario"] == scenario))
    spec.update({**after, "epoch_start": "2026-10-08", "epoch_start_minute": 14 * 60 + 32, "prior": [{"start": "2025-07-01", **prior}]})
    if runtime:
        spec["runtime_factor_by_size"] = runtime
    return demo.simulate_demo_world(spec, index, ANALYSIS)


class DemoAnswersTests(unittest.TestCase):
    """C06 (Sales Analytics, LARGE, one dashboard query a minute) cut to MEDIUM; C11 (Pinned Capacity, three clusters)
    with its floor cut to one."""

    def size_runtime(self, rows):
        x = rows["running_idle_inputs"][-1]
        period = dict(PERIOD, workspace_id=x["workspace_id"], warehouse_id=x["warehouse_id"], epoch_start=x["epoch_start"])
        return demo.replay_demo_rows(rows, query_history.build_size_runtime_sql([period], min_source_queries=5), "size_runtime")

    def test_size_runtime_is_one_when_queries_run_as_long_on_either_size(self):
        [row] = self.size_runtime(_world("C06", 6, {"size": "MEDIUM"}, {"size": "LARGE"}))
        self.assertEqual(row["matched_sources"], 1)  # one dashboard source in both windows
        self.assertEqual(row["post_ms_at_post_median"], row["post_ms_at_pre_median"])
        self.assertEqual(row["post_busy_wall_minutes"], 3600.0)  # 0.3 of each minute, 600 minutes a weekday, 20 weekdays
        self.assertEqual(row["pre_busy_wall_minutes"], 3600.0)

    def test_a_world_slower_on_the_new_size_shows_its_runtime_factor(self):
        [row] = self.size_runtime(_world("C06", 6, {"size": "MEDIUM"}, {"size": "LARGE"}, runtime={"MEDIUM": 1.2}))
        self.assertAlmostEqual(row["post_ms_at_post_median"] / row["post_ms_at_pre_median"], 1.2, places=9)
        self.assertAlmostEqual(row["post_busy_wall_minutes"], 4320.0, places=6)  # busy time 1.2 x as long
        self.assertEqual(row["pre_busy_wall_minutes"], 3600.0)

    def test_running_minutes_by_cluster_count_follow_the_simulated_timeline(self):
        rows = _world("C11", 11, {"min": 1}, {"min": 3})
        post = rows["running_idle_inputs"][-1]
        pre = rows["running_idle_inputs"][0]
        ask = lambda x, label, first, last: dict(POST, workspace_id=x["workspace_id"], warehouse_id=x["warehouse_id"], label=label, start=first,  # noqa: E731
                                                 end=last, epoch_start=x["epoch_start"])
        got = demo.replay_demo_rows(rows, events.build_cluster_levels_sql(Thresholds(), [ask(post, "post", "2026-10-09", "2026-11-05")], as_of=ANALYSIS),
                                    "cluster_levels", {})
        self.assertEqual([(r["cluster_count"], r["running_minutes"]) for r in got], [(1, 12600.0)])  # nine concurrent queries need one cluster
        got = demo.replay_demo_rows(rows, events.build_cluster_levels_sql(Thresholds(), [ask(pre, "pre", "2026-09-10", "2026-10-07")], as_of=ANALYSIS),
                                    "cluster_levels", {})
        self.assertEqual([(r["cluster_count"], r["running_minutes"]) for r in got], [(3, 12600.0)])  # the floor held three

    def test_the_demo_fleet_is_unchanged_by_the_world_only_keys(self):
        spec = next(s for s in demo.DEMO_FLEET if s["scenario"] == "C06")
        plain = demo._simulate_demo_warehouse(spec, 6, demo.DEMO_ANALYSIS_TIME)
        same = demo._simulate_demo_warehouse({**spec, "runtime_factor_by_size": {}}, 6, demo.DEMO_ANALYSIS_TIME)
        self.assertEqual(plain, same)
        self.assertNotIn("world_guard", plain)


if __name__ == "__main__":
    unittest.main()
