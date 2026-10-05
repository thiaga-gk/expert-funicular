"""Billed SQL warehouses with no configuration snapshot in system.compute.warehouses.

The table keeps 365 days and writes a row only when a warehouse's configuration changes; Databricks does not document
whether an unchanged warehouse's only row survives the retention period. A live run therefore lists SQL warehouses
billed in the last 30 days that have no row in the table: a WARNING in publisher.log and a count in manifest.json. No
package is written for them (their configuration is unknown) and nothing is guessed.
"""
import contextlib
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, statement, *, label="collector_sql"):
        self.calls.append((label, statement))
        return [dict(r) for r in self.rows.get(label, [])]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


UNSEEN = [{"workspace_id": "222", "warehouse_id": "wh-b", "billed_dbu_30": 12.5, "last_usage_date": "2026-09-30"},
          {"workspace_id": "111", "warehouse_id": "wh-a", "billed_dbu_30": 40.0, "last_usage_date": "2026-10-01"}]


class UnseenBilledWarehouseTests(unittest.TestCase):
    def test_sql_reads_last_30_billed_days_against_every_snapshot(self):
        sql = p.UNSEEN_BILLED_WAREHOUSES_SQL
        self.assertIn("billing_origin_product='SQL'", sql)
        self.assertIn("usage_date>=DATE_SUB(CURRENT_DATE(),30) AND usage_date<CURRENT_DATE()", sql)
        self.assertIn("FROM system.compute.warehouses", sql)
        self.assertIn("LEFT ANTI JOIN", sql)
        self.assertIn("__INGESTION_CUTOFF__", sql)

    def test_sql_reports_only_warehouses_active_in_this_region(self):
        # Billing is account-wide but the warehouses table is regional: a warehouse billed in another region has no
        # row here and must not be reported. Regional activity = a warehouse event in the table's retention or a query
        # in the last 30 days (an always-on warehouse may emit no event for months).
        sql = " ".join(p.UNSEEN_BILLED_WAREHOUSES_SQL.split())
        self.assertIn("FROM system.compute.warehouse_events", sql)
        self.assertIn("FROM system.query.history", sql)
        self.assertIn("LEFT SEMI JOIN regional r ON b.workspace_id=r.workspace_id AND b.warehouse_id=r.warehouse_id", sql)
        self.assertLess(sql.index("LEFT SEMI JOIN regional"), sql.index("LEFT ANTI JOIN snapshots"))

    def test_billed_dbu_is_rounded(self):
        rows = [dict(UNSEEN[0], billed_dbu_30=12.123456789)]
        with self.assertLogs("publisher", level="WARNING"):
            self.assertEqual(p.collect_unseen_billed_warehouses(FakeClient({"unseen_billed_warehouses": rows}))[0]["billed_dbu_30"], 12.123457)

    def test_collect_lists_unseen_warehouses_sorted_and_warns(self):
        client = FakeClient({"unseen_billed_warehouses": UNSEEN})
        with self.assertLogs("publisher", level="WARNING") as logs:
            unseen = p.collect_unseen_billed_warehouses(client, ingestion_cutoff="2026-10-01")
        self.assertEqual([(x["workspace_id"], x["warehouse_id"]) for x in unseen], [("111", "wh-a"), ("222", "wh-b")])
        self.assertEqual(unseen[0], {"workspace_id": "111", "warehouse_id": "wh-a", "billed_dbu_30": 40.0, "last_usage_date": "2026-10-01",
                                     "reason_code": "NO_CONFIGURATION_SNAPSHOT"})
        self.assertIn("2 billed SQL warehouse(s) have no configuration snapshot", logs.output[0])
        self.assertIn("111/wh-a", logs.output[0])
        label, statement = client.calls[0]
        self.assertEqual(label, "unseen_billed_warehouses")
        self.assertIn("DATE'2026-10-01'", statement)

    def test_collect_with_none_unseen_does_not_warn(self):
        with self.assertNoLogs("publisher", level="WARNING"):
            self.assertEqual(p.collect_unseen_billed_warehouses(FakeClient({})), [])

    def test_manifest_records_the_check(self):
        logging.disable(logging.CRITICAL)
        try:
            evidence = p.synthetic_demo_evidence(1)
            packages = p.build_recommendation_packages(evidence, p.Thresholds())
            with tempfile.TemporaryDirectory() as td:
                p.package_locked_contract(Path(td), packages, evidence, generate_workbook=False,
                                          unseen_billed_warehouses=p.collect_unseen_billed_warehouses(FakeClient({"unseen_billed_warehouses": UNSEEN[:1]})))
                m = json.loads((Path(td) / "manifest.json").read_text())
                self.assertEqual((m["unseen_billed_warehouse_check"], m["unseen_billed_warehouse_count"]), ("CHECKED", 1))
                self.assertEqual(m["unseen_billed_warehouses"][0]["warehouse_id"], "wh-b")
            with tempfile.TemporaryDirectory() as td:  # demo and replay runs have no live tables to check
                p.package_locked_contract(Path(td), packages, evidence, generate_workbook=False)
                m = json.loads((Path(td) / "manifest.json").read_text())
                self.assertEqual((m["unseen_billed_warehouse_check"], m["unseen_billed_warehouse_count"], m["unseen_billed_warehouses"]),
                                 ("NOT_CHECKED", None, []))
        finally:
            logging.disable(logging.NOTSET)

    def test_live_run_checks_and_reports_in_the_manifest(self):
        logging.disable(logging.CRITICAL)
        try:
            evidence = p.synthetic_demo_evidence(1)
            client = FakeClient({"unseen_billed_warehouses": UNSEEN})
            with tempfile.TemporaryDirectory() as td, \
                    patch("publisher.config.loader._resolve_execution_args", return_value=("warehouse", "abc")), \
                    patch("publisher.analyzer.analyzer.resolve_cli_oauth", return_value=object()), \
                    patch("publisher.analyzer.analyzer.collect_workspace_compatibility_evidence", return_value={"mode": "A_HITL_FALLBACK", "status": "UNAVAILABLE"}), \
                    patch("publisher.analyzer.analyzer.SqlClient", return_value=client), \
                    patch("publisher.analyzer.analyzer.extract_evidence", return_value=evidence):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = p.main(["--profile", "X", "--sql-warehouse-id", "abc", "--source-region", "us-east-1", "--output-dir", td, "--no-workbook"])
                self.assertEqual(code, 0)
                m = json.loads((Path(td) / "manifest.json").read_text())
                self.assertEqual((m["unseen_billed_warehouse_check"], m["unseen_billed_warehouse_count"]), ("CHECKED", 2))
                self.assertNotIn("wh-a", {x["warehouse_id"] for x in m["packages"]})
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
