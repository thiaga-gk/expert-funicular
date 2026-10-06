import copy
import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p


class PublisherV21Tests(unittest.TestCase):
    def setUp(self):
        self.t = p.Thresholds()
        self.evidence = p.demo_evidence()
        self.report = p.build_report(self.evidence, self.t)
        self.w = self.report.warehouses[0]
        self.by_knob = {c.knob: c for c in self.w.candidates}

    def test_six_system_table_candidates(self):
        self.assertEqual(set(self.by_knob), set(p.ACTIVE_KNOBS))
        self.assertEqual(len(self.w.candidates), 6)

    def test_expected_demo_decisions(self):
        self.assertEqual(self.by_knob["warehouse_size"].status, "TEST_REQUIRED")
        self.assertEqual(self.by_knob["warehouse_size"].recommended_config, {"warehouse_size": "MEDIUM"})
        self.assertEqual(self.by_knob["max_clusters"].recommended_config, {"max_clusters": 3})
        self.assertEqual(self.by_knob["min_clusters"].recommended_config, {"min_clusters": 1})
        self.assertEqual(self.by_knob["auto_stop_minutes"].recommended_config, {"auto_stop_minutes": 30})
        self.assertEqual(self.by_knob["warehouse_type"].recommended_config, {"warehouse_type": "SERVERLESS"})

    def test_recommendation_ids_are_deterministic_and_include_current_config(self):
        r2 = p.build_report(p.demo_evidence(), self.t)
        self.assertEqual([c.recommendation_id for c in self.w.candidates], [c.recommendation_id for c in r2.warehouses[0].candidates])
        old = self.by_knob["warehouse_size"].recommendation_id
        changed = p.demo_evidence()
        changed[0].current_epoch.config = p.WarehouseConfig("PRO", "CURRENT", "X_LARGE", 2, 4, 120)
        new = next(c for c in p.build_report(changed, self.t).warehouses[0].candidates if c.knob == "warehouse_size").recommendation_id
        self.assertNotEqual(old, new)

    def test_analysis_time_makes_decision_semantics_repeatable(self):
        first = p.build_report(p.demo_evidence(), self.t).warehouses[0]
        second = p.build_report(p.demo_evidence(), self.t).warehouses[0]
        self.assertEqual(p.canonical_json(first.candidates), p.canonical_json(second.candidates))
        self.assertEqual(p.canonical_json(first.scenarios), p.canonical_json(second.scenarios))

    def test_current_epoch_drives_findings_and_history_calibrates_estimator(self):
        current = self.evidence[0].current_epoch_id
        for c in self.w.candidates:
            for f in c.findings:
                if f.metric != "serverless_usage_pct_365":
                    self.assertEqual(f.epoch_id, current)
        size = self.by_knob["warehouse_size"]
        calib = size.metadata.get("historical_epoch_calibration")
        self.assertIsNotNone(calib)
        self.assertNotEqual(calib["epoch_id"], current)

    def test_candidates_are_independent_and_portfolio_is_not_sum(self):
        standalone = sum((c.estimated_savings_365 or 0) for c in self.w.candidates)
        chosen = next(s for s in self.w.scenarios if s.recommended)
        portfolio = chosen.portfolio_estimate.estimated_savings_range_365
        self.assertIsNotNone(portfolio)
        self.assertNotAlmostEqual(standalone, portfolio.base, places=2)
        self.assertTrue(chosen.portfolio_estimate.non_additive)
        self.assertTrue(all(c.metadata["candidate_savings_must_not_be_summed"] for c in self.w.candidates))

    def test_stateful_bundle_reduces_overlap_between_min_and_autostop(self):
        chosen = next(s for s in self.w.scenarios if s.recommended)
        portfolio = chosen.portfolio_estimate.estimated_savings_range_365
        independent_high = sum(
            self.by_knob[k].estimated_savings_range_365.high
            for k in ("warehouse_size", "min_clusters", "auto_stop_minutes")
            if self.by_knob[k].estimated_savings_range_365
        )
        self.assertLess(portfolio.high, independent_high)
        self.assertLessEqual(portfolio.high, self.w.baseline_forecast_cost_365)
        self.assertIn("cluster-minutes", chosen.portfolio_estimate.method)

    def test_scenario_contract_and_order(self):
        self.assertEqual([s.name for s in self.w.scenarios], [
            "Plan A — Optimize current warehouse type", "Plan B — Serverless migration experiment"
        ])
        plan_a = next(s for s in self.w.scenarios if s.name.startswith("Plan A"))
        plan_b = next(s for s in self.w.scenarios if s.name.startswith("Plan B"))
        self.assertTrue(plan_a.recommended)
        self.assertEqual([x.knob for x in plan_a.steps], ["warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes"])
        self.assertEqual(plan_b.portfolio_estimate.estimate_status, "REQUIRES_CANARY")
        self.assertEqual([x.knob for x in self.w.recommended_execution_plan], [x.knob for x in plan_a.steps])

    def test_portfolio_range_ordering_and_plan_running_ranges(self):
        plan = next(s for s in self.w.scenarios if s.recommended)
        r = plan.portfolio_estimate.estimated_savings_range_365
        self.assertLessEqual(r.low, r.base)
        self.assertLessEqual(r.base, r.high)
        authoritative = [s for s in plan.steps if s.execution_state != "PENDING_REEVALUATION"]
        contingent = [s for s in plan.steps if s.execution_state == "PENDING_REEVALUATION"]
        self.assertTrue(all(s.running_estimated_savings_range_365 is not None for s in authoritative))
        self.assertTrue(all(s.running_estimated_savings_range_365 is None for s in contingent))


    def test_pending_reevaluation_steps_are_not_priced_into_authoritative_plan_a(self):
        plan = next(s for s in self.w.scenarios if s.recommended)
        pending = [s for s in plan.steps if s.execution_state == "PENDING_REEVALUATION"]
        self.assertTrue(pending)
        self.assertTrue(all(s.running_estimated_savings_range_365 is None for s in pending))
        # The scenario estimate must equal the running estimate after the last
        # authoritative prefix step rather than pricing stale downstream knobs.
        prefix = [s for s in plan.steps if s.execution_state != "PENDING_REEVALUATION"]
        self.assertTrue(prefix)
        last = prefix[-1].running_estimated_savings_range_365
        scenario = plan.portfolio_estimate.estimated_savings_range_365
        self.assertEqual(p.canonical_json(last), p.canonical_json(scenario))

    def test_reporter_json_workbook_and_hitl_findings(self):
        with tempfile.TemporaryDirectory() as td:
            files = p.package(Path(td), self.report, self.evidence)
            payload = json.loads(Path(files["report_json"]).read_text())
            wh = payload["warehouses"][0]
            for field in ("candidates", "scenarios", "recommended_execution_plan", "forecast_metadata", "epochs"):
                self.assertIn(field, wh)
            with open(files["findings_csv"], encoding="utf-8") as fh:
                findings = list(csv.DictReader(fh))
            self.assertGreater(len(findings), 0)
            self.assertTrue(all(x["recommendation_id"] and x["interpretation"] and x["source_table"] for x in findings))
            with zipfile.ZipFile(files["report_workbook"]) as z:
                workbook = z.read("xl/workbook.xml").decode()
                for name in ["Portfolio Summary", "Candidates", "Execution Plan", "Recommended Plan", "Scenarios", "Evidence Findings", "Config Epochs", "Forecast Model"]:
                    self.assertIn(f'name="{name}"', workbook)
                self.assertIn('name="Source &amp; Policy"', workbook)

    def test_no_rest_api_and_api_required_capabilities_are_explicit(self):
        self.assertFalse(self.report.source_policy["rest_api_calls"])
        self.assertEqual(set(self.report.api_required_capabilities), {"enable_photon", "spot_instance_policy", "statement_timeout"})
        self.assertTrue(all(not v["api_used"] for v in self.report.api_required_capabilities.values()))
        source = Path(p.__file__).read_text()
        self.assertNotIn("/api/2.0/sql/warehouses", source)
        self.assertNotIn("urllib.request", source)

    def test_query_source_tag_cache_evidence_exposed(self):
        metrics = {f.metric for c in self.w.candidates for f in c.findings}
        for metric in ("avg_read_io_cache_percent_30", "tagged_query_pct_30", "dashboard_queries_30", "job_queries_30"):
            self.assertIn(metric, metrics)

    def test_identical_evidence_merge_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            p.save_evidence(a, self.evidence)
            p.save_evidence(b, self.evidence)
            merged = p.load_evidence_files([str(a), str(b)])
            self.assertEqual(len(merged), 1)
            self.assertEqual(len(merged[0].epochs), 2)

    def test_same_warehouse_id_in_different_workspaces_is_not_collapsed(self):
        other = copy.deepcopy(self.evidence[0])
        other.workspace_id = "9999999999999999"
        other.current_epoch_id = p.epoch_id(other.workspace_id, other.warehouse_id, other.current_epoch.epoch_start)
        for ep in other.epochs:
            ep.workspace_id = other.workspace_id
            ep.epoch_id = p.epoch_id(other.workspace_id, other.warehouse_id, ep.epoch_start)
            if ep.is_current:
                other.current_epoch_id = ep.epoch_id
        for row in other.daily_cost_365:
            row["workspace_id"] = other.workspace_id
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            p.save_evidence(a, self.evidence)
            p.save_evidence(b, [other])
            merged = p.load_evidence_files([str(a), str(b)])
            self.assertEqual(len(merged), 2)

    def test_mixed_currency_rejected(self):
        model = p.Modeler(self.t)
        rows = [
            {"usage_date": "2026-01-01", "list_cost": 1, "currency_code": "USD"},
            {"usage_date": "2026-01-02", "list_cost": 1, "currency_code": "EUR"},
        ]
        with self.assertRaises(ValueError):
            model.forecast_from_daily_cost(rows, "2025-01-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")

    def test_forecast_zero_fills_calendar_days(self):
        model = p.Modeler(self.t)
        rows = [
            {"usage_date": "2026-09-28", "list_cost": 100, "currency_code": "USD"},
            {"usage_date": "2026-09-30", "list_cost": 100, "currency_code": "USD"},
        ]
        out = model.forecast_from_daily_cost(rows, "2026-09-27T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        self.assertEqual(out["cost_history_days"], 4)
        self.assertEqual(out["active_cost_days"], 2)
        self.assertEqual(out["zero_cost_days_in_basis"], 2)
        self.assertAlmostEqual(out["forecast_cost_365"], 50 * 365, places=2)

    def test_gap_collector_handles_overlapping_queries_and_policy_targets(self):
        sql = p.build_epoch_gap_metrics_sql(self.t)
        self.assertIn("MAX(x.end_time) OVER", sql)
        for target in {self.t.serverless_auto_stop_target_minutes, self.t.low_activity_auto_stop_target_minutes, self.t.medium_activity_auto_stop_target_minutes, self.t.high_activity_auto_stop_target_minutes}:
            self.assertIn(f"saved_idle_minutes_if_{target}", sql)

    def test_profile_host_and_cli_oauth_flow(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[WHOPT]\nhost = https://dbc.example.cloud.databricks.com\n")
            self.assertEqual(p._load_profile_host("WHOPT", str(cfg)), "dbc.example.cloud.databricks.com")
            class R:
                stdout = '{"access_token":"short-lived","expiry":"2026-10-01T20:00:00Z"}'
            with patch("publisher.subprocess.run", return_value=R()) as run:
                auth = p.resolve_cli_oauth("WHOPT", "abc123", str(cfg))
                self.assertEqual(auth.http_path, "/sql/1.0/warehouses/abc123")
                self.assertEqual(auth.access_token, "short-lived")
                run.assert_called_once()
                call = run.call_args
                self.assertEqual(call.kwargs["env"]["DATABRICKS_CONFIG_FILE"], str(cfg))
                self.assertNotIn("DATABRICKS_TOKEN", call.kwargs["env"])

    def test_pat_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[PAT]\nhost = https://dbc.example.cloud.databricks.com\ntoken = secret\n")
            with self.assertRaises(SystemExit):
                p.resolve_cli_oauth("PAT", "abc123", str(cfg))

    def test_same_evidence_produces_identical_report_and_workbook_bytes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"; b = Path(td) / "b"
            ra = p.build_report(p.demo_evidence(), self.t)
            rb = p.build_report(p.demo_evidence(), self.t)
            fa = p.package(a, ra, p.demo_evidence())
            fb = p.package(b, rb, p.demo_evidence())
            for key in ("report_json", "report_workbook", "report_markdown", "candidates_csv", "scenarios_csv", "execution_plan_csv", "findings_csv", "config_epochs_csv", "manifest"):
                ha = hashlib.sha256(Path(fa[key]).read_bytes()).hexdigest()
                hb = hashlib.sha256(Path(fb[key]).read_bytes()).hexdigest()
                self.assertEqual(ha, hb, key)

    def test_auto_stop_blocks_without_gap_or_event_evidence(self):
        ev = p.demo_evidence()
        ev[0].current_epoch.gaps = {}
        ev[0].current_epoch.events["cluster_minutes"] = 0
        report = p.build_report(ev, self.t)
        auto = next(c for c in report.warehouses[0].candidates if c.knob == "auto_stop_minutes")
        self.assertEqual(auto.status, "BLOCKED")
        self.assertIn("INSUFFICIENT_IDLE_GAP_SAMPLE", auto.blockers)
        self.assertIn("INSUFFICIENT_EVENT_RESOURCE_EVIDENCE", auto.blockers)

    def test_manifest_carries_versions_thresholds_provenance_and_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            files = p.package(Path(td), self.report, self.evidence)
            manifest = json.loads(Path(files["manifest"]).read_text())
            for field in ("model_versions", "thresholds", "provenance", "warnings", "counts", "semantics"):
                self.assertIn(field, manifest)
            self.assertEqual(manifest["policy_version"], p.POLICY_VERSION)
            self.assertFalse(manifest["semantics"]["rest_api_calls"])

    def test_reviewer_rejects_cross_workspace_candidate(self):
        candidates = list(self.w.candidates)
        bad = copy.deepcopy(candidates[0])
        bad.workspace_id = "wrong"
        candidates[0] = bad
        with self.assertRaises(ValueError):
            p.Reviewer(self.t).review(self.evidence[0], candidates)


if __name__ == "__main__":
    unittest.main()
