"""Collector SQL measured for a named configuration epoch and as-of time, not only the current epoch up to now.

The Realizer measures a post-change epoch that may already be closed (a later change ended it) and the epoch before the
change; a backtest measures past changes. The running-idle and canary guard statements therefore take an epoch, an
as-of time and the auto-stop values to replay, while the Publisher's own statements render byte for byte as before.
"""
import datetime as dt
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector import demo  # noqa: E402
from publisher.analyzer.collector.queries import events, query_history  # noqa: E402
from publisher.analyzer.metrics import running_idle_metrics  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402

WINDOWS = [{"workspace_id": "1234", "warehouse_id": "wh-1", "label": "long", "start": "2026-07-01", "end": "2026-09-29"},
           {"workspace_id": "1234", "warehouse_id": "wh-1", "label": "epoch", "start": None, "end": None}]
POST = {"workspace_id": "1234", "warehouse_id": "wh-1", "label": "post", "start": "2026-10-09", "end": "2026-11-05",
        "epoch_start": "2026-10-08T14:32:00+00:00"}
PERIOD = {"workspace_id": "1", "warehouse_id": "w", "epoch_start": "2026-10-08T14:32:00+00:00",
          "previous": ["2026-09-10", "2026-10-07"], "current": ["2026-10-09", "2026-11-05"]}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class DefaultStatementsUnchangedTests(unittest.TestCase):
    """The Publisher's statements are the ones it ran before the parameters existed (hashes taken at master 8157a16)."""

    def test_current_epoch_statements_render_byte_for_byte(self):
        t = Thresholds()
        self.assertEqual(sha(events.build_running_idle_sql(t, WINDOWS)), "2b00c53737c8a710c901cf117234858aeaff334ae290013ada50687571824e53")
        self.assertEqual(sha(events.build_running_idle_targets_sql(t, WINDOWS)), "f8178ce24f981cde490f9ca0d3ce2555a51904db243f1954e7f1181d830cd767")
        self.assertEqual(sha(query_history.CANARY_GUARD_SQL), "3d73982718cd3cca4b30d592239b30b6565f6d0c0b1944237946dd0079ad040d")
        self.assertIsNone(events.running_idle_request_from_statement(events.build_running_idle_sql(t, WINDOWS)))


