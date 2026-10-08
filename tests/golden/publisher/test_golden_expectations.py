import copy
import json
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthesize_resource_daily  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.common.hashing import canonical_json  # noqa: E402
from publisher.common.utils import safe_float  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from tests.evidence_helpers import move_current_epoch_start  # noqa: E402
from tests.golden.publisher import case_names, load_case  # noqa: E402


class GoldenScenarioTests(unittest.TestCase):
    def setUp(self):
        self.t=Thresholds()

    def load(self,name):
        payload=load_case(name)
        ev=[evidence_from_dict(x) for x in payload["evidence"]]
        return payload,p.evaluate_warehouses(ev,self.t)

    def warehouse(self,report,warehouse_id):
        return next(w for w in report if w.warehouse_id==warehouse_id)

    def test_all_golden_files_parse_and_are_deterministic(self):
        names=case_names(); self.assertGreaterEqual(len(names),10)
        for name in names:
            payload=load_case(name)
            if "evidence" not in payload: continue  # collector-level fixture (raw snapshots), covered by tests/golden/publisher/test_golden_publisher.py
            ev=[evidence_from_dict(x) for x in payload["evidence"]]
            a=p.evaluate_warehouses(copy.deepcopy(ev),self.t); b=p.evaluate_warehouses(copy.deepcopy(ev),self.t)
            self.assertEqual(canonical_json(a),canonical_json(b),name)

    def test_idle_expensive(self):
        _,r=self.load("idle-expensive-warehouse"); w=self.warehouse(r,"wh-demo-001"); by={c.knob:c for c in w.candidates}
        self.assertEqual(by["min_clusters"].status,"TEST_REQUIRED"); self.assertEqual(by["auto_stop_minutes"].status,"ACTIONABLE")
        self.assertGreater(w.aws_365d_baseline_cost,0)

    def test_classic_warehouse_is_optimized_like_its_pro_twin(self):
        # No demo or other golden warehouse is Classic. This case is the c06 evidence with the type and the billed SQL SKU class
        # switched to Classic: the same size, cluster and auto-stop rules apply, AWS cost stays modeled, and Serverless is
        # the migration experiment (docs/architecture/publisher/context.md, PRD acceptance criterion 1).
        _, classic_report = self.load("classic-warehouse")
        _, pro_report = self.load("c06-oversized-warehouse")
        classic, pro = self.warehouse(classic_report, "wh-demo-001"), self.warehouse(pro_report, "wh-demo-001")
        self.assertEqual(classic.evidence.current_epoch.config.warehouse_type, "CLASSIC")
        by = {c.knob: c for c in classic.candidates}
        self.assertEqual(by["warehouse_size"].status, "TEST_REQUIRED")
        self.assertEqual((by["warehouse_size"].current_config["warehouse_size"], by["warehouse_size"].recommended_config["warehouse_size"]), ("LARGE", "MEDIUM"))
        self.assertEqual(by["warehouse_type"].status, "TEST_REQUIRED")
        self.assertEqual((by["warehouse_type"].current_config["warehouse_type"], by["warehouse_type"].recommended_config["warehouse_type"]), ("CLASSIC", "SERVERLESS"))
        self.assertIn("A_B_COST_PERFORMANCE_VALIDATION_REQUIRED", by["warehouse_type"].blockers)
        self.assertGreater(classic.aws_365d_baseline_cost, 0)  # Classic has the AWS component, like Pro
        decisions = lambda w: {c.knob: (c.status, c.recommended_config, c.blockers) for c in w.candidates}  # noqa: E731
        self.assertEqual(decisions(classic), decisions(pro))  # type-independent rules decide identically
        packages = {name: p.build_recommendation_packages([evidence_from_dict(x) for x in load_case(name)["evidence"]], self.t)[0].to_dict()
                    for name in ("classic-warehouse", "c06-oversized-warehouse")}
        self.assertEqual(packages["classic-warehouse"]["current_config"]["warehouse_type"], "CLASSIC")
        self.assertEqual([s["scenario_type"] for s in packages["classic-warehouse"]["scenarios"]],
                         [s["scenario_type"] for s in packages["c06-oversized-warehouse"]["scenarios"]])

    def test_spill_constrained(self):
        _,r=self.load("spill-constrained"); c=next(c for c in r[0].candidates if c.knob=="warehouse_size")
        self.assertEqual(c.status,"TEST_REQUIRED"); self.assertEqual(c.recommended_config["warehouse_size"],"X_LARGE")

    def spill_evidence(self, *, queries_30=12000, epoch_start=None):
        """Spill-constrained golden fixture with the sample size / epoch age overridden (audit H2)."""
        payload = load_case("spill-constrained")
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        epoch["query"]["queries_30"] = queries_30
        epoch["query"]["decision_queries_30"] = queries_30  # spill and latency are judged on executed queries
        e = evidence_from_dict(ev)
        if epoch_start is not None:
            move_current_epoch_start(e, epoch_start)
        return [e]

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
        # Analysis time is 2026-10-01T12:00Z: an epoch started the day before has no two settled billed days yet.
        _, c = self.size_candidate(self.spill_evidence(epoch_start="2026-09-30T12:00:00+00:00"))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("CURRENT_CONFIG_EPOCH_TOO_NEW", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)

    def test_upsize_reports_every_missing_evidence_requirement(self):
        _, c = self.size_candidate(self.spill_evidence(queries_30=1, epoch_start="2026-09-30T12:00:00+00:00"))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(set(c.blockers), {"INSUFFICIENT_QUERY_SAMPLE", "CURRENT_CONFIG_EPOCH_TOO_NEW"})

    def test_upsize_canary_exactly_at_evidence_boundaries(self):
        # 100 queries and a configuration with settled billed days (ready) are sufficient.
        _, c = self.size_candidate(self.spill_evidence(queries_30=100, epoch_start="2026-09-24T12:00:00+00:00"))
        self.assertEqual(c.status, "TEST_REQUIRED")
        self.assertEqual(c.recommended_config["warehouse_size"], "X_LARGE")
        _, below = self.size_candidate(self.spill_evidence(queries_30=99))
        self.assertEqual(below.status, "INSUFFICIENT_EVIDENCE")

    def test_insufficient_evidence_upsize_has_no_savings_and_yields_valid_package(self):
        ev = self.spill_evidence(queries_30=1)
        _, c = self.size_candidate(ev)
        self.assertIsNone(c.estimated_savings_365)
        packages = p.build_recommendation_packages(ev, self.t)  # runs semantic validation
        pkg = packages[0]
        pkg_dict = pkg if isinstance(pkg, dict) else json.loads(canonical_json(pkg))
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
        payload = load_case(golden)
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        for legacy in ("p95_spill_bytes_30", "p95_wait_capacity_ms_30", "p95_wait_compute_ms_30"):
            epoch["query"].pop(legacy, None)
        epoch["query"].update(self.NO_SEVERITY)
        epoch["query"].update(query)
        return [evidence_from_dict(ev)]

    def candidate(self, evidence, knob):
        return next(c for c in p.evaluate_warehouses(evidence, self.t)[0].candidates if c.knob == knob)

    GIB10 = 10 * 1024**3

    def test_rare_but_severe_spill_triggers_upsize(self):
        # 3% of 12,000 queries spill 10 GiB; the other 97% spill nothing. The old all-query p95 was 0.
        ev = self.severity_evidence("spill-constrained", spill_query_pct_30=3.0, spill_queries_30=360,
                                    p95_spill_bytes_when_spilled_30=self.GIB10)
        c = self.candidate(ev, "warehouse_size")
        self.assertEqual(c.status, "TEST_REQUIRED")
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

    def test_cold_start_budget_counts_added_restarts_not_current_waits(self):
        # Long compute waits on today's first-after-start queries do not block a lower auto-stop that adds no restart:
        # the guard is the share of queries that would meet a restart the change adds.
        ev = self.severity_evidence("idle-expensive-warehouse", compute_wait_queries_30=30, p95_wait_compute_ms_when_waited_30=120000)
        self.assertEqual(self.candidate(ev, "auto_stop_minutes").status, "ACTIONABLE")
        ev = self.severity_evidence("idle-expensive-warehouse")
        for window in ev[0].current_epoch.gaps["running_idle_windows"].values():  # the same share blended and recently
            window["queries"] = 12000
            rows = window["running_idle_by_target"]
            rows["10"]["additional_starts"] = 241  # 2.008% of 12,000 queries: over the 2% budget
            rows["15"]["additional_starts"] = 240  # exactly 2%: within it
        c = self.candidate(ev, "auto_stop_minutes")
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertNotEqual(c.recommended_config["auto_stop_minutes"], 10)

    def scaled(self, golden, *, max_clusters=None, **events):
        """Severity-schema golden fixture with the current epoch's max_clusters / events overridden (audit M3)."""
        import dataclasses
        ev = self.severity_evidence(golden)
        ep = ev[0].current_epoch
        if max_clusters is not None:
            ep.config = dataclasses.replace(ep.config, max_clusters=max_clusters)
        ep.events.update(events)
        uptime = safe_float(ep.events.get("uptime_minutes"))
        for pct, minutes in (("time_at_max_pct", "minutes_at_max"), ("time_at_min_pct", "minutes_at_min")):
            if pct in events and minutes not in events:
                ep.events[minutes] = uptime * events[pct] / 100.0
        # Capacity rules read the recent daily resource window: rebuild it from the overridden totals.
        ep.resource_daily = synthesize_resource_daily(ep, ev[0].analysis_time)
        return ev

    def queued(self, **overrides):
        return self.scaled("concurrency-constrained", **overrides)

    def increase_candidate(self, **events):
        ev = self.queued(**events)
        q = ev[0].current_epoch.query
        q.update(queued_query_pct_30=5.0, queued_queries_30=600, p95_wait_capacity_ms_when_queued_30=45000)
        return self.candidate(ev, "max_clusters")

    def test_cluster_recycling_blip_above_max_is_not_saturation(self):
        # Peak 3 > configured max 2 (recycle transient) but only 0.2% of running time actually at the ceiling.
        c = self.increase_candidate(max_observed_clusters=3, time_at_max_pct=0.2, minutes_at_max=20)
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertEqual(c.recommended_config, c.current_config)

    def test_increase_needs_sustained_time_at_max_with_boundary(self):
        limit = self.t.time_at_max_pct_saturated
        self.assertEqual(limit, 5.0)
        up = self.increase_candidate(max_observed_clusters=2, time_at_max_pct=limit)
        self.assertEqual(up.status, "ACTIONABLE")
        self.assertEqual(up.recommended_config["max_clusters"], 3)
        below = self.increase_candidate(max_observed_clusters=2, time_at_max_pct=limit - 0.01)
        self.assertEqual(below.status, "NO_CHANGE")

    def reduction(self, max_clusters, peak_queries):
        """Idle-expensive fixture with max overridden and P99.5 running + queued queries per busy minute = peak_queries."""
        ev = self.scaled("idle-expensive-warehouse", max_clusters=max_clusters, max_observed_clusters=2, time_at_max_pct=0.0, minutes_at_max=0.0)
        hist = {"1": 990.0, str(peak_queries): 10.0}
        ev[0].current_epoch.query["concurrency_histogram"].update(RECENT=dict(hist), EPOCH=dict(hist))
        return self.candidate(ev, "max_clusters")

    def test_max_reduction_targets_peak_demand_plus_one_cluster(self):
        # Pro/Classic: one cluster per 10 concurrent queries at P99.5, plus one cluster of headroom, in one move.
        self.assertEqual(self.reduction(4, 20).recommended_config["max_clusters"], 3)   # 2 clusters needed
        self.assertEqual(self.reduction(4, 30).status, "NO_CHANGE")                      # 3 + 1 = 4: not below max 4
        self.assertEqual(self.reduction(10, 40).recommended_config["max_clusters"], 5)  # 4 + 1
        self.assertEqual(self.reduction(10, 21).recommended_config["max_clusters"], 4)  # 21 queries need 3 clusters

    def test_max_reduction_never_goes_below_min_clusters(self):
        c = self.reduction(10, 5)  # 1 cluster + 1 = 2; idle-expensive min_clusters is 2
        self.assertEqual(c.recommended_config["max_clusters"], 2)

    def test_few_long_capacity_waits_block_min_cluster_reduction(self):
        # Guard rules fire on any affected queries: 12 queued queries (0.1%) waiting 20s block a savings change.
        ev = self.severity_evidence("idle-expensive-warehouse", queued_query_pct_30=0.1, queued_queries_30=12,
                                    p95_wait_capacity_ms_when_queued_30=20000)
        c = self.candidate(ev, "min_clusters")
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertIn("QUEUE_PRESSURE_PRESENT", c.blockers)

    def without_severity(self, golden, **query):
        """Golden fixture whose conditional-severity fields are absent (older collector), with `query` overrides."""
        payload = load_case(golden)
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        for key in list(self.NO_SEVERITY) + ["p95_spill_bytes_30", "p95_wait_capacity_ms_30", "p95_wait_compute_ms_30"]:
            epoch["query"].pop(key, None)
        epoch["query"].update(query)
        return [evidence_from_dict(ev)]

    def test_upsize_with_unknown_spill_severity_is_insufficient_evidence_not_a_silent_no_change(self):
        # Review finding: spill frequency crosses the threshold but severity is unknown. The decision must say so.
        for ev in (
            self.without_severity("spill-constrained"),  # count and p95 absent
            self.without_severity("spill-constrained", spill_queries_30=360, p95_spill_bytes_when_spilled_30=None),  # p95 null
        ):
            c = self.candidate(ev, "warehouse_size")
            self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
            self.assertEqual(c.blockers, ["SPILL_SEVERITY_EVIDENCE_UNAVAILABLE"])
            self.assertEqual(c.recommended_config, c.current_config)

    def test_max_clusters_increase_with_unknown_wait_severity_is_insufficient_evidence(self):
        c = self.candidate(self.without_severity("concurrency-constrained"), "max_clusters")
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(c.blockers, ["QUEUE_WAIT_EVIDENCE_UNAVAILABLE"])
        self.assertEqual(c.recommended_config, c.current_config)

    def test_missing_severity_evidence_fails_closed(self):
        # Older evidence without the conditional-severity fields must not be read as 'no pressure' (zero).
        payload = load_case("idle-expensive-warehouse")
        ev = payload["evidence"][0]
        epoch = next(x for x in ev["epochs"] if x["is_current"])
        for key in list(epoch["query"]):
            if key in self.NO_SEVERITY or key in ("p95_spill_bytes_30", "p95_wait_capacity_ms_30", "p95_wait_compute_ms_30"):
                del epoch["query"][key]
        evidence = [evidence_from_dict(ev)]
        minc = self.candidate(evidence, "min_clusters")
        self.assertEqual(minc.status, "NO_CHANGE")
        self.assertIn("QUEUE_WAIT_EVIDENCE_UNAVAILABLE", minc.blockers)
        # Auto-stop no longer reads compute-wait severity (the cold-start guard counts the restarts a change adds).
        auto = self.candidate(evidence, "auto_stop_minutes")
        self.assertEqual(auto.status, "ACTIONABLE")

    # --- H5: a withheld estimate is null, never zero --------------------------------------------------------
    def golden_package(self, name):
        payload = load_case(name)
        evidence = [evidence_from_dict(x) for x in payload["evidence"]]
        return p.build_recommendation_packages(evidence, self.t)[0].to_dict()

    @staticmethod
    def all_null(ranges):
        return all(ranges[k] is None for k in ("dbu", "aws", "total"))

    def test_blocked_candidate_economics_are_null_not_zero(self):
        pkg = self.golden_package("c26-failure-heavy")
        blocked = next(c for c in pkg["candidates"] if c["knob"] == "auto_stop_minutes")
        self.assertEqual(blocked["status"], "BLOCKED")
        econ = blocked["independent_economics"]
        self.assertEqual(econ["estimation_status"], "UNAVAILABLE")
        self.assertTrue(self.all_null(econ["savings_365d"]))
        self.assertTrue(self.all_null(econ["estimated_cost_365d"]))

    def test_insufficient_evidence_candidate_economics_are_null_not_zero(self):
        pkg = p.build_recommendation_packages(self.spill_evidence(queries_30=1), self.t)[0].to_dict()
        size = next(c for c in pkg["candidates"] if c["knob"] == "warehouse_size")
        self.assertEqual(size["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(size["independent_economics"]["estimation_status"], "UNAVAILABLE")
        self.assertTrue(self.all_null(size["independent_economics"]["savings_365d"]))

    def test_no_change_candidate_keeps_a_known_zero(self):
        pkg = self.golden_package("cold-start-sensitive")
        # A no-change candidate (or the unpriced ceiling guardrail) is a known zero, not unknown.
        unchanged = next(c for c in pkg["candidates"] if c["status"] == "NO_CHANGE" or c["knob"] == "max_clusters")
        econ = unchanged["independent_economics"]
        self.assertEqual(econ["estimation_status"], "NO_DELTA_MODELED")
        self.assertEqual(econ["savings_365d"]["total"], {"low": 0.0, "base": 0.0, "high": 0.0})

    def test_halted_scenario_economics_are_null_and_excluded_from_the_package(self):
        evidence = demo_evidence()
        evidence[0].sql_sku_prices = {}  # no target SQL price: the migration has no deterministic DBU counterfactual
        pkg = p.build_recommendation_packages(evidence, self.t)[0].to_dict()
        plan_b = next(s for s in pkg["scenarios"] if s["scenario_name"].startswith("Plan B"))
        econ = plan_b["portfolio_economics"]
        self.assertEqual(econ["estimation_status"], "UNAVAILABLE")
        for key in ("savings_365d", "estimated_cost_365d"):
            self.assertTrue(self.all_null(econ[key]), key)
        self.assertIsNone(econ["contingent_savings_365d"])
        self.assertTrue(all(st["projected_economics"] is None for st in plan_b["execution_plan"]))
        # Excluded from the envelope; the priced recommended plan still carries the package economics.
        self.assertIn(plan_b["scenario_id"], [x["scenario_id"] for x in pkg["package_economics"]["excluded_scenarios"]])
        self.assertIsNotNone(pkg["package_economics"]["estimated_savings_365d"])

    def test_scenario_with_no_steps_is_a_known_zero_not_unavailable(self):
        pkg = self.golden_package("newly-changed-config")
        plan_a = next(s for s in pkg["scenarios"] if s["scenario_name"].startswith("Plan A"))
        self.assertEqual([st for st in plan_a["execution_plan"] if st["step_type"] == "CANDIDATE"], [])
        econ = plan_a["portfolio_economics"]
        self.assertEqual(econ["estimation_status"], "NO_DELTA_MODELED")
        self.assertEqual(econ["savings_365d"]["total"], {"low": 0.0, "base": 0.0, "high": 0.0})

    def test_concurrency_constrained(self):
        _,r=self.load("concurrency-constrained"); c=next(c for c in r[0].candidates if c.knob=="max_clusters")
        self.assertEqual(c.status,"ACTIONABLE"); self.assertEqual(c.recommended_config["max_clusters"],3)

    def test_cold_start_sensitive(self):
        # Every lower value adds restarts for 5% of queries (600 of 12,000): above the 2% budget, so auto-stop stays.
        _,r=self.load("cold-start-sensitive"); c=next(c for c in r[0].candidates if c.knob=="auto_stop_minutes")
        self.assertEqual(c.status,"NO_CHANGE"); self.assertIn("COLD_START_BUDGET_EXCEEDED",c.blockers)

    def test_new_epoch_and_sparse_blockers(self):
        for name,blocker in (("newly-changed-config","CURRENT_CONFIG_EPOCH_TOO_NEW"),("sparse-evidence","INSUFFICIENT_QUERY_SAMPLE")):
            _,r=self.load(name); self.assertTrue(any(blocker in c.blockers for c in r[0].candidates),name)

    def test_photon_is_observed_and_reported_but_never_recommended(self):
        # Databricks publishes no on/off price for Photon on SQL warehouses, so the product leaves Photon at its
        # default: the billing-derived observation stays in current_config, but no candidate or plan step exists.
        for name in case_names():
            payload=load_case(name)
            if "evidence" not in payload: continue  # collector-level fixture (raw snapshots)
            ev=[evidence_from_dict(x) for x in payload["evidence"]]
            for w in p.evaluate_warehouses(ev,self.t):
                self.assertNotIn("enable_photon",{c.knob for c in w.candidates},name)
                for sc in w.scenarios:
                    self.assertNotIn("enable_photon",[st.knob for st in sc.steps],name)
        pkg=self.golden_package("photon-disabled")
        self.assertEqual(pkg["current_config"]["photon"]["status"],"DISABLED")
        self.assertNotIn("enable_photon",[c["knob"] for c in pkg["candidates"]])
        self.assertEqual(len(pkg["candidates"]),5)
        plan_a=next(x for x in pkg["scenarios"] if x["scenario_name"].startswith("Plan A"))
        self.assertNotEqual(plan_a["portfolio_economics"]["estimation_status"],"UNAVAILABLE")  # no longer blocked by Photon
        self.assertIsNotNone(plan_a["portfolio_economics"]["savings_365d"]["total"])

    def test_serverless_to_pro_costed_with_workspace_calibration(self):
        _,r=self.load("serverless-to-pro-costed"); w=self.warehouse(r,"wh-serverless-001")
        c=next(c for c in w.candidates if c.knob=="warehouse_type"); self.assertEqual(c.recommended_config,{"warehouse_type":"PRO"})
        self.assertIsNotNone(c.dbu_365d_estimated_cost_range); self.assertIsNotNone(c.aws_365d_estimated_cost_range)
        self.assertEqual(c.metadata["migration_model"]["dbu_rate_source"],"WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR")
        b=next(s for s in w.scenarios if s.name.startswith("Plan B")); self.assertEqual(b.portfolio_estimate.estimate_status,"PRE_CANARY_MODELED_RANGE")

    def test_missing_aws_region_is_explicit(self):
        _,r=self.load("aws-region-missing"); w=r[0]
        self.assertIsNone(w.aws_365d_baseline_cost); self.assertIsNone(w.total_365d_baseline_cost)
