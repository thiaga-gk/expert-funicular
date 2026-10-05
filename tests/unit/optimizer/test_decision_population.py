"""Latency and spill gates read the decision population: queries not served from the result cache.

Result-cache hits finish in about 0 ms, so mixing them in makes the downsize latency gate more permissive than
intended (40% cache hits: executed p95 7 s is blocked, mixed p95 4.1 s passes). Failed and cancelled queries stay in:
a statement timeout is the slowest latency there is. Compilation and result-fetch time are collected as evidence of
latency a size change cannot fix.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402

POPULATION = "COALESCE(x.from_result_cache, FALSE) = FALSE"


class DecisionPopulationTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = p.Thresholds()
        self.e = copy.deepcopy(p.demo_evidence()[0])

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def size(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["warehouse_size"]

    def test_latency_and_spill_use_finished_non_cache_queries(self):
        sql = p.EPOCH_QUERY_METRICS_SQL
        self.assertIn(f"{POPULATION} AS in_decision_population", sql)
        for metric in ("p95_execution_ms_30", "p99_execution_ms_30", "max_execution_ms_30", "spill_queries_30", "p95_spill_bytes_when_spilled_30"):
            line = next(x for x in sql.splitlines() if x.rstrip(",").endswith(f"AS {metric}"))
            self.assertIn("in_decision_population", line, metric)
        self.assertIn("AS decision_queries_30", sql)
        self.assertNotIn("execution_status = 'FINISHED'", sql)  # timeouts (cancelled at the limit) stay in the tail
        self.assertIn("NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population),0) AS spill_query_pct_30", sql)

    def test_size_sample_counts_decision_queries_only(self):
        # 12,000 queries, but only 80 executed (the rest cache hits or failures): too few to judge latency.
        self.e.current_epoch.query["decision_queries_30"] = 80
        size = self.size(self.e)
        self.assertNotIn(size.status, {"ACTIONABLE", "TEST_REQUIRED"})
        self.assertIn("INSUFFICIENT_STRONG_QUERY_SAMPLE", size.blockers)

    def test_unknown_decision_sample_fails_closed(self):
        self.e.current_epoch.query.pop("decision_queries_30", None)
        size = self.size(self.e)
        self.assertIn("DECISION_QUERY_EVIDENCE_UNAVAILABLE", size.blockers)
        self.assertNotIn(size.status, {"ACTIONABLE", "TEST_REQUIRED"})

    def test_compilation_and_result_fetch_are_evidence_only(self):
        sql = p.EPOCH_QUERY_METRICS_SQL
        self.assertIn("AS p95_compilation_ms_30", sql)
        self.assertIn("AS p95_result_fetch_ms_30", sql)
        decisions = []
        for compile_ms, fetch_ms in ((10, 10), (60000, 120000)):
            e = copy.deepcopy(self.e)
            e.current_epoch.query.update(p95_compilation_ms_30=compile_ms, p95_result_fetch_ms_30=fetch_ms)
            size = self.size(e)
            found = {f.metric: f.value for f in size.findings}
            self.assertEqual((found["p95_compilation_ms_30"], found["p95_result_fetch_ms_30"]), (compile_ms, fetch_ms))
            decisions.append((size.status, size.recommended_config, size.blockers, size.estimated_savings_365))
        self.assertEqual(decisions[0], decisions[1])


if __name__ == "__main__":
    unittest.main()
