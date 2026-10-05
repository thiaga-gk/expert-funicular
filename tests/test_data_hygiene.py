"""Data hygiene in the collector and normalizer.

Redacted owners are unknown, not owners; statement text counts as visible only when it is real text; a Hive reference
count is unknown when no statement text is visible; a MAP column arrives as a mapping, a JSON object, or a list of
key/value pairs depending on the connector; the I/O cache share is weighted by bytes read.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


class OwnerRedactionTests(unittest.TestCase):
    def test_redacted_owner_values_are_unknown(self):
        for value in ("__REDACTED__", "<REDACTED>", "redacted", "  ", ""):
            self.assertIsNone(p.clean_owner(value), value)
        self.assertEqual(p.clean_owner(" ana@example.com "), "ana@example.com")

    def test_attribution_sql_drops_redacted_owners_before_picking_the_latest(self):
        sql = " ".join(p.EPOCH_BILLING_ATTRIBUTION_SQL.split())
        self.assertTrue("UPPER(TRIM(identity_metadata.owned_by)) IN ('__REDACTED__','<REDACTED>','REDACTED')" in sql, "expected SQL fragment missing")


class StatementVisibilityTests(unittest.TestCase):
    def test_empty_statement_text_is_not_visible(self):
        sql = " ".join(p.EPOCH_QUERY_METRICS_SQL.split())
        self.assertTrue("statement_text IS NOT NULL AND TRIM(statement_text) <> '' AND statement_text <> '<REDACTED>'" in sql, "expected SQL fragment missing")

    def test_hive_reference_count_is_unknown_without_visible_text(self):
        logging.disable(logging.CRITICAL)
        try:
            t = p.Thresholds()
            e = copy.deepcopy(p.synthetic_demo_evidence()[0])
            e.current_epoch.query["statement_text_visible_pct_30"] = 0.0
            e.current_epoch.query["hive_metastore_reference_queries_30"] = 0
            facts = p.Analyzer(t).analyze(e)["warehouse_type"].facts
            self.assertIsNone(facts["hive_metastore_reference_queries_30"])
            e.current_epoch.query["statement_text_visible_pct_30"] = 40.0
            self.assertEqual(p.Analyzer(t).analyze(e)["warehouse_type"].facts["hive_metastore_reference_queries_30"], 0)
        finally:
            logging.disable(logging.NOTSET)


class MapDecodingTests(unittest.TestCase):
    def test_list_of_pairs_and_key_value_entries_decode_to_a_mapping(self):
        expected = {"team": "bi", "env": "prod"}
        self.assertEqual(p.normalize_map([["team", "bi"], ["env", "prod"]]), expected)
        self.assertEqual(p.normalize_map([("team", "bi"), ("env", "prod")]), expected)
        self.assertEqual(p.normalize_map([{"key": "team", "value": "bi"}, {"key": "env", "value": "prod"}]), expected)
        self.assertEqual(p.normalize_map('[["team", "bi"], ["env", "prod"]]'), expected)
        self.assertEqual(p.normalize_map({"team": "bi"}), {"team": "bi"})
        self.assertEqual(p.normalize_map('{"team": "bi"}'), {"team": "bi"})
        self.assertEqual(p.normalize_map(None), {})

    def test_undecodable_values_are_kept_raw(self):
        self.assertEqual(p.normalize_map("not json"), {"raw": "not json"})
        self.assertEqual(p.normalize_map([["only-one"]]), {"raw": "[['only-one']]"})


class IoCacheWeightingTests(unittest.TestCase):
    def test_io_cache_share_is_weighted_by_bytes_read(self):
        sql = " ".join(p.EPOCH_QUERY_METRICS_SQL.split())
        self.assertFalse("AVG(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN read_io_cache_percent END)" in sql, "unweighted average still present")
        self.assertTrue("SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND read_io_cache_percent IS NOT NULL THEN read_io_cache_percent * COALESCE(read_bytes,0) END)" in sql, "expected SQL fragment missing")



class ReviewFollowUpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.fleet = p.synthetic_demo_evidence()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def scenario(self, name):
        return copy.deepcopy(next(e for e in self.fleet if e.current_epoch.tags["golden_scenario"] == name))

    def test_hive_compatibility_evidence_is_null_without_visible_text(self):
        e = self.scenario("C01")
        e.current_epoch.query["statement_text_visible_pct_30"] = 0.0
        e.current_epoch.query["hive_metastore_reference_queries_30"] = 0
        w = p.evaluate_warehouses([e], self.t)[0]
        planner = next(c for s in w.scenarios for c in s.compatibility.get("checks", []) if c["check"] == "legacy_hive_metastore_sql_reference")
        self.assertEqual(planner["status"], "UNKNOWN")  # not PASS_WITH_LIMITATION or REVIEW
        pkg = p.build_recommendation_packages([e], self.t)[0].to_dict()
        check = next(c for s in pkg["scenarios"] for c in s["compatibility"]["checks"] if c["check"] == "legacy_hive_metastore_sql_reference")
        self.assertIn('"reference_queries":null', check["details"])

    def test_replayed_evidence_drops_redacted_owners(self):
        e = self.scenario("C01")
        raw = p.as_serializable(e)
        raw["attribution"]["owner"] = "__REDACTED__"
        raw["attribution"]["observed_owners"] = ["ana@example.com", "<REDACTED>"]
        back = p.evidence_from_dict(raw)
        self.assertIsNone(back.attribution.owner)
        self.assertEqual(back.attribution.observed_owners, ["ana@example.com"])

    def test_unknown_io_cache_share_is_null_not_zero(self):
        e = self.scenario("C06")
        e.current_epoch.query["avg_read_io_cache_percent_30"] = None
        findings = p.Analyzer(self.t).analyze(e)["warehouse_size"].findings
        self.assertIsNone(next(f for f in findings if f.metric == "avg_read_io_cache_percent_30").value)


if __name__ == "__main__":
    unittest.main()
