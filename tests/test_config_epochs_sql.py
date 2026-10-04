"""Config epochs are islands of the decision fields in system.compute.warehouses, executed for real in sqlite.

A snapshot row that changes only cosmetic properties (name, tags) must not start a new configuration period:
that would reset the epoch-maturity gate, the cost history and the forecast basis.
"""
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

COLUMNS = ("account_id", "workspace_id", "warehouse_id", "warehouse_name", "warehouse_type", "warehouse_channel",
           "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes", "tags", "change_time", "delete_time")
NOW = "2026-10-01 12:00:00"


def snapshot(change_time, *, name="BI", size="MEDIUM", min_c=1, max_c=2, auto_stop=10, wtype="PRO", channel="CHANNEL_NAME_CURRENT",
             tags=None, delete_time=None, warehouse_id="w1"):
    return {"account_id": "a", "workspace_id": "1", "warehouse_id": warehouse_id, "warehouse_name": name, "warehouse_type": wtype,
            "warehouse_channel": channel, "warehouse_size": size, "min_clusters": min_c, "max_clusters": max_c,
            "auto_stop_minutes": auto_stop, "tags": json.dumps(tags or {}, sort_keys=True), "change_time": change_time,
            "delete_time": delete_time}


def run_sql(sql, rows, now=NOW):
    """Execute Databricks collector SQL in sqlite: only the table name and the clock are substituted."""
    cutoff = (dt.datetime.fromisoformat(now) - dt.timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    sql = sql.replace("system.compute.warehouses", "warehouses").replace("CURRENT_TIMESTAMP() - INTERVAL 365 DAYS", f"'{cutoff}'")
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE warehouses ({', '.join(COLUMNS)})")
    con.executemany(f"INSERT INTO warehouses VALUES ({', '.join('?' for _ in COLUMNS)})", [tuple(r[c] for c in COLUMNS) for r in rows])
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


class ConfigEpochIslandTests(unittest.TestCase):
    def epochs(self, rows):
        return run_sql(p.CONFIG_EPOCHS_SQL, rows)

    def test_tag_and_rename_snapshots_do_not_start_a_new_epoch(self):
        out = self.epochs([
            snapshot("2026-06-01 00:00:00", tags={"team": "a"}),
            snapshot("2026-09-28 00:00:00", tags={"team": "b"}),
            snapshot("2026-09-30 00:00:00", name="BI renamed", tags={"team": "b"}),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["epoch_start"], "2026-06-01 00:00:00")
        self.assertIsNone(out[0]["epoch_end"])
        self.assertEqual(out[0]["is_current"], 1)
        # Cosmetic properties come from the latest snapshot of the period.
        self.assertEqual(out[0]["warehouse_name"], "BI renamed")
        self.assertEqual(json.loads(out[0]["tags"]), {"team": "b"})

    def test_decision_field_change_starts_a_new_epoch(self):
        out = self.epochs([
            snapshot("2026-03-01 00:00:00"),
            snapshot("2026-04-01 00:00:00", tags={"x": "1"}),
            snapshot("2026-06-01 00:00:00", size="LARGE"),
            snapshot("2026-09-30 00:00:00", size="LARGE", name="renamed"),
        ])
        self.assertEqual([(r["epoch_start"], r["epoch_end"], r["warehouse_size"]) for r in out], [
            ("2026-03-01 00:00:00", "2026-06-01 00:00:00", "MEDIUM"),
            ("2026-06-01 00:00:00", None, "LARGE"),
        ])

    def test_every_decision_field_is_compared(self):
        changes = {"wtype": "SERVERLESS", "channel": "CHANNEL_NAME_PREVIEW", "size": "LARGE", "min_c": 2, "max_c": 3, "auto_stop": 30}
        for field, value in changes.items():
            with self.subTest(field=field):
                out = self.epochs([snapshot("2026-06-01 00:00:00"), snapshot("2026-07-01 00:00:00", **{field: value})])
                self.assertEqual(len(out), 2)

    def test_change_and_revert_gives_three_epochs(self):
        out = self.epochs([
            snapshot("2026-03-01 00:00:00"),
            snapshot("2026-05-01 00:00:00", auto_stop=30),
            snapshot("2026-07-01 00:00:00"),
        ])
        self.assertEqual([r["auto_stop_minutes"] for r in out], [10, 30, 10])

    def test_single_snapshot_epoch_start_is_unchanged(self):
        out = self.epochs([snapshot("2025-01-01 00:00:00")])
        self.assertEqual([(r["epoch_start"], r["is_current"]) for r in out], [("2025-01-01 00:00:00", 1)])

    def test_snapshots_with_the_same_change_time_give_one_deterministic_epoch_set(self):
        prior = snapshot("2026-03-01 00:00:00")
        renamed = snapshot("2026-06-01 00:00:00", name="renamed")
        resized = snapshot("2026-06-01 00:00:00", size="LARGE")
        a = self.epochs([prior, renamed, resized])
        b = self.epochs([prior, resized, renamed])
        self.assertEqual(a, b)
        starts = [r["epoch_start"] for r in a]
        self.assertEqual(len(starts), len(set(starts)), a)  # no zero-length epoch sharing an id with the next one

    def test_a_deleted_snapshot_wins_a_change_time_tie(self):
        live = snapshot("2026-06-01 00:00:00")
        deleted = snapshot("2026-09-01 00:00:00", delete_time="2026-09-01 00:00:00")
        undeleted_twin = snapshot("2026-09-01 00:00:00")
        self.assertEqual(self.epochs([live, deleted, undeleted_twin]), [])
        self.assertEqual(self.epochs([live, undeleted_twin, deleted]), [])

    def test_deleted_warehouse_is_not_active(self):
        out = self.epochs([snapshot("2026-06-01 00:00:00"), snapshot("2026-09-01 00:00:00", delete_time="2026-09-01 00:00:00")])
        self.assertEqual(out, [])

    def test_every_epoch_query_uses_the_shared_island_cte(self):
        sqls = {
            "CONFIG_EPOCHS_SQL": p.CONFIG_EPOCHS_SQL,
            "EPOCH_QUERY_METRICS_SQL": p.EPOCH_QUERY_METRICS_SQL,
            "EPOCH_EVENT_METRICS_SQL": p.EPOCH_EVENT_METRICS_SQL,
            "gap metrics": p.build_epoch_gap_metrics_sql(p.Thresholds()),
            "EPOCH_DAILY_COST_SQL": p.EPOCH_DAILY_COST_SQL,
            "EPOCH_BILLING_ATTRIBUTION_SQL": p.EPOCH_BILLING_ATTRIBUTION_SQL,
        }
        for name, sql in sqls.items():
            with self.subTest(sql=name):
                self.assertIn(p.CONFIG_EPOCHS_CTE, sql)
                self.assertNotIn("LEAD(change_time)", sql)


if __name__ == "__main__":
    unittest.main()
