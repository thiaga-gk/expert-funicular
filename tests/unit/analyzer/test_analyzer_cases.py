import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.queries import billing as billing_queries  # noqa: E402
from publisher.analyzer.blockers import dbu_baseline_blocker  # noqa: E402
from publisher.analyzer.collector.billing import billing_check_through, effective_billing_watermark  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.queries.billing import BILLING_WATERMARK_SQL, EPOCH_DAILY_COST_SQL  # noqa: E402
from publisher.analyzer.collector.queries.events import EPOCH_EVENT_METRICS_SQL, build_epoch_gap_metrics_sql  # noqa: E402
from publisher.analyzer.collector.queries.query_history import EPOCH_QUERY_METRICS_SQL  # noqa: E402
from publisher.analyzer.collector.replay import load_evidence_files, save_evidence  # noqa: E402
from publisher.analyzer.collector.sql_executor import SqlClient  # noqa: E402
from publisher.analyzer.collector.workspace_api import _load_profile_host, resolve_cli_oauth  # noqa: E402
from publisher.analyzer.findings import Analyzer  # noqa: E402
from publisher.analyzer.forecast import Modeler  # noqa: E402
from publisher.analyzer.metrics import blended_daily_rate  # noqa: E402
from publisher.analyzer.normalizer import infer_photon_observation  # noqa: E402
from publisher.common.ids import epoch_id  # noqa: E402
from publisher.common.time import parse_datetime  # noqa: E402
from publisher.common.utils import optional_float  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS  # noqa: E402
from publisher.config.versions import EVIDENCE_FORMAT_VERSION, PUBLISHER_VERSION  # noqa: E402
from publisher.packager.adapter import _forecast_confidence  # noqa: E402
from publisher.reviewer.package import validate_locked_package_schema  # noqa: E402
import datetime as dt  # noqa: E402
from tests.golden import load_case  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402


