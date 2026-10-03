import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p


class PublisherV27Tests(unittest.TestCase):
    def setUp(self):
        self.t = p.Thresholds()
        self.evidence = p.demo_evidence()
        self.results = p.evaluate_warehouses(self.evidence, self.t)
        self.w = self.results[0]
        self.by_knob = {c.knob: c for c in self.w.candidates}
        self.packages = p.build_recommendation_packages(self.evidence, self.t)

    def test_seven_deterministic_candidates(self):
        self.assertEqual(set(self.by_knob), set(p.ACTIVE_KNOBS))
        self.assertEqual(len(self.w.candidates), 7)

    def test_expected_demo_decisions(self):
        self.assertEqual(self.by_knob["enable_photon"].status, "NO_CHANGE")
        self.assertEqual(self.by_knob["enable_photon"].current_config, {"enable_photon": True})
        self.assertEqual(self.by_knob["warehouse_size"].status, "TEST_REQUIRED")
        self.assertEqual(self.by_knob["warehouse_size"].recommended_config, {"warehouse_size": "MEDIUM"})
        self.assertEqual(self.by_knob["max_clusters"].recommended_config, {"max_clusters": 3})
        self.assertEqual(self.by_knob["min_clusters"].recommended_config, {"min_clusters": 1})
        self.assertEqual(self.by_knob["auto_stop_minutes"].recommended_config, {"auto_stop_minutes": 30})
        self.assertEqual(self.by_knob["warehouse_type"].recommended_config, {"warehouse_type": "SERVERLESS"})

    def test_recommendation_ids_are_deterministic_and_include_current_config(self):
        r2 = p.evaluate_warehouses(p.demo_evidence(), self.t)
        self.assertEqual([c.recommendation_id for c in self.w.candidates], [c.recommendation_id for c in r2[0].candidates])
        old = self.by_knob["warehouse_size"].recommendation_id
        changed = p.demo_evidence()
        changed[0].current_epoch.config = p.WarehouseConfig("PRO", "CURRENT", "X_LARGE", 2, 4, 120)
        new = next(c for c in p.evaluate_warehouses(changed, self.t)[0].candidates if c.knob == "warehouse_size").recommendation_id
        self.assertNotEqual(old, new)

    def test_analysis_time_makes_decision_semantics_repeatable(self):
        first = p.evaluate_warehouses(p.demo_evidence(), self.t)[0]
        second = p.evaluate_warehouses(p.demo_evidence(), self.t)[0]
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
        self.assertLessEqual(portfolio.high, self.w.total_365d_baseline_cost)
        self.assertIn("cluster-minutes", chosen.portfolio_estimate.method)

    def test_scenario_contract_and_order(self):
        self.assertEqual([s.name for s in self.w.scenarios], [
            "Plan A — Optimize current warehouse type", "Plan B — Serverless migration experiment"
        ])
        plan_a = next(s for s in self.w.scenarios if s.name.startswith("Plan A"))
        plan_b = next(s for s in self.w.scenarios if s.name.startswith("Plan B"))
        self.assertTrue(plan_a.recommended)
        self.assertEqual([x.knob for x in plan_a.steps], ["warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes"])
        self.assertEqual(plan_b.portfolio_estimate.estimate_status, "PRE_CANARY_MODELED_RANGE")
        self.assertIsNotNone(plan_b.portfolio_estimate.dbu_365d_savings_range)
        self.assertIsNotNone(plan_b.portfolio_estimate.aws_365d_savings_range)
        self.assertEqual([x.knob for x in self.w.recommended_execution_plan], [x.knob for x in plan_a.steps])

    def test_portfolio_range_ordering_and_plan_running_ranges(self):
        plan = next(s for s in self.w.scenarios if s.recommended)
        r = plan.portfolio_estimate.estimated_savings_range_365
        self.assertLessEqual(r.low, r.base)
        self.assertLessEqual(r.base, r.high)
        self.assertTrue(all(s.running_estimated_savings_range_365 is not None for s in plan.steps))

    def test_reporter_json_workbook_and_hitl_findings(self):
        from artifact_tool import Blob, SpreadsheetFile
        with tempfile.TemporaryDirectory() as td:
            packages = self.packages
            files = p.package_locked_contract(Path(td), packages, self.evidence)
            payload = json.loads(Path(files[f"package:{self.evidence[0].workspace_id}:{self.evidence[0].warehouse_id}"]).read_text())
            for field in ("candidates", "scenarios", "recommended_plan", "package_economics", "evidence", "workload_profile", "realization"):
                self.assertIn(field, payload)
            self.assertEqual(len(payload["candidates"]), 7)
            self.assertGreater(len(payload["evidence"]), 0)
            wb = SpreadsheetFile.import_xlsx(Blob.load(files["workbook"]))
            sheets = wb.inspect({"kind":"sheet","include":"id,name"}).ndjson
            for name in ("Executive Summary","Warehouse Portfolio","Scenario Plans","Recommendation Registry"):
                self.assertIn(name, sheets)
    def test_read_only_api_scope_and_supported_type_gate_are_explicit(self):
        self.assertEqual(p.SUPPORTED_WAREHOUSE_TYPES, {"CLASSIC", "PRO", "SERVERLESS"})
        source = Path(p.__file__).read_text()
        self.assertIn("/api/2.0/sql/config/warehouses", source)
        self.assertIn("/api/2.1/clusters/get", source)
        self.assertNotIn("/api/2.0/sql/warehouses/{id}", source)
        unsupported = p.demo_evidence()
        unsupported[0].current_epoch.config = p.WarehouseConfig("LAKEHOUSE_REAL_TIME", "CURRENT", "MEDIUM", 1, 1, 10)
        with self.assertRaises(ValueError):
            p.build_recommendation_packages(unsupported, self.t)

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

    def test_all_purpose_cluster_execution_path_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[WHOPT]\nhost = https://dbc.example.cloud.databricks.com\n")
            class R:
                stdout = '{"access_token":"short-lived","expiry":"2026-10-01T20:00:00Z"}'
            with patch("publisher.subprocess.run", return_value=R()), patch("publisher._validate_all_purpose_cluster", return_value={"cluster_source":"UI"}), patch("publisher._current_workspace_id", return_value="123456789"):
                auth = p.resolve_cli_oauth("WHOPT", "cluster", "1234-567890-demo123", str(cfg))
                self.assertEqual(auth.http_path, "sql/protocolv1/o/123456789/1234-567890-demo123")
                self.assertEqual(auth.execution_kind, "cluster")

    def test_compatibility_api_b_and_fallback_a(self):
        auth=p.AuthContext("WHOPT","dbc.example.cloud.databricks.com","/sql/1.0/warehouses/x","tok",None,"warehouse","x","123")
        payload={"enable_serverless_compute":True,"enabled_warehouse_types":[{"warehouse_type":"PRO","enabled":True}],"data_access_config":[],"security_policy":"NONE"}
        with patch("publisher._api_get", return_value=payload):
            ev=p.collect_workspace_compatibility_evidence(auth,"auto")
            self.assertEqual(ev["mode"],"B_READ_ONLY_API")
            self.assertTrue(ev["enable_serverless_compute"])
            self.assertFalse(ev["external_legacy_hive_metastore_detected"])
        with patch("publisher._api_get", side_effect=RuntimeError("denied")):
            ev=p.collect_workspace_compatibility_evidence(auth,"auto")
            self.assertEqual(ev["mode"],"A_HITL_FALLBACK")

    def test_candidate_and_recommended_plan_views(self):
        from contracts import workbook_contract as wc
        projection = wc.project_workbook(self.packages)
        row = projection.warehouse_portfolio[0]
        self.assertIn("Photon", row["candidate_view"])
        self.assertIn("Size", row["candidate_view"])
        self.assertIn("Type", row["candidate_view"])
        self.assertIn("warehouse_size", row["recommended_execution_plan"])
        self.assertIn("READY_FOR_CANARY", row["recommended_execution_plan"])
        self.assertIn("remeasure 7d", row["recommended_execution_plan"])

    def test_logging_file_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            log=p.setup_logging(Path(td),"INFO")
            p.LOGGER.info("test.event value=%s","ok")
            self.assertTrue(log.exists())
            self.assertIn("test.event",log.read_text())

    def test_pat_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[PAT]\nhost = https://dbc.example.cloud.databricks.com\ntoken = secret\n")
            with self.assertRaises(SystemExit):
                p.resolve_cli_oauth("PAT", "abc123", str(cfg))

    def test_same_evidence_produces_identical_locked_json_and_workbook_projection(self):
        from contracts import workbook_contract as wc
        ea = p.demo_evidence(); eb = p.demo_evidence()
        pa = p.build_recommendation_packages(ea, self.t)
        pb = p.build_recommendation_packages(eb, self.t)
        self.assertEqual([x.to_dict() for x in pa], [x.to_dict() for x in pb])
        self.assertEqual(wc.project_workbook(pa), wc.project_workbook(pb))
    def test_auto_stop_blocks_without_gap_or_event_evidence(self):
        ev = p.demo_evidence()
        ev[0].current_epoch.gaps = {}
        ev[0].current_epoch.events["cluster_minutes"] = 0
        result = p.evaluate_warehouses(ev, self.t)[0]
        auto = next(c for c in result.candidates if c.knob == "auto_stop_minutes")
        self.assertEqual(auto.status, "BLOCKED")
        self.assertIn("INSUFFICIENT_IDLE_GAP_SAMPLE", auto.blockers)
        self.assertIn("INSUFFICIENT_EVENT_RESOURCE_EVIDENCE", auto.blockers)

    def test_manifest_carries_locked_contract_and_workbook_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            packages = self.packages
            files = p.package_locked_contract(Path(td), packages, self.evidence)
            manifest = json.loads(Path(files["manifest"]).read_text())
            self.assertEqual(manifest["publisher_version"], p.PUBLISHER_VERSION)
            self.assertEqual(manifest["reporter_contract_version"], "1.0.0")
            self.assertEqual(manifest["workbook_contract_version"], "1.1.0")
            self.assertEqual(manifest["workbook_status"], "GENERATED")
            self.assertEqual(manifest["package_count"], 1)
            self.assertEqual(manifest["workbook_file"], "workbook.xlsx")
    def test_reviewer_rejects_cross_workspace_candidate(self):
        candidates = list(self.w.candidates)
        bad = copy.deepcopy(candidates[0])
        bad.workspace_id = "wrong"
        candidates[0] = bad
        with self.assertRaises(ValueError):
            p.Reviewer(self.t).review(self.evidence[0], candidates)


    def test_photon_inference_uses_coverage_freshness_and_signed_dbu(self):
        row={"photon_true_dbu":950.0,"photon_false_dbu":50.0,"photon_null_dbu":0.0,"latest_usage_time":"2026-09-30T12:00:00+00:00"}
        obs=p.infer_photon_observation(row,"2026-10-01T12:00:00+00:00",self.t)
        self.assertEqual(obs.status,"ENABLED")
        self.assertEqual(obs.enabled_usage_pct,95.0)
        row["photon_null_dbu"]=500.0
        obs=p.infer_photon_observation(row,"2026-10-01T12:00:00+00:00",self.t)
        self.assertEqual(obs.status,"UNKNOWN")

    def test_owner_query_source_and_scenario_confidence_are_reported(self):
        self.assertEqual(self.w.evidence.attribution.owner,"sales-bi@databricks.example")
        self.assertEqual(self.w.query_source_profile["primary_category"],"DASHBOARD")
        self.assertEqual(self.w.query_source_profile["categories"]["POWER_BI"],4200)
        for sc in self.w.scenarios:
            self.assertIn("score",sc.confidence)
            for st in sc.steps:
                self.assertEqual(st.confidence["score"],self.by_knob[st.knob].confidence["score"])

    def test_normalized_domain_contracts_are_dataclasses(self):
        from dataclasses import is_dataclass
        for cls in (p.WarehouseConfig,p.EpochEvidence,p.PhotonObservation,p.QuerySourceProfile,p.WarehouseAttribution,p.AnalysisResult,p.OptimizationDecision,p.CandidateRecommendation,p.PlanStep,p.Scenario,p.WarehouseOptimization):
            self.assertTrue(is_dataclass(cls),cls.__name__)

    def test_pro_size_estimator_does_not_reuse_serverless_dbu_curve(self):
        c=self.by_knob["warehouse_size"]
        self.assertNotEqual(c.metadata.get("capacity_ratio_source"),"DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR_RATIO")
        self.assertIn(c.metadata.get("capacity_ratio_source"),{
            "NOMINAL_SIZE_CAPACITY_RATIO",
            "WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR_RATIO",
        })