class EpochBoundRunningIdleTests(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_a_named_epoch_is_measured_up_to_its_end_or_the_as_of_time(self):
        sql = events.build_running_idle_targets_sql(self.t, [POST], targets=[30], as_of="2026-11-07T12:00:00Z")
        self.assertIn("m.epoch_start = c.epoch_start", sql)
        self.assertIn("('1234', 'wh-1', TIMESTAMP'2026-10-08T14:32:00.000000Z')", sql)
        self.assertIn("LEAST(COALESCE(c.epoch_end, TIMESTAMP'2026-11-07T12:00:00.000000Z'), TIMESTAMP'2026-11-07T12:00:00.000000Z') AS w_end", sql)
        self.assertIn("AND event_time < TIMESTAMP'2026-11-07T12:00:00.000000Z'", sql)  # no event after the as-of time
        self.assertIn("EXPLODE(ARRAY(30))", sql)
        self.assertNotIn("WHERE c.epoch_end IS NULL", sql)
        self.assertEqual(events.running_idle_request_from_statement(sql),
                         {"as_of": "2026-11-07T12:00:00Z", "epochs": {"1234/wh-1": "2026-10-08T14:32:00+00:00"}, "targets": [30]})
        self.assertEqual(events.running_idle_windows_from_statement(sql), [POST])

    def test_without_an_as_of_time_an_open_epoch_runs_to_now(self):
        sql = events.build_running_idle_sql(self.t, [POST])
        self.assertIn("LEAST(COALESCE(c.epoch_end, CURRENT_TIMESTAMP()), CURRENT_TIMESTAMP()) AS w_end", sql)
        self.assertIn("EXPLODE(SEQUENCE(5, 1440, 5))", events.build_running_idle_targets_sql(self.t, [POST]))

    def test_targets_alone_keep_the_current_epoch(self):
        sql = events.build_running_idle_targets_sql(self.t, WINDOWS[:1], targets=[events.NEVER_STOP_TARGET_MINUTES])
        self.assertIn("WHERE c.epoch_end IS NULL", sql)
        self.assertIn(f"EXPLODE(ARRAY({events.NEVER_STOP_TARGET_MINUTES}))", sql)

    def test_requests_that_cannot_be_answered_unambiguously_are_refused(self):
        bad = [
            dict(windows=[POST, dict(POST, label="pre", epoch_start=None)]),  # some windows name an epoch, some not
            dict(windows=[POST, dict(POST, label="pre", epoch_start="2026-09-01T00:00:00+00:00")]),  # two epochs, one warehouse
            dict(windows=[POST], targets=[0]),
            dict(windows=[POST], targets=[30, 30]),
            dict(windows=[POST], targets=[]),
            dict(windows=[POST], as_of="2026-11-07T12:00:00"),  # no UTC offset
            dict(windows=[dict(POST, epoch_start="2026-10-08' OR '1'='1")]),
            dict(windows=[POST], as_of="now()"),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(PublisherError):
                events.build_running_idle_sql(self.t, kwargs.pop("windows"), **kwargs)


class WindowedCanaryGuardTests(unittest.TestCase):
    def test_explicit_periods_on_the_same_sources_and_columns(self):
        sql = query_history.build_canary_guard_sql([PERIOD])
        self.assertIn("TIMESTAMP'2026-09-10T00:00:00Z', TIMESTAMP'2026-10-08T00:00:00Z', TIMESTAMP'2026-10-09T00:00:00Z', TIMESTAMP'2026-11-06T00:00:00Z'", sql)
        self.assertIn("CASE WHEN x.start_time >= p.cur_a THEN 'CURRENT' ELSE 'PREVIOUS' END AS period", sql)
        self.assertNotIn("CURRENT_TIMESTAMP()", sql)  # the periods alone decide what is read
        for field in query_history.CANARY_GUARD_FIELDS:
            self.assertIn(f"AS {field}", sql)
        default = query_history.CANARY_GUARD_SQL
        self.assertEqual(sql.split("), shared AS (")[1], default.split("), shared AS (")[1])  # same aggregation
        self.assertEqual(query_history.canary_guard_periods_from_statement(sql), [PERIOD])

    def test_periods_that_overlap_repeat_or_inject_are_refused(self):
        bad = [[dict(PERIOD, previous=["2026-09-10", "2026-10-09"])], [PERIOD, PERIOD], [dict(PERIOD, warehouse_id="w' --")],
               [dict(PERIOD, epoch_start="2026-10-08")], [dict(PERIOD, current=["2026-10-09", "2026-10-0X"])], []]
        for periods in bad:
            with self.subTest(periods=periods), self.assertRaises((PublisherError, ValueError)):
                query_history.build_canary_guard_sql(periods)


class DemoWorldReplayTests(unittest.TestCase):
    """The demo client answers epoch-bound statements with the reference arithmetic on the simulated timeline."""

    @classmethod
    def setUpClass(cls):
        spec = dict(next(s for s in demo.DEMO_FLEET if s["scenario"] == "C13"))
        cls.spec = {**spec, "auto_stop": 10, "epoch_start": "2026-10-08", "prior": [{"start": "2025-07-01", "auto_stop": 30}]}
        cls.analysis = "2026-11-07T12:00:00+00:00"
        cls.rows = demo.simulate_demo_world(cls.spec, 13, cls.analysis)
        cls.t = Thresholds()

    def bound(self, label, windows, **kwargs):
        build = events.build_running_idle_targets_sql if label == "running_idle_targets" else events.build_running_idle_sql
        return demo.replay_demo_rows(self.rows, build(self.t, windows, **kwargs), label)

    def epoch_input(self, start):
        return next(x for x in self.rows["running_idle_inputs"] if x["epoch_start"].startswith(start))

    def test_every_epoch_keeps_its_inputs_only_in_world_mode(self):
        self.assertEqual(sorted(x["epoch_start"][:10] for x in self.rows["running_idle_inputs"]), ["2025-07-01", "2026-10-08"])
        plain = demo._simulate_demo_warehouse(self.spec, 13, self.analysis)
        self.assertEqual([x["epoch_start"][:10] for x in plain["running_idle_inputs"]], ["2026-10-08"])
        self.assertNotIn("world_guard", plain)

    def test_the_post_change_epoch_replays_the_old_value(self):
        post = dict(POST, workspace_id=self.rows["config_epochs"][0]["workspace_id"], warehouse_id="wh-demo-013",
                    epoch_start=self.epoch_input("2026-10-08")["epoch_start"])
        window = self.bound("running_idle", [post], targets=[30])[0]
        target = self.bound("running_idle_targets", [post], targets=[30])
        self.assertEqual([r["target_min"] for r in target], [30])
        self.assertAlmostEqual(window["observed_running_cluster_minutes"], 3640.0, places=3)
        self.assertAlmostEqual(target[0]["added_idle_cluster_minutes"], 5600.0, places=3)  # 20 more minutes in 280 gaps
        self.assertEqual(target[0]["avoided_starts"], 0)  # 42-minute gaps: 30 minutes never bridge them

    def test_a_closed_epoch_matches_the_reference_arithmetic_up_to_the_as_of_time(self):
        x = self.epoch_input("2025-07-01")
        ws = x["workspace_id"]
        pre = {"workspace_id": ws, "warehouse_id": "wh-demo-013", "label": "pre", "start": "2026-09-10", "end": "2026-10-07", "epoch_start": x["epoch_start"]}
        rows = self.bound("running_idle_targets", [pre], targets=[10], as_of="2026-10-20T00:00:00+00:00")
        t0 = dt.datetime.fromisoformat(x["t0"])
        m = lambda d, s=0: (dt.datetime.combine(dt.date.fromisoformat(d) + dt.timedelta(days=s), dt.time(), dt.timezone.utc) - t0).total_seconds() / 60.0
        cov, hi = x["epoch_window"]
        ref = running_idle_metrics([tuple(b) for b in x["busy"]], [tuple(s) for s in x["segments"]], (cov, hi), {"pre": (m("2026-09-10"), m("2026-10-07", 1))},
                                   30, [10], x["floor_clusters"], self.t.running_idle_full_window_fraction, self.t.auto_stop_bridge_margin_pct,
                                   self.t.kept_alive_gap_tolerance_minutes, self.t.kept_alive_stopped_gap_minutes)["pre"]["running_idle_by_target"]["10"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["removable_running_cluster_minutes"], ref["removable_running_cluster_minutes"], places=3)
        self.assertGreater(ref["removable_running_cluster_minutes"], 0)
        # An as-of time before the epoch's evidence starts measures nothing.
        self.assertEqual(self.bound("running_idle", [pre], as_of="2025-06-01T00:00:00+00:00"), [])

    def test_windowed_canary_guard_counts_each_period(self):
        x = self.epoch_input("2026-10-08")
        rows = demo.replay_demo_rows(self.rows, query_history.build_canary_guard_sql([dict(PERIOD, workspace_id=x["workspace_id"], warehouse_id="wh-demo-013", epoch_start=x["epoch_start"])]), "canary_guard")
        by = {r["period"]: r for r in rows}
        self.assertEqual(set(by), {"CURRENT", "PREVIOUS"})
        self.assertEqual(by["CURRENT"]["queries"], by["PREVIOUS"]["queries"])  # 14 queries a weekday, 20 weekdays in each
        self.assertEqual(by["CURRENT"]["queries"], 280)
        self.assertEqual(by["CURRENT"]["p95_execution_ms_shared_sources"], 2500)


if __name__ == "__main__":
    unittest.main()