class AnalyzerCases(PublisherCase):
    def test_current_epoch_drives_findings_and_history_only_measures_size_rates(self):
        current = self.evidence[0].current_epoch_id
        for c in self.w.candidates:
            for f in c.findings:
                if f.metric != "serverless_usage_pct_365":
                    self.assertEqual(f.epoch_id, current)
        # The prior MEDIUM epoch contributes its billed DBU per cluster-hour (24), which validates LARGE -> MEDIUM
        # against the published ratio; its cost per 1,000 queries no longer enters the price.
        size = self.by_knob["warehouse_size"]
        self.assertNotIn("historical_epoch_calibration", size.metadata)
        self.assertAlmostEqual(size.metadata["capacity_ratio"], 24.0 / 40.0, places=2)

    def test_query_sql_collects_epoch_task_time(self):
        self.assertIn("SUM(total_task_duration_ms) AS epoch_task_duration_ms", EPOCH_QUERY_METRICS_SQL)

    def test_utilization_is_reported_on_size_and_floor(self):
        e = copy.deepcopy(self.evidence[0])
        e.current_epoch.query["epoch_task_duration_ms"] = 60000.0 * e.current_epoch.events["cluster_minutes"] * 0.4
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        for knob in ("warehouse_size", "min_clusters"):
            self.assertAlmostEqual(self._utilization(by, knob), 0.4, places=4)

    def test_missing_task_time_is_unknown(self):
        e = copy.deepcopy(self.evidence[0])
        e.current_epoch.query.pop("epoch_task_duration_ms", None)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertIsNone(self._utilization(by, "warehouse_size"))

    def test_missing_pressure_evidence_is_reported_as_unknown_not_zero(self):
        w = p.evaluate_warehouses([self._without_pressure_evidence()], self.t)[0]
        values = {(c.knob, f.metric): f.value for c in w.candidates for f in c.findings}
        self.assertIsNone(values[("warehouse_size", "spill_query_pct_30")])
        self.assertIsNone(values[("warehouse_size", "queued_query_pct_30")])
        pkg = p.build_recommendation_packages([self._without_pressure_evidence()], self.t)[0]
        validate_locked_package_schema(pkg.to_dict())

    def test_unparseable_pressure_value_is_unknown(self):
        self.assertIsNone(optional_float("n/a"))
        self.assertIsNone(optional_float(None))
        self.assertEqual(optional_float("1.5"), 1.5)

    def test_known_queue_pressure_is_not_reported_as_missing_evidence(self):
        e = copy.deepcopy(self.evidence[0])
        e.current_epoch.query.update(queued_query_pct_30=3.0, queued_queries_30=None, p95_wait_capacity_ms_when_queued_30=None)
        mx = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]
        self.assertEqual(mx.recommended_config, mx.current_config)
        self.assertNotIn("QUEUE_WAIT_EVIDENCE_UNAVAILABLE", mx.blockers)

    def test_workload_profile_reports_missing_pressure_as_unknown(self):
        pkg = p.build_recommendation_packages([self._without_pressure_evidence(("p95_execution_ms_30", "queued_query_pct_30"))], self.t)[0]
        profile = pkg.to_dict()["workload_profile"]["workload_characteristics"]
        self.assertEqual(profile["latency_class"], "UNKNOWN")
        self.assertEqual(profile["concurrency_pattern"], "UNKNOWN")

    def test_query_source_tag_cache_evidence_exposed(self):
        metrics = {f.metric for c in self.w.candidates for f in c.findings}
        for metric in ("avg_read_io_cache_percent_30", "tagged_query_pct_30", "dashboard_queries_30", "job_queries_30"):
            self.assertIn(metric, metrics)

    def test_identical_evidence_merge_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            save_evidence(a, self.evidence)
            save_evidence(b, self.evidence)
            merged = load_evidence_files([str(a), str(b)])
            self.assertEqual(len(merged), 1)
            self.assertEqual(len(merged[0].epochs), 2)

    def test_same_warehouse_id_in_different_workspaces_is_not_collapsed(self):
        other = copy.deepcopy(self.evidence[0])
        other.workspace_id = "9999999999999999"
        other.current_epoch_id = epoch_id(other.workspace_id, other.warehouse_id, other.current_epoch.epoch_start)
        for ep in other.epochs:
            ep.workspace_id = other.workspace_id
            ep.epoch_id = epoch_id(other.workspace_id, other.warehouse_id, ep.epoch_start)
            if ep.is_current:
                other.current_epoch_id = ep.epoch_id
        for row in other.daily_cost_365:
            row["workspace_id"] = other.workspace_id
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            save_evidence(a, self.evidence)
            save_evidence(b, [other])
            merged = load_evidence_files([str(a), str(b)])
            self.assertEqual(len(merged), 2)

    def test_mixed_currency_rejected(self):
        model = Modeler(self.t)
        rows = [
            {"usage_date": "2026-01-01", "list_cost": 1, "currency_code": "USD"},
            {"usage_date": "2026-01-02", "list_cost": 1, "currency_code": "EUR"},
        ]
        with self.assertRaises(ValueError):
            model.forecast_from_daily_cost(rows, "2025-01-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")

    def test_forecast_zero_fills_calendar_days(self):
        model = Modeler(self.unsettled())
        # 15-day epoch with no billing on 09-25 and 09-28: those count as $0 days. The rate uses the last two whole weeks.
        rows = [{"usage_date": f"2026-09-{d:02d}", "list_cost": 100, "currency_code": "USD"} for d in range(16, 31) if d not in (25, 28)]
        out = model.forecast_from_daily_cost(rows, "2026-09-16T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        self.assertEqual(out["cost_history_days"], 15)
        self.assertEqual(out["active_cost_days"], 13)
        self.assertEqual(out["zero_cost_days_in_basis"], 2)
        self.assertEqual((out["rate_basis_days"], out["rate_billed_days"]), (14, 12))
        self.assertAlmostEqual(out["forecast_cost_365"], 100 * 12 / 14 * 365, places=2)

    def test_cv_is_unknown_when_history_is_too_short(self):
        # 4 days < forecast_cv_min_days (7): the old code published CV 0.0 = "perfectly stable".
        model = Modeler(self.t)
        out = model.forecast_from_daily_cost(self._cost_rows(4, "2026-09-26"), "2026-09-26T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        self.assertIsNone(out["cost_cv_84"])

    def test_cv_is_unknown_without_any_cost_basis(self):
        model = Modeler(self.t)
        out = model.forecast_from_daily_cost([], "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        self.assertIsNone(out["cost_cv_84"])

    def test_cv_is_unknown_when_mean_cost_is_zero(self):
        model = Modeler(self.t)
        out = model.forecast_from_daily_cost(self._cost_rows(30, cost=lambda i: 0.0), "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        self.assertIsNone(out["cost_cv_84"])

    def test_cv_is_computed_when_history_is_sufficient(self):
        model = Modeler(self.unsettled())
        out = model.forecast_from_daily_cost(self._cost_rows(30, cost=lambda i: 50.0 if i % 2 else 150.0), "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        self.assertAlmostEqual(out["cost_cv_84"], 0.5, places=3)

    def test_unknown_cv_is_unknown_stability_not_perfect_stability(self):
        known = copy.deepcopy(self.evidence)
        unknown = copy.deepcopy(self.evidence)
        known[0].cost_cv_84 = 0.0
        unknown[0].cost_cv_84 = None
        c_known = self.candidate_of(known, "warehouse_size")
        c_unknown = self.candidate_of(unknown, "warehouse_size")
        self.assertEqual(c_known.confidence["components"]["stability"], 1.0)
        self.assertIsNone(c_unknown.confidence["components"]["stability"])
        # Unknown stability earns no credit, so confidence can only go down (some candidates sit at a cap).
        scores = [(self.candidate_of(known, k).confidence["score"], self.candidate_of(unknown, k).confidence["score"]) for k in ACTIVE_KNOBS]
        self.assertTrue(all(u <= k for k, u in scores))
        self.assertTrue(any(u < k for k, u in scores))
        fc = _forecast_confidence(unknown[0], self.t)
        self.assertIsNone(fc.components["cost_stability"])
        self.assertLess(fc.score, _forecast_confidence(known[0], self.t).score)

    def test_unknown_cv_survives_evidence_roundtrip_and_merge(self):
        self.evidence[0].cost_cv_84 = None
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            save_evidence(a, self.evidence)
            save_evidence(b, self.evidence)
            self.assertIsNone(load_evidence_files([str(a)])[0].cost_cv_84)
            merged = load_evidence_files([str(a), str(b)])
            self.assertIsNone(merged[0].cost_cv_84)

    def test_failure_cancel_rate_is_reported_as_a_size_finding(self):
        ev = copy.deepcopy(self.evidence)
        ev[0].current_epoch.query["failure_cancel_pct_30"] = 8.0
        analysis = Analyzer(self.t).analyze(ev[0])["warehouse_size"]
        finding = next(f for f in analysis.findings if f.metric == "failure_cancel_pct_30")
        self.assertEqual(finding.value, 8.0)
        self.assertEqual(finding.threshold, self.t.failure_cancel_pct_downsize_safe)

    def test_billing_sql_uses_an_exact_half_open_window_that_excludes_today(self):
        for name in ("EPOCH_DAILY_COST_SQL", "BILLING_FEATURE_SQL", "EPOCH_BILLING_ATTRIBUTION_SQL"):
            sql = getattr(billing_queries, name)
            self.assertIn("usage_date>=DATE_SUB(CURRENT_DATE(),365)", sql, name)
            self.assertIn("usage_date<CURRENT_DATE()", sql, name)

    def test_billing_cost_sql_keeps_corrections_in_the_sum(self):
        # RETRACTION (negative) and RESTATEMENT rows net out original usage; filtering to ORIGINAL would overstate cost.
        # record_type may only label correction quantities, never filter rows out of a sum.
        for name in ("BILLING_FEATURE_SQL", "EPOCH_BILLING_ATTRIBUTION_SQL"):
            self.assertNotIn("record_type", getattr(billing_queries, name).lower(), name)
        sql = EPOCH_DAILY_COST_SQL
        allowed = (
            "CASE WHEN u.record_type='RETRACTION' THEN -CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS retracted_dbu_quantity,",
            "CASE WHEN u.record_type='RESTATEMENT' THEN CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS restated_dbu_quantity,",
        )
        rest = sql
        for line in allowed:
            self.assertEqual(rest.count(line), 1, line)
            rest = rest.replace(line, "")
        self.assertNotIn("record_type", rest.lower())
        self.assertIn("CAST(u.usage_quantity AS DOUBLE) AS dbu_quantity,", sql)
        self.assertIn("SUM(c.dbu_quantity) AS dbu_quantity", sql)
        # Nor may rows be dropped by their quantity (negative retractions) or by the correction columns.
        self.assertNotRegex(sql, r"usage_quantity\s*(<|>|=|!=|<>)")
        self.assertNotRegex(sql.lower(), r"(where|having|and|or)\s+\(?\s*c\.(retracted|restated)_dbu_quantity")

    def test_cost_sql_collects_retracted_and_restated_dbu(self):
        sql = EPOCH_DAILY_COST_SQL
        self.assertIn("u.record_type='RETRACTION'", sql)
        self.assertIn("u.record_type='RESTATEMENT'", sql)
        self.assertIn("SUM(c.retracted_dbu_quantity) AS retracted_dbu_quantity", sql)
        self.assertIn("SUM(c.restated_dbu_quantity) AS restated_dbu_quantity", sql)

    def test_correction_share_is_retracted_over_originally_billed_dbu(self):
        # 30 days x 100 DBU originally billed = 3,000; 09-10 retracted 60 and restated 45 (net 85 that day).
        rows = self.correction_rows(retracted={"2026-09-10": 60.0}, restated={"2026-09-10": 45.0})
        out = self.forecast(rows, watermark="2026-10-01T00:00:00+00:00")
        self.assertEqual(out["billing_retracted_dbu_quantity_365"], 60.0)
        self.assertEqual(out["billing_restated_dbu_quantity_365"], 45.0)
        self.assertEqual(out["billing_correction_dbu_share_365"], 0.02)  # 60 / 3,000

    def test_correction_share_is_zero_without_corrections(self):
        out = self.forecast(self.correction_rows(), watermark="2026-10-01T00:00:00+00:00")
        self.assertEqual(out["billing_correction_dbu_share_365"], 0.0)

    def test_correction_share_uses_the_settled_cost_window(self):
        # A retraction on a day the watermark has not passed is outside the cost basis and outside the share.
        rows = self.correction_rows(retracted={"2026-09-30": 50.0})
        out = self.forecast(rows, watermark="2026-09-30T20:00:00+00:00")
        self.assertEqual(out["billing_complete_through"], "2026-09-29")
        self.assertEqual(out["billing_retracted_dbu_quantity_365"], 0.0)
        self.assertEqual(out["billing_correction_dbu_share_365"], 0.0)

    def test_repeated_corrections_can_push_the_share_above_one(self):
        # One day: ORIGINAL 100, RETRACTION -100, RESTATEMENT 90, RETRACTION -90, RESTATEMENT 80 (net 80).
        rows = self.correction_rows(days=1, first="2026-09-29", retracted={"2026-09-29": 190.0}, restated={"2026-09-29": 170.0})
        out = self.forecast(rows, watermark="2026-10-01T00:00:00+00:00", start="2026-09-29T00:00:00+00:00")
        self.assertEqual(out["billing_correction_dbu_share_365"], 1.9)

    def test_correction_share_is_unknown_when_not_collected(self):
        rows = self.correction_rows()
        for r in rows:
            r.pop("retracted_dbu_quantity"); r.pop("restated_dbu_quantity")
        out = self.forecast(rows, watermark="2026-10-01T00:00:00+00:00")
        self.assertIsNone(out["billing_correction_dbu_share_365"])
        self.assertIsNone(out["billing_retracted_dbu_quantity_365"])

    def test_billing_watermark_sql_is_read_only_and_bounded(self):
        sql = BILLING_WATERMARK_SQL
        self.assertIn("MAX(usage_end_time)", sql)
        self.assertIn("system.billing.usage", sql)
        self.assertIn("usage_date>=DATE_SUB(CURRENT_DATE()", sql)
        self.assertNotRegex(sql.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b")

    def test_day_not_yet_covered_by_the_watermark_is_excluded_from_the_basis(self):
        rows = self._cost_rows(30, "2026-09-01")  # 09-01 .. 09-30, $100/day; 09-30 is only partly ingested
        rows[-1]["list_cost"] = 20.0
        out = self.forecast(rows, watermark="2026-09-30T20:00:00+00:00")
        self.assertEqual(out["billing_complete_through"], "2026-09-29")
        self.assertEqual(out["billing_completeness"], "COVERED_BY_WATERMARK")
        self.assertEqual(out["excluded_incomplete_days"], 1)
        self.assertEqual(out["cost_history_days"], 29)
        self.assertAlmostEqual(out["forecast_cost_365"], 100 * 365, places=2)
        self.assertAlmostEqual(out["current_cost_365"], 29 * 100, places=2)

    def test_watermark_exactly_at_midnight_completes_the_previous_day(self):
        rows = self._cost_rows(30, "2026-09-01")
        self.assertEqual(self.forecast(rows, watermark="2026-10-01T00:00:00+00:00")["billing_complete_through"], "2026-09-30")
        self.assertEqual(self.forecast(rows, watermark="2026-09-30T23:00:00+00:00")["billing_complete_through"], "2026-09-29")

    def test_watermark_never_extends_the_basis_past_yesterday(self):
        out = self.forecast(self._cost_rows(30, "2026-09-01"), watermark="2026-10-02T06:00:00+00:00")
        self.assertEqual(out["billing_complete_through"], "2026-09-30")
        self.assertEqual(out["excluded_incomplete_days"], 0)

    def test_missing_watermark_is_labeled_unverified_not_silently_complete(self):
        out = self.forecast(self._cost_rows(30, "2026-09-01"))
        self.assertEqual(out["billing_completeness"], "UNVERIFIED_ASSUMED_COMPLETE_THROUGH_PRIOR_DAY")
        self.assertIsNone(out["billing_watermark"])
        self.assertEqual(out["billing_complete_through"], "2026-09-30")
        also_none = self.forecast(self._cost_rows(30, "2026-09-01"), watermark=None)
        self.assertEqual(also_none["billing_completeness"], "UNVERIFIED_ASSUMED_COMPLETE_THROUGH_PRIOR_DAY")

    def test_actual_cost_uses_the_same_half_open_365_day_window(self):
        rows = [
            {"usage_date": "2025-09-30", "list_cost": 1000.0, "currency_code": "USD"},  # 366 days back: outside
            {"usage_date": "2025-10-01", "list_cost": 10.0, "currency_code": "USD"},   # 365 days back: inside
            {"usage_date": "2026-09-30", "list_cost": 10.0, "currency_code": "USD"},   # yesterday: inside
            {"usage_date": "2026-10-01", "list_cost": 500.0, "currency_code": "USD"},  # today (incomplete): outside
        ]
        out = self.forecast(rows, start="2025-10-01T00:00:00+00:00")
        self.assertEqual(out["current_cost_365"], 20.0)

    # --- G2: usage with no list_prices row is unavailable cost, never silently dropped ------------------------
    def test_cost_sql_keeps_unpriced_usage_and_one_price_per_usage_record(self):
        sql = EPOCH_DAILY_COST_SQL
        self.assertIn("LEFT JOIN system.billing.list_prices", sql)
        self.assertNotRegex(sql, r"\)\s*JOIN system\.billing\.list_prices|\bu\s+JOIN system\.billing\.list_prices")
        self.assertIn("unpriced_dbu_quantity", sql)
        # Overlapping price rows must not double count one usage record.
        self.assertIn("PARTITION BY u.record_id", sql)

    def test_unpriced_usage_in_forecast_basis_makes_forward_and_actual_cost_unknown(self):
        out = self.forecast(self._rows_with_unpriced(["2026-09-20", "2026-09-21"]))
        self.assertIsNone(out["forecast_cost_365"])
        self.assertIsNone(out["current_cost_365"])
        self.assertEqual(out["unpriced_dbu_quantity_365"], 300.0)
        self.assertEqual(out["unpriced_dbu_quantity_in_basis"], 300.0)
        self.assertEqual(out["dbu_pricing_status"], "UNPRICED_USAGE")
        self.assertEqual(out["currency"], "USD")  # unpriced rows carry no currency and do not count as mixed

    def test_unpriced_usage_before_current_epoch_only_nulls_the_trailing_actual(self):
        rows = self._rows_with_unpriced(["2026-08-15"])
        out = self.forecast(rows)
        self.assertIsNone(out["current_cost_365"])
        self.assertIsNotNone(out["forecast_cost_365"])
        self.assertEqual(out["unpriced_dbu_quantity_in_basis"], 0.0)
        self.assertEqual(out["dbu_pricing_status"], "UNPRICED_USAGE")

    def test_fully_priced_usage_is_complete(self):
        out = self.forecast(self._rows_with_unpriced([]))
        self.assertEqual(out["dbu_pricing_status"], "COMPLETE")
        self.assertEqual(out["unpriced_dbu_quantity_365"], 0.0)
        self.assertAlmostEqual(out["current_cost_365"], 3000.0)

    def test_collector_marks_unpriced_epoch_cost_unknown(self):
        class Client:
            def query(self_inner, statement, *, label="collector_sql"):
                if label == "config_epochs":
                    return [{"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                             "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 1, "max_clusters": 2,
                             "auto_stop_minutes": 10, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}]
                if label == "billing_daily_cost":
                    return [dict(r, workspace_id="1", warehouse_id="w", epoch_start="2026-09-01T00:00:00+00:00")
                            for r in self._rows_with_unpriced(["2026-09-20"])]
                return []
        e = extract_evidence(Client(), self.t, "us-east-1")[0]
        self.assertIsNone(e.baseline_forecast_cost_365)
        self.assertIsNone(e.current_cost_365)
        self.assertIsNone(e.current_epoch.cost["list_cost"])
        self.assertEqual(e.current_epoch.cost["unpriced_dbu_quantity"], 150.0)
        self.assertEqual(e.current_epoch.cost["dbu_quantity"], 6150.0)

    def test_unpriced_usage_in_basis_leaves_trend_and_stability_unknown(self):
        # A run of unpriced days is not a cost drop: trend, run rates and CV must not read it as $0 days.
        rows = [{"usage_date": f"2026-{m:02d}-{d:02d}", "list_cost": 100.0, "dbu_quantity": 200.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
                for m, days in ((5, 31), (6, 30), (7, 31), (8, 31)) for d in range(1, days + 1)]
        rows += [{"usage_date": f"2026-09-{d:02d}", "list_cost": None, "dbu_quantity": 200.0, "unpriced_dbu_quantity": 200.0, "currency_code": None}
                 for d in range(1, 31)]
        out = self.forecast(rows, start="2026-05-01T00:00:00+00:00")
        self.assertIsNone(out["forecast_cost_365"])
        self.assertIsNone(out["trend_ratio"])
        self.assertEqual(out["trend_adjustment_factor"], 1.0)
        self.assertIsNone(out["run_rate_28"])
        self.assertIsNone(out["run_rate_84"])
        self.assertIsNone(out["cost_cv_84"])

    def test_cost_sql_treats_a_null_list_price_as_unpriced_and_breaks_price_ties(self):
        sql = EPOCH_DAILY_COST_SQL
        self.assertIn("p.pricing.effective_list.default IS NULL", sql)
        self.assertIn("ORDER BY p.price_start_time DESC NULLS LAST, p.currency_code", sql)

    def test_evidence_with_unpriced_rows_merges_with_itself(self):
        payload = load_case("c32-unpriced-sku")
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            envelope = {"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": PUBLISHER_VERSION, "warehouses": payload["evidence"]}
            for path in (a, b):
                path.write_text(json.dumps(envelope))
            merged = load_evidence_files([str(a), str(b)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].daily_cost_365), len(payload["evidence"][0]["daily_cost_365"]))

    def test_unknown_cost_survives_evidence_round_trip(self):
        e = copy.deepcopy(self.evidence[0])
        e.current_cost_365 = None
        e.baseline_forecast_cost_365 = None
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "e.json"
            save_evidence(path, [e])
            back = load_evidence_files([str(path)])[0]
        self.assertIsNone(back.current_cost_365)
        self.assertIsNone(back.baseline_forecast_cost_365)

    # --- Collector determinism: tie-broken window ordering and UTC session dates ---------------------------------
    def test_gap_sql_orders_the_busy_window_with_a_unique_tie_breaker(self):
        sql = build_epoch_gap_metrics_sql(self.t)
        self.assertIn("ORDER BY x.start_time, x.end_time, x.statement_id", sql)

    def test_event_sql_orders_tied_events_deterministically(self):
        self.assertIn("ORDER BY event_time, event_type, cluster_count", EPOCH_EVENT_METRICS_SQL)

    def test_sql_session_is_pinned_and_verified_to_utc(self):
        calls, modules = self._fake_connector("Etc/UTC")
        with patch.dict(sys.modules, modules):
            with SqlClient(self._auth()):
                pass
        self.assertEqual(calls["connect"]["session_configuration"], {"TIMEZONE": "UTC"})
        self.assertIn("current_timezone()", calls["statements"][0])

    def test_sql_session_not_in_utc_fails_closed(self):
        calls, modules = self._fake_connector("America/Los_Angeles")
        with patch.dict(sys.modules, modules):
            with self.assertRaises(PublisherError):
                with SqlClient(self._auth()):
                    pass
        self.assertTrue(calls.get("closed"))

    # --- An unknown DBU baseline is never published as $0 -------------------------------------------------------
    def test_too_few_active_cost_days_leave_the_forecast_unknown(self):
        # Three days apart: frequent enough for the run-rate model, which needs more billed days (weekly billing would
        # be forecast from its cycles instead).
        rows = [{"usage_date": d, "list_cost": 200.0, "currency_code": "USD"} for d in ("2026-09-17", "2026-09-20", "2026-09-23", "2026-09-26")]
        out = self.forecast(rows)
        self.assertEqual(out["cost_history_days"], 30)
        self.assertEqual(out["active_cost_days"], 4)
        self.assertIsNone(out["forecast_cost_365"])
        self.assertEqual(out["dbu_forecast_status"], "INSUFFICIENT_ACTIVE_COST_DAYS")

    def test_no_cost_rows_give_an_unknown_forecast_not_zero(self):
        out = self.forecast([], start="2026-09-25T00:00:00+00:00")
        self.assertIsNone(out["forecast_cost_365"])
        self.assertEqual(out["dbu_forecast_status"], "INSUFFICIENT_ACTIVE_COST_DAYS")
        out = self.forecast([], start="2026-10-01T00:00:00+00:00")  # epoch starts today: empty basis
        self.assertIsNone(out["forecast_cost_365"])

    def test_non_positive_run_rate_is_unknown_not_zero(self):
        rows = self._cost_rows(30, cost=lambda i: 10.0)
        rows.append({"usage_date": "2026-09-30", "list_cost": -5000.0, "currency_code": "USD"})  # large net correction
        out = self.forecast(rows)
        self.assertIsNone(out["forecast_cost_365"])
        self.assertEqual(out["dbu_forecast_status"], "NON_POSITIVE_RUN_RATE")

    def test_regular_history_forecasts_normally(self):
        out = self.forecast(self._cost_rows(30))
        self.assertEqual(out["dbu_forecast_status"], "COMPLETE")
        self.assertAlmostEqual(out["forecast_cost_365"], 36500.0, places=2)

    def test_mature_epoch_with_few_billed_days_names_billed_days_not_the_time_gate(self):
        # COST_HISTORY_INSUFFICIENT is the calendar gate that the re-evaluation window promises will clear; a mature
        # epoch billed on too few days is a different, not time-boxed condition.
        e = self._sparse(30)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual(by["warehouse_type"].blockers, ["BILLED_COST_DAYS_INSUFFICIENT"])
        # An approval-only cost change waits for its own price: held with the same code.
        self.assertEqual(by["auto_stop_minutes"].status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("BILLED_COST_DAYS_INSUFFICIENT", by["auto_stop_minutes"].blockers)
        self.assertEqual(by["auto_stop_minutes"].metadata["value_gate"]["reason_code"], "BILLED_COST_DAYS_INSUFFICIENT")
        pkg = p.build_recommendation_packages([e], self.t)[0].to_dict()
        methods = " ".join(sc["portfolio_economics"].get("method") or "" for sc in pkg["scenarios"])
        self.assertNotIn("no list price", methods)
        self.assertIn("billed days", methods)

    def test_blocker_follows_the_configured_cost_history_floor(self):
        import dataclasses
        t14 = dataclasses.replace(self.t, min_economics_cost_history_days=14)
        self.assertEqual(dbu_baseline_blocker(self._sparse(10), t14), "COST_HISTORY_INSUFFICIENT")
        self.assertEqual(dbu_baseline_blocker(self._sparse(10), self.t), "BILLED_COST_DAYS_INSUFFICIENT")

    def test_young_epoch_keeps_the_time_gate_code(self):
        by = {c.knob: c for c in p.evaluate_warehouses([self._sparse(3)], self.t)[0].candidates}
        self.assertEqual(by["warehouse_type"].blockers, ["COST_HISTORY_INSUFFICIENT"])

    def test_a_correction_only_day_is_not_a_billed_day(self):
        rows = [{"usage_date": f"2026-09-{d:02d}", "list_cost": 100.0, "currency_code": "USD"} for d in range(1, 7)]
        rows.append({"usage_date": "2026-09-10", "list_cost": -1.0, "currency_code": "USD"})
        out = self.forecast(rows)
        self.assertEqual(out["dbu_forecast_status"], "INSUFFICIENT_ACTIVE_COST_DAYS")
        self.assertIsNone(out["forecast_cost_365"])

    def test_short_epochs_forecast_without_day_of_week_bias(self):
        # Weekday-only $100/day: the true annual run rate is 100 x 5/7 x 365 = 26,071.43 whatever the epoch length.
        model = Modeler(self.unsettled())
        for days in (14, 15, 20, 30, 40, 90):
            with self.subTest(days=days):
                start = (dt.date(2026, 10, 1) - dt.timedelta(days=days)).isoformat()
                out = model.forecast_from_daily_cost(self._weekday_only(start, days), start + "T00:00:00+00:00", self.ANALYSIS)
                self.assertAlmostEqual(out["forecast_cost_365"], 26071.43, places=2)
                self.assertEqual(out["rate_basis_days"] % 7, 0)
                self.assertLessEqual(out["rate_basis_days"], 84)

    def test_billed_day_gate_counts_the_days_the_rate_uses(self):
        model = Modeler(self.unsettled())
        # Weekday-only, 9-12 days: the last whole week holds 5 billed days, below the 7-day floor: unknown, not guessed.
        for days in (9, 12):
            with self.subTest(days=days):
                start = (dt.date(2026, 10, 1) - dt.timedelta(days=days)).isoformat()
                out = model.forecast_from_daily_cost(self._weekday_only(start, days), start + "T00:00:00+00:00", self.ANALYSIS)
                self.assertIsNone(out["forecast_cost_365"])
                self.assertEqual(out["dbu_forecast_status"], "INSUFFICIENT_ACTIVE_COST_DAYS")
        # 13 days billed on days 1-6 and 13: 7 billed days overall, but the rate's week holds only one.
        start = "2026-09-18"
        rows = [{"usage_date": (dt.date(2026, 9, 18) + dt.timedelta(days=i)).isoformat(), "list_cost": 100.0 if (i < 6 or i == 12) else 0.0,
                 "currency_code": "USD"} for i in range(13)]
        out = model.forecast_from_daily_cost(rows, start + "T00:00:00+00:00", self.ANALYSIS)
        self.assertIsNone(out["forecast_cost_365"])

    def test_resource_blend_uses_whole_weeks_too(self):
        values = [100.0 if i % 7 < 5 else 0.0 for i in range(10)]
        self.assertAlmostEqual(blended_daily_rate(values, self.t) * 365, 26071.43, places=2)

    def test_a_day_counts_only_after_the_settle_buffer(self):
        out = Modeler(self.t).forecast_from_daily_cost(self._daily(), "2026-09-01T00:00:00+00:00", "2026-10-01T12:00:00+00:00")
        self.assertEqual(out["billing_complete_through"], "2026-09-29")  # 09-30 ends 10-01 00:00; settles 10-02 00:00
        self.assertEqual(out["billing_settle_hours"], 24)
        out = Modeler(self.t).forecast_from_daily_cost(self._daily(), "2026-09-01T00:00:00+00:00", "2026-10-02T00:30:00+00:00")
        self.assertEqual(out["billing_complete_through"], "2026-09-30")

    def test_watermark_still_limits_settled_days(self):
        out = Modeler(self.t).forecast_from_daily_cost(self._daily(), "2026-09-01T00:00:00+00:00", "2026-10-03T12:00:00+00:00",
                                                         billing_watermark="2026-09-28T06:00:00+00:00")
        self.assertEqual(out["billing_complete_through"], "2026-09-27")

    def test_billing_completeness_is_judged_per_workspace(self):
        seen = {}
        ev = {e.workspace_id: e for e in extract_evidence(self._two_workspace_client(seen), self.t, "us-east-1",
                                                            analysis_time="2026-10-03T12:00:00+00:00")}
        self.assertEqual(ev["ws-fresh"].forecast_metadata["billing_complete_through"], "2026-10-01")
        self.assertEqual(ev["ws-lagging"].forecast_metadata["billing_complete_through"], "2026-09-24")
        self.assertEqual(ev["ws-lagging"].forecast_metadata["billing_ingested_through"], "2026-09-25")
        self.assertIn("GROUP BY workspace_id", seen["billing_watermark"])
        self.assertIn("MAX(ingestion_date)", seen["billing_watermark"])

    def test_an_idle_workspace_is_not_mistaken_for_lagging_billing(self):
        # The warehouse stopped running after 09-20, so its later $0 days are complete, not "not yet ingested".
        quiet = self._idle_workspace_evidence(running_after_last_usage=False)
        self.assertEqual(quiet.forecast_metadata["billing_complete_through"], "2026-10-02")

    def test_a_running_warehouse_without_billing_is_lagging(self):
        lagging = self._idle_workspace_evidence(running_after_last_usage=True)
        self.assertEqual(lagging.forecast_metadata["billing_complete_through"], "2026-09-19")

    def test_running_on_the_watermark_day_itself_is_lag(self):
        import datetime as dt
        # Midnight watermark, any running that day; 10:00 watermark, running until 23:00 (780 minutes > 600 before it).
        for wm, minutes in (("2026-09-28T00:00:00+00:00", 60.0), ("2026-09-28T10:00:00+00:00", 780.0)):
            with self.subTest(watermark=wm):
                _, source = effective_billing_watermark(wm, "2026-10-04T08:00:00+00:00", [{"usage_day": "2026-09-28", "uptime_minutes": minutes}],
                                                          check_through=dt.date(2026, 10, 2))
                self.assertEqual(source, "WORKSPACE_LAGGING")
        # 18:00 watermark with one hour of running that day: it may all precede the watermark, so it is not lag.
        _, source = effective_billing_watermark("2026-09-28T18:00:00+00:00", "2026-10-04T08:00:00+00:00",
                                                  [{"usage_day": "2026-09-28", "uptime_minutes": 60.0}], check_through=dt.date(2026, 10, 2))
        self.assertEqual(source, "ACCOUNT_IDLE_WAREHOUSE")

    def test_running_on_days_not_yet_billable_does_not_count_as_lag(self):
        import datetime as dt
        # Idle since 09-20; the only run is on 10-03, which the settle buffer has not released yet.
        wm, source = effective_billing_watermark("2026-09-20T18:00:00+00:00", "2026-10-04T08:00:00+00:00", self._ran("2026-10-03"),
                                                   check_through=dt.date(2026, 10, 2))
        self.assertEqual(source, "ACCOUNT_IDLE_WAREHOUSE")
        self.assertEqual(wm, "2026-10-04T08:00:00+00:00")

    def test_check_window_ends_at_the_earliest_billable_bound(self):
        import datetime as dt
        bound = billing_check_through("2026-10-14T12:00:00+00:00", "2026-10-14T08:00:00+00:00", ingestion_cutoff="2026-10-04", settle_hours=24)
        self.assertEqual(bound, dt.date(2026, 10, 2))  # same as the original 10-04 run: settle applied at the cut-off day
        self.assertEqual(billing_check_through("2026-10-04T12:00:00+00:00", "2026-10-04T08:00:00+00:00", None, 24), dt.date(2026, 10, 2))

    def test_pinned_recollection_reproduces_the_original_basis_end(self):
        rows = self._daily("2026-10-03")
        original = Modeler(self.t).forecast_from_daily_cost(rows, "2026-09-01T00:00:00+00:00", "2026-10-04T23:00:00+00:00")
        pinned = Modeler(self.t).forecast_from_daily_cost(rows, "2026-09-01T00:00:00+00:00", "2026-10-14T12:00:00+00:00", ingestion_cutoff="2026-10-04")
        self.assertEqual(original["billing_complete_through"], "2026-10-02")
        self.assertEqual(pinned["billing_complete_through"], "2026-10-02")

    def test_ingestion_cutoff_pins_every_billing_query(self):
        billing = ("billing_daily_cost", "billing_watermark", "billing_features", "billing_attribution_photon_owner")
        seen = {}
        extract_evidence(self._two_workspace_client(seen), self.t, "us-east-1", ingestion_cutoff="2026-09-30")
        for label in billing:
            self.assertIn("ingestion_date <= DATE'2026-09-30'", seen[label], label)
        seen = {}
        extract_evidence(self._two_workspace_client(seen), self.t, "us-east-1")
        for label in billing:
            self.assertIn("CAST(NULL AS DATE)", seen[label], label)
        with self.assertRaises(PublisherError):
            extract_evidence(self._two_workspace_client({}), self.t, "us-east-1", ingestion_cutoff="30/09/2026")

    def test_collector_passes_the_billing_watermark_to_the_forecast(self):
        seen = {}
        real = Modeler.forecast_from_daily_cost
        def spy(model, rows, start, analysis_time, **kw):
            seen.update(kw)
            return real(model, rows, start, analysis_time, **kw)
        class Client:
            def query(self_inner, statement, *, label="collector_sql"):
                if label == "billing_watermark":
                    return [{"workspace_id": "1", "billing_watermark": "2026-09-30T20:00:00+00:00"}]
                if label == "config_epochs":
                    return [{"workspace_id": "1", "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                             "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 1, "max_clusters": 2,
                             "auto_stop_minutes": 10, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}]
                return []
        with patch.object(Modeler, "forecast_from_daily_cost", spy):
            extract_evidence(Client(), self.t, "us-east-1")
        self.assertEqual(parse_datetime(seen["billing_watermark"]).isoformat(), "2026-09-30T20:00:00+00:00")

    def test_watermark_sql_looks_back_far_enough_to_catch_a_stalled_feed_and_is_sql_scoped(self):
        sql = BILLING_WATERMARK_SQL
        self.assertIn("usage_date>=DATE_SUB(CURRENT_DATE(),30)", sql)
        self.assertIn("billing_origin_product='SQL'", sql)

    def test_demo_cv_is_unknown_for_a_warehouse_with_too_little_history(self):
        for e in synthetic_demo_evidence():
            if e.cost_history_days < self.t.forecast_cv_min_days:
                self.assertIsNone(e.cost_cv_84, e.warehouse_name)

    def test_gap_collector_handles_overlapping_queries_in_milliseconds(self):
        sql = build_epoch_gap_metrics_sql(self.t)
        self.assertIn("MAX(x.end_time) OVER", sql)
        self.assertNotIn("TIMESTAMPDIFF(SECOND", sql)
        self.assertNotIn("saved_idle", sql)  # auto-stop savings come from the running-idle collector

    def test_profile_host_and_cli_oauth_flow(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[WHOPT]\nhost = https://dbc.example.cloud.databricks.com\n")
            self.assertEqual(_load_profile_host("WHOPT", str(cfg)), "dbc.example.cloud.databricks.com")
            class R:
                stdout = '{"access_token":"short-lived","expiry":"2026-10-01T20:00:00Z"}'
            with patch("publisher.analyzer.collector.workspace_api.subprocess.run", return_value=R()) as run:
                auth = resolve_cli_oauth("WHOPT", "warehouse", "abc123", str(cfg))
                self.assertEqual(auth.http_path, "/sql/1.0/warehouses/abc123")
                self.assertEqual(auth.access_token, "short-lived")
                run.assert_called_once()
                call = run.call_args
                self.assertEqual(call.kwargs["env"]["DATABRICKS_CONFIG_FILE"], str(cfg))
                self.assertNotIn("DATABRICKS_TOKEN", call.kwargs["env"])

    def test_all_purpose_cluster_execution_path_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[WHOPT]\nhost = https://dbc.example.cloud.databricks.com\n")
            class R:
                stdout = '{"access_token":"short-lived","expiry":"2026-10-01T20:00:00Z"}'
            with patch("publisher.analyzer.collector.workspace_api.subprocess.run", return_value=R()), patch("publisher.analyzer.collector.workspace_api._validate_all_purpose_cluster", return_value={"cluster_source":"UI"}), patch("publisher.analyzer.collector.workspace_api._current_workspace_id", return_value="123456789"):
                auth = resolve_cli_oauth("WHOPT", "cluster", "1234-567890-demo123", str(cfg))
                self.assertEqual(auth.http_path, "sql/protocolv1/o/123456789/1234-567890-demo123")
                self.assertEqual(auth.execution_kind, "cluster")

    def test_the_legacy_positional_form_is_refused(self):
        # (profile, warehouse_id, config_file) was a pre-1.0 form: the second argument is now always the execution kind.
        with self.assertRaises(PublisherError) as raised:
            resolve_cli_oauth("WHOPT", "abc123", "/nonexistent/.databrickscfg")
        self.assertIn("Unsupported execution kind", str(raised.exception))

    def test_pat_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[PAT]\nhost = https://dbc.example.cloud.databricks.com\ntoken = secret\n")
            with self.assertRaises(PublisherError):
                resolve_cli_oauth("PAT", "warehouse", "abc123", str(cfg))

    def test_photon_inference_uses_coverage_freshness_and_signed_dbu(self):
        row={"photon_true_dbu":950.0,"photon_false_dbu":50.0,"photon_null_dbu":0.0,"latest_usage_time":"2026-09-30T12:00:00+00:00"}
        obs=infer_photon_observation(row,"2026-10-01T12:00:00+00:00",self.t)
        self.assertEqual(obs.status,"ENABLED")
        self.assertEqual(obs.enabled_usage_pct,95.0)
        row["photon_null_dbu"]=500.0
        obs=infer_photon_observation(row,"2026-10-01T12:00:00+00:00",self.t)
        self.assertEqual(obs.status,"UNKNOWN")

    def test_owner_query_source_and_scenario_confidence_are_reported(self):
        self.assertEqual(self.w.evidence.attribution.owner,"sales-bi@databricks.example")
        self.assertEqual(self.w.query_source_profile["primary_category"],"DASHBOARD")
        self.assertEqual(self.w.query_source_profile["categories"]["POWER_BI"],4200)
        for sc in self.w.scenarios:
            self.assertIn("score",sc.confidence)
            for st in sc.steps:
                self.assertEqual(st.confidence["score"],self.by_knob[st.knob].confidence["score"])