class GoldenScenarioTests(unittest.TestCase):
    def setUp(self):
        self.t=p.Thresholds(); self.golden=ROOT/"tests"/"golden"

    def load(self,name):
        payload=json.loads((self.golden/f"{name}.json").read_text())
        ev=[p.evidence_from_dict(x) for x in payload["evidence"]]
        return payload,p.evaluate_warehouses(ev,self.t)

    def warehouse(self,report,warehouse_id):
        return next(w for w in report if w.warehouse_id==warehouse_id)

    def test_all_golden_files_parse_and_are_deterministic(self):
        files=sorted(self.golden.glob("*.json")); self.assertEqual(len(files),10)
        for f in files:
            payload=json.loads(f.read_text()); ev=[p.evidence_from_dict(x) for x in payload["evidence"]]
            a=p.evaluate_warehouses(copy.deepcopy(ev),self.t); b=p.evaluate_warehouses(copy.deepcopy(ev),self.t)
            self.assertEqual(p.canonical_json(a),p.canonical_json(b),f.name)

    def test_idle_expensive(self):
        _,r=self.load("idle-expensive-warehouse"); w=self.warehouse(r,"wh-demo-001"); by={c.knob:c for c in w.candidates}
        self.assertEqual(by["min_clusters"].status,"ACTIONABLE"); self.assertEqual(by["auto_stop_minutes"].status,"ACTIONABLE")
        self.assertGreater(w.aws_365d_baseline_cost,0)

    def test_spill_constrained(self):
        _,r=self.load("spill-constrained"); c=next(c for c in r[0].candidates if c.knob=="warehouse_size")
        self.assertEqual(c.status,"ACTIONABLE"); self.assertEqual(c.recommended_config["warehouse_size"],"X_LARGE")

    def spill_evidence(self, *, queries_30=12000, epoch_start=None):
        """Spill-constrained golden fixture with the sample size / epoch age overridden (audit H2)."""
        payload = json.loads((self.golden / "spill-constrained.json").read_text())
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        epoch["query"]["queries_30"] = queries_30
        if epoch_start is not None:
            epoch["epoch_start"] = epoch_start
        return [p.evidence_from_dict(ev)]

    def size_candidate(self, evidence):
        w = p.evaluate_warehouses(evidence, self.t)[0]
        return w, next(c for c in w.candidates if c.knob == "warehouse_size")

    def test_upsize_requires_minimum_query_sample(self):
        # H2: 1 query that spills must not resize a warehouse.
        w, c = self.size_candidate(self.spill_evidence(queries_30=1))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("INSUFFICIENT_QUERY_SAMPLE", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)
        plan = next(s for s in w.scenarios if s.recommended)
        self.assertNotIn("warehouse_size", [s.knob for s in plan.steps])

    def test_upsize_requires_mature_config_epoch(self):
        # Analysis time is 2026-10-01T12:00Z, so this epoch is 2 days old (< 7).
        _, c = self.size_candidate(self.spill_evidence(epoch_start="2026-09-29T12:00:00+00:00"))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("CURRENT_CONFIG_EPOCH_TOO_NEW", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)

    def test_upsize_reports_every_missing_evidence_requirement(self):
        _, c = self.size_candidate(self.spill_evidence(queries_30=1, epoch_start="2026-09-29T12:00:00+00:00"))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(set(c.blockers), {"INSUFFICIENT_QUERY_SAMPLE", "CURRENT_CONFIG_EPOCH_TOO_NEW"})

    def test_upsize_actionable_exactly_at_evidence_boundaries(self):
        # 100 queries and exactly 7 days of epoch age are sufficient.
        _, c = self.size_candidate(self.spill_evidence(queries_30=100, epoch_start="2026-09-24T12:00:00+00:00"))
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertEqual(c.recommended_config["warehouse_size"], "X_LARGE")
        _, below = self.size_candidate(self.spill_evidence(queries_30=99))
        self.assertEqual(below.status, "INSUFFICIENT_EVIDENCE")

    def test_insufficient_evidence_upsize_has_no_savings_and_yields_valid_package(self):
        ev = self.spill_evidence(queries_30=1)
        _, c = self.size_candidate(ev)
        self.assertIn(c.estimated_savings_365, (None, 0, 0.0))
        packages = p.build_recommendation_packages(ev, self.t)  # runs semantic validation
        pkg = packages[0]
        pkg_dict = pkg if isinstance(pkg, dict) else json.loads(p.canonical_json(pkg))
        size = next(x for x in pkg_dict["candidates"] if x["knob"] == "warehouse_size")
        self.assertEqual(size["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(size["independent_economics"]["estimation_status"], "UNAVAILABLE")
        self.assertEqual(size["recommended_value"], size["current_value"])

    # --- audit H3: severity is measured among *affected* queries, not all queries -------------------------
    NO_SEVERITY = {
        "spill_queries_30": 0, "p95_spill_bytes_when_spilled_30": None,
        "queued_queries_30": 0, "p95_wait_capacity_ms_when_queued_30": None,
        "compute_wait_queries_30": 0, "p95_wait_compute_ms_when_waited_30": None,
    }

    def severity_evidence(self, golden, **query):
        """Golden fixture re-expressed in the conditional-severity schema, with `query` overrides."""
        payload = json.loads((self.golden / f"{golden}.json").read_text())
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        for legacy in ("p95_spill_bytes_30", "p95_wait_capacity_ms_30", "p95_wait_compute_ms_30"):
            epoch["query"].pop(legacy, None)
        epoch["query"].update(self.NO_SEVERITY)
        epoch["query"].update(query)
        return [p.evidence_from_dict(ev)]

    def candidate(self, evidence, knob):
        return next(c for c in p.evaluate_warehouses(evidence, self.t)[0].candidates if c.knob == knob)

    GIB10 = 10 * 1024**3

    def test_rare_but_severe_spill_triggers_upsize(self):
        # 3% of 12,000 queries spill 10 GiB; the other 97% spill nothing. The old all-query p95 was 0.
        ev = self.severity_evidence("spill-constrained", spill_query_pct_30=3.0, spill_queries_30=360,
                                    p95_spill_bytes_when_spilled_30=self.GIB10)
        c = self.candidate(ev, "warehouse_size")
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertEqual(c.recommended_config["warehouse_size"], "X_LARGE")

    def test_spill_severity_is_judged_among_spilled_queries(self):
        ev = self.severity_evidence("spill-constrained", spill_query_pct_30=3.0, spill_queries_30=360,
                                    p95_spill_bytes_when_spilled_30=200 * 1024**2)
        c = self.candidate(ev, "warehouse_size")
        self.assertNotEqual(c.status, "ACTIONABLE")
        self.assertIn("SPILL_PRESENT", c.blockers)

    def test_upsize_needs_minimum_affected_queries(self):
        # 100 queries is enough sample (H2), but 5 spilling queries cannot support a p95 severity claim.
        ev = self.severity_evidence("spill-constrained", queries_30=100, spill_query_pct_30=5.0, spill_queries_30=5,
                                    p95_spill_bytes_when_spilled_30=self.GIB10)
        c = self.candidate(ev, "warehouse_size")
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("INSUFFICIENT_AFFECTED_QUERY_SAMPLE", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)

    def test_queue_severity_among_queued_queries_at_incidence_boundary(self):
        # Exactly 5.0% queued with a 45s tail among queued queries: increase signal.
        ev = self.severity_evidence("concurrency-constrained", queued_query_pct_30=5.0, queued_queries_30=600,
                                    p95_wait_capacity_ms_when_queued_30=45000)
        c = self.candidate(ev, "max_clusters")
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertEqual(c.recommended_config["max_clusters"], 3)
        short = self.severity_evidence("concurrency-constrained", queued_query_pct_30=5.0, queued_queries_30=600,
                                       p95_wait_capacity_ms_when_queued_30=10000)
        self.assertNotEqual(self.candidate(short, "max_clusters").status, "ACTIONABLE")

    def test_max_clusters_increase_needs_minimum_affected_queries(self):
        ev = self.severity_evidence("concurrency-constrained", queries_30=100, queued_query_pct_30=10.0, queued_queries_30=10,
                                    p95_wait_capacity_ms_when_queued_30=45000)
        c = self.candidate(ev, "max_clusters")
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("INSUFFICIENT_AFFECTED_QUERY_SAMPLE", c.blockers)

    def test_cold_start_waits_on_first_queries_block_auto_stop(self):
        # 30 first-after-start queries wait 120s out of 12,000; the all-query p95 compute wait was 0.
        ev = self.severity_evidence("cold-start-sensitive", compute_wait_queries_30=30,
                                    p95_wait_compute_ms_when_waited_30=120000)
        c = self.candidate(ev, "auto_stop_minutes")
        self.assertEqual(c.status, "BLOCKED")
        self.assertIn("COLD_START_SENSITIVE", c.blockers)

    def test_few_long_capacity_waits_block_min_cluster_reduction(self):
        # Guard rules fire on any affected queries: 12 queued queries (0.1%) waiting 20s block a savings change.
        ev = self.severity_evidence("idle-expensive-warehouse", queued_query_pct_30=0.1, queued_queries_30=12,
                                    p95_wait_capacity_ms_when_queued_30=20000)
        c = self.candidate(ev, "min_clusters")
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertIn("QUEUE_PRESSURE_PRESENT", c.blockers)

    def test_missing_severity_evidence_fails_closed(self):
        # Older evidence without the conditional-severity fields must not be read as 'no pressure' (zero).
        payload = json.loads((self.golden / "idle-expensive-warehouse.json").read_text())
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        for key in list(epoch["query"]):
            if key in self.NO_SEVERITY or key in ("p95_spill_bytes_30", "p95_wait_capacity_ms_30", "p95_wait_compute_ms_30"):
                del epoch["query"][key]
        evidence = [p.evidence_from_dict(ev)]
        minc = self.candidate(evidence, "min_clusters")
        self.assertEqual(minc.status, "NO_CHANGE")
        self.assertIn("QUEUE_WAIT_EVIDENCE_UNAVAILABLE", minc.blockers)
        auto = self.candidate(evidence, "auto_stop_minutes")
        self.assertEqual(auto.status, "BLOCKED")
        self.assertIn("COLD_START_EVIDENCE_UNAVAILABLE", auto.blockers)

    def test_concurrency_constrained(self):
        _,r=self.load("concurrency-constrained"); c=next(c for c in r[0].candidates if c.knob=="max_clusters")
        self.assertEqual(c.status,"ACTIONABLE"); self.assertEqual(c.recommended_config["max_clusters"],3)

    def test_cold_start_sensitive(self):
        _,r=self.load("cold-start-sensitive"); c=next(c for c in r[0].candidates if c.knob=="auto_stop_minutes")
        self.assertEqual(c.status,"BLOCKED"); self.assertIn("COLD_START_SENSITIVE",c.blockers)

    def test_new_epoch_and_sparse_blockers(self):
        for name,blocker in (("newly-changed-config","CURRENT_CONFIG_EPOCH_TOO_NEW"),("sparse-evidence","INSUFFICIENT_QUERY_SAMPLE")):
            _,r=self.load(name); self.assertTrue(any(blocker in c.blockers for c in r[0].candidates),name)

    def test_photon_disabled_is_inferred_and_becomes_canary_candidate(self):
        _,r=self.load("photon-disabled")
        self.assertNotIn("enable_photon",{})
        w=r[0]
        c=next(c for c in w.candidates if c.knob=="enable_photon")
        self.assertEqual(c.status,"TEST_REQUIRED")
        self.assertEqual(c.current_config,{"enable_photon":False})
        self.assertEqual(c.recommended_config,{"enable_photon":True})
        self.assertIn("warehouse_size",c.invalidates)
        plan=next(s for s in w.scenarios if s.recommended)
        self.assertEqual(plan.steps[0].knob,"enable_photon")
        self.assertEqual(plan.portfolio_estimate.estimate_status,"REQUIRES_CANARY")

    def test_serverless_to_pro_costed_with_workspace_calibration(self):
        _,r=self.load("serverless-to-pro-costed"); w=self.warehouse(r,"wh-serverless-001")
        c=next(c for c in w.candidates if c.knob=="warehouse_type"); self.assertEqual(c.recommended_config,{"warehouse_type":"PRO"})
        self.assertIsNotNone(c.dbu_365d_estimated_cost_range); self.assertIsNotNone(c.aws_365d_estimated_cost_range)
        self.assertEqual(c.metadata["migration_model"]["dbu_rate_source"],"WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR")
        b=next(s for s in w.scenarios if s.name.startswith("Plan B")); self.assertEqual(b.portfolio_estimate.estimate_status,"PRE_CANARY_MODELED_RANGE")

    def test_missing_aws_region_is_explicit(self):
        _,r=self.load("aws-region-missing"); w=r[0]
        self.assertIsNone(w.aws_365d_baseline_cost); self.assertIsNone(w.total_365d_baseline_cost)



if __name__ == "__main__":
    unittest.main()
