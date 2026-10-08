import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS  # noqa: E402
from publisher.contracts.source import WarehouseConfig  # noqa: E402
from publisher.reviewer.package import validate_locked_package_schema  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class OptimizerCases(PublisherCase):
    def test_five_deterministic_candidates_and_no_photon_or_channel_recommendation(self):
        self.assertEqual(set(self.by_knob), set(ACTIVE_KNOBS))
        self.assertEqual(len(self.w.candidates), 5)
        self.assertNotIn("warehouse_channel", self.by_knob)
        self.assertNotIn("enable_photon", self.by_knob)

    def test_expected_demo_decisions(self):
        self.assertEqual(self.by_knob["warehouse_size"].status, "TEST_REQUIRED")
        self.assertEqual(self.by_knob["warehouse_size"].recommended_config, {"warehouse_size": "MEDIUM"})
        self.assertEqual(self.by_knob["max_clusters"].recommended_config, {"max_clusters": 3})
        self.assertEqual(self.by_knob["min_clusters"].recommended_config, {"min_clusters": 1})
        # Cheapest value on measured running idle: 10 removes 25,000 cluster-minutes and adds 210 restarts (1,680).
        self.assertEqual(self.by_knob["auto_stop_minutes"].recommended_config, {"auto_stop_minutes": 10})
        self.assertEqual(self.by_knob["warehouse_type"].recommended_config, {"warehouse_type": "SERVERLESS"})

    def test_recommendation_ids_are_deterministic_and_include_current_config(self):
        r2 = p.evaluate_warehouses(demo_evidence(), self.t)
        self.assertEqual([c.recommendation_id for c in self.w.candidates], [c.recommendation_id for c in r2[0].candidates])
        old = self.by_knob["warehouse_size"].recommendation_id
        changed = demo_evidence()
        changed[0].current_epoch.config = WarehouseConfig("PRO", "CURRENT", "X_LARGE", 2, 4, 120)
        new = next(c for c in p.evaluate_warehouses(changed, self.t)[0].candidates if c.knob == "warehouse_size").recommendation_id
        self.assertNotEqual(old, new)

    def test_min_cluster_reduction_is_a_canary_candidate_not_actionable(self):
        # H4: time at the floor proves the autoscaler sat at min, not that a floor cluster was spare.
        c = self.by_knob["min_clusters"]
        self.assertEqual(c.status, "TEST_REQUIRED")
        self.assertIn("SPARE_FLOOR_CAPACITY_UNPROVEN", c.blockers)
        self.assertEqual(c.recommended_config, {"min_clusters": 1})
        plan = next(s for s in self.w.scenarios if s.recommended)
        step = next(x for x in plan.steps if x.knob == "min_clusters")
        self.assertIn("CANARY", step.validation_gate)

    def test_min_cluster_savings_are_spare_floor_capacity_scaled_by_realization(self):
        c = self.by_knob["min_clusters"]
        r = c.estimated_savings_range_365
        share = c.metadata["spare_floor_share"]
        self.assertGreater(share, 0)
        # High: all spare floor capacity is removed; base and low scale it by the shared realization tracks.
        self.assertAlmostEqual(r.high, c.total_365d_baseline_cost * share, delta=1.0)
        self.assertAlmostEqual(r.base, r.high * self.t.realization_base, delta=1.0)
        self.assertAlmostEqual(r.low, r.high * self.t.realization_conservative, delta=1.0)
        self.assertIn("spare floor capacity", c.metadata["method"])

    def test_min_cluster_cut_without_floor_demand_is_not_priced(self):
        # Evidence without the floor-demand maps: the cut stays a canary, its economics are withheld (never zero).
        e = copy.deepcopy(self.evidence[0])
        for row in e.current_epoch.resource_daily:
            row.pop("floor_minutes_by_demand_clusters", None)
        c = next(x for x in p.evaluate_warehouses([e], self.t)[0].candidates if x.knob == "min_clusters")
        self.assertEqual(c.status, "TEST_REQUIRED")
        self.assertIsNone(c.estimated_savings_365)
        self.assertEqual(c.metadata["estimate_blocker"], "FLOOR_DEMAND_EVIDENCE_UNAVAILABLE")

    def test_utilization_does_not_change_decisions(self):
        decisions = []
        for ratio in (0.01, 5.0):
            e = copy.deepcopy(self.evidence[0])
            e.current_epoch.query["epoch_task_duration_ms"] = 60000.0 * e.current_epoch.events["cluster_minutes"] * ratio
            decisions.append([(c.knob, c.status, c.recommended_config, c.blockers, c.estimated_savings_range_365)
                              for c in p.evaluate_warehouses([e], self.t)[0].candidates])
        self.assertEqual(decisions[0], decisions[1])

    def test_baseline_demo_shrinks_capacity(self):
        # Guards the tests below: with full evidence the demo warehouse gets a downsize canary, a ceiling cut and a floor cut.
        self.assertEqual(self.by_knob["warehouse_size"].status, "TEST_REQUIRED")
        self.assertEqual(self.by_knob["max_clusters"].recommended_config, {"max_clusters": 3})
        self.assertNotEqual(self.by_knob["min_clusters"].status, "NO_CHANGE")

    def test_missing_pressure_evidence_blocks_every_capacity_cut(self):
        w = p.evaluate_warehouses([self._without_pressure_evidence()], self.t)[0]
        by = {c.knob: c for c in w.candidates}
        size = by["warehouse_size"]
        self.assertEqual(size.recommended_config, size.current_config)
        for code in ("SPILL_EVIDENCE_UNAVAILABLE", "QUEUE_EVIDENCE_UNAVAILABLE", "LATENCY_EVIDENCE_UNAVAILABLE"):
            self.assertIn(code, size.blockers)
        self.assertEqual(by["max_clusters"].recommended_config, by["max_clusters"].current_config)
        self.assertIn("QUEUE_EVIDENCE_UNAVAILABLE", by["max_clusters"].blockers)
        self.assertEqual(by["min_clusters"].recommended_config, by["min_clusters"].current_config)
        self.assertIn("QUEUE_EVIDENCE_UNAVAILABLE", by["min_clusters"].blockers)

    def test_each_missing_pressure_field_blocks_the_cuts_that_depend_on_it(self):
        cases = {
            "p95_execution_ms_30": ({"warehouse_size": "LATENCY_EVIDENCE_UNAVAILABLE"}, {"max_clusters", "min_clusters"}),
            "p99_execution_ms_30": ({"warehouse_size": "LATENCY_EVIDENCE_UNAVAILABLE"}, {"max_clusters", "min_clusters"}),
            "spill_query_pct_30": ({"warehouse_size": "SPILL_EVIDENCE_UNAVAILABLE"}, {"max_clusters", "min_clusters"}),
            "queued_query_pct_30": ({k: "QUEUE_EVIDENCE_UNAVAILABLE" for k in ("warehouse_size", "max_clusters", "min_clusters")}, set()),
        }
        for field, (blocked, still_cut) in cases.items():
            with self.subTest(missing=field):
                by = {c.knob: c for c in p.evaluate_warehouses([self._without_pressure_evidence((field,))], self.t)[0].candidates}
                for knob, code in blocked.items():
                    self.assertEqual(by[knob].recommended_config, by[knob].current_config, knob)
                    self.assertIn(code, by[knob].blockers, knob)
                for knob in still_cut:
                    self.assertNotEqual(by[knob].recommended_config, by[knob].current_config, knob)

    def test_no_queueing_does_not_need_a_wait_percentile_for_the_floor_cut(self):
        e = copy.deepcopy(self.evidence[0])
        e.current_epoch.query.update(queued_query_pct_30=0.0, queued_queries_30=0, p95_wait_capacity_ms_when_queued_30=None)
        mn = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["min_clusters"]
        self.assertNotIn("QUEUE_WAIT_EVIDENCE_UNAVAILABLE", mn.blockers)
        self.assertNotEqual(mn.recommended_config, mn.current_config)

    def test_queued_queries_without_wait_evidence_block_the_ceiling_cut(self):
        e = copy.deepcopy(self.evidence[0])
        e.current_epoch.query.update(queued_query_pct_30=0.5, queued_queries_30=40, p95_wait_capacity_ms_when_queued_30=None)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual(by["max_clusters"].recommended_config, by["max_clusters"].current_config)
        self.assertIn("QUEUE_WAIT_EVIDENCE_UNAVAILABLE", by["max_clusters"].blockers)

    def test_elevated_failure_cancel_rate_blocks_size_down(self):
        # Demo warehouse downsizes LARGE->MEDIUM at 0.2% failures; at 8% the "no pressure" evidence is not trustworthy.
        self.assertEqual(self.size_candidate_with_failure_rate(0.2).status, "TEST_REQUIRED")
        c = self.size_candidate_with_failure_rate(8.0)
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertIn("FAILURE_CANCEL_RATE_HIGH", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)

    def test_failure_cancel_threshold_boundary_is_inclusive(self):
        limit = self.t.failure_cancel_pct_downsize_safe
        self.assertEqual(self.size_candidate_with_failure_rate(limit).status, "TEST_REQUIRED")
        self.assertIn("FAILURE_CANCEL_RATE_HIGH", self.size_candidate_with_failure_rate(limit + 0.01).blockers)

    def test_missing_failure_cancel_evidence_fails_closed_for_size_down(self):
        for c in (self.size_candidate_with_failure_rate(None, drop=True), self.size_candidate_with_failure_rate(None)):
            self.assertEqual(c.status, "NO_CHANGE")
            self.assertIn("FAILURE_CANCEL_EVIDENCE_UNAVAILABLE", c.blockers)

    def test_serverless_5x_large_downsize_is_not_priced_from_a_nominal_ratio(self):
        # 5X-Large has no published Serverless DBU/hour. Before: 5X->4X was priced at a guessed 0.5 ratio (~$9,450).
        c = self.candidate_of(self.serverless_size_evidence("5X_LARGE"), "warehouse_size")
        self.assertEqual(c.status, "TEST_REQUIRED")
        self.assertEqual(c.recommended_config, {"warehouse_size": "4X_LARGE"})
        self.assertIsNone(c.estimated_cost_365)
        self.assertIsNone(c.estimated_savings_365)
        self.assertIsNone(c.total_365d_savings_estimate)
        self.assertIsNone(c.dbu_365d_savings_range)
        self.assertEqual(c.metadata["estimate_blocker"], "SERVERLESS_DBU_RATE_NOT_PUBLISHED")
        self.assertNotIn("capacity_ratio_source", c.metadata)
        self.assertEqual(c.metadata["bundle_effect"]["type"], "requires_canary")

    def test_serverless_upsize_into_5x_large_is_not_priced_from_a_nominal_ratio(self):
        ev = self.serverless_size_evidence(
            "4X_LARGE", golden="spill-constrained",
        )
        c = self.candidate_of(ev, "warehouse_size")
        self.assertEqual(c.recommended_config, {"warehouse_size": "5X_LARGE"})
        self.assertIsNone(c.estimated_savings_365)
        self.assertEqual(c.metadata["estimate_blocker"], "SERVERLESS_DBU_RATE_NOT_PUBLISHED")

    def test_serverless_sizes_with_published_rates_are_still_priced(self):
        c = self.candidate_of(self.serverless_size_evidence("4X_LARGE"), "warehouse_size")
        self.assertEqual(c.recommended_config, {"warehouse_size": "3X_LARGE"})
        self.assertGreater(c.estimated_savings_365, 0)
        self.assertEqual(c.metadata["capacity_ratio_source"], "DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR_RATIO")
        self.assertNotIn("estimate_blocker", c.metadata)

    def test_migration_needs_the_cost_history_floor(self):
        floor = self.t.min_economics_cost_history_days
        self.assertEqual(floor, 7)
        short = self.candidate_of(self.with_cost_history(floor - 1), "warehouse_type")
        self.assertEqual(short.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(short.blockers, ["COST_HISTORY_INSUFFICIENT"])
        self.assertEqual(short.recommended_config, short.current_config)
        self.assertIsNone(short.estimated_savings_365)
        ok = self.candidate_of(self.with_cost_history(floor), "warehouse_type")
        self.assertEqual(ok.status, "TEST_REQUIRED")
        self.assertIsNotNone(ok.estimated_savings_365)

    def test_short_cost_history_keeps_the_decision_but_withholds_economics(self):
        # Queue pressure on a 6-day-old epoch: the performance decision stands (it does not rest on cost), the number does not.
        short = self.candidate_of(self.with_cost_history(6, "concurrency-constrained"), "max_clusters")
        full = self.candidate_of(self.with_cost_history(7, "concurrency-constrained"), "max_clusters")
        self.assertEqual(short.status, full.status)
        self.assertEqual(short.recommended_config, full.recommended_config)
        self.assertIsNone(short.estimated_savings_365)
        self.assertIsNone(short.estimated_cost_range_365)
        self.assertEqual(short.metadata["estimate_blocker"], "COST_HISTORY_INSUFFICIENT")
        self.assertEqual(short.metadata["bundle_effect"]["type"], "estimate_withheld")
        self.assertNotIn("estimate_blocker", full.metadata)

    def test_short_cost_history_does_not_touch_no_change_candidates(self):
        for c in p.evaluate_warehouses(self.with_cost_history(3, "newly-changed-config"), self.t)[0].candidates:
            if c.status == "NO_CHANGE":
                self.assertNotIn("estimate_blocker", c.metadata)

    def test_correction_share_does_not_change_decisions(self):
        decisions = []
        for share in (None, 0.0, 0.5):
            pkg = p.build_recommendation_packages([self.with_correction_share(share)], self.t)[0].to_dict()
            decisions.append([(c["candidate_id"], c["knob"], c["status"], c["confidence"], c["recommended_value"], c["independent_economics"]) for c in pkg["candidates"]])
        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual(decisions[0], decisions[2])

    def test_unknown_dbu_baseline_withholds_estimates_and_migration(self):
        e = copy.deepcopy(self.evidence[0])
        e.baseline_forecast_cost_365 = None
        e.current_cost_365 = None
        w = p.evaluate_warehouses([e], self.t)[0]
        by = {c.knob: c for c in w.candidates}
        self.assertEqual(by["warehouse_type"].status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("DBU_COST_UNAVAILABLE", by["warehouse_type"].blockers)
        for c in w.candidates:
            self.assertIsNone(c.dbu_365d_baseline_cost, c.knob)
            if c.status not in {"NO_CHANGE"}:
                self.assertIsNone(c.estimated_savings_range_365, c.knob)
        pkg = p.build_recommendation_packages([e], self.t)[0].to_dict()
        validate_locked_package_schema(pkg)
        self.assertIsNone(pkg["cost_baseline"]["forward_365d"]["dbu_cost"])
        self.assertEqual(pkg["cost_baseline"]["forward_365d"]["availability"], "PARTIAL")

    def test_high_failure_share_blocks_every_capacity_cut(self):
        by = self._with_failure(12.0)
        for knob in ("warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes"):
            with self.subTest(knob=knob):
                self.assertTrue(by[knob].status == "BLOCKED" or by[knob].recommended_config == by[knob].current_config, by[knob].status)
                self.assertIn("FAILURE_CANCEL_RATE_HIGH", by[knob].blockers)

    def test_unknown_failure_share_blocks_every_capacity_cut(self):
        by = self._with_failure(None)
        for knob in ("warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes"):
            with self.subTest(knob=knob):
                self.assertTrue(by[knob].status == "BLOCKED" or by[knob].recommended_config == by[knob].current_config, by[knob].status)
                self.assertIn("FAILURE_CANCEL_EVIDENCE_UNAVAILABLE", by[knob].blockers)

    def test_low_failure_share_does_not_block_cuts(self):
        by = self._with_failure(0.2)
        for knob in ("max_clusters", "auto_stop_minutes"):
            self.assertNotEqual(by[knob].recommended_config, by[knob].current_config, knob)

    def test_max_increase_waits_for_a_mature_epoch_and_a_query_sample(self):
        young = self._queueing_at_ceiling(1, 25)
        self.assertEqual(young.recommended_config, young.current_config)
        self.assertEqual(young.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("CURRENT_CONFIG_EPOCH_TOO_NEW", young.blockers)
        self.assertIn("INSUFFICIENT_QUERY_SAMPLE", young.blockers)

    def test_max_increase_stays_actionable_when_gates_pass(self):
        mature = self._queueing_at_ceiling(30, 12000)
        self.assertEqual(mature.status, "ACTIONABLE")
        self.assertEqual(mature.recommended_config, {"max_clusters": mature.current_config["max_clusters"] + 1})

    def test_auto_stop_fails_closed_without_running_idle_or_event_evidence(self):
        ev = demo_evidence()
        ev[0].current_epoch.gaps = {}  # evidence collected before running-idle evidence existed
        auto = next(c for c in p.evaluate_warehouses(ev, self.t)[0].candidates if c.knob == "auto_stop_minutes")
        self.assertEqual(auto.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(auto.blockers, ["RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE"])
        self.assertIsNone(auto.estimated_savings_365)
        ev = demo_evidence()
        for window in ev[0].current_epoch.gaps["running_idle_windows"].values():
            window["observed_running_cluster_minutes"] = 0  # no running time on the forecast's days
        auto = next(c for c in p.evaluate_warehouses(ev, self.t)[0].candidates if c.knob == "auto_stop_minutes")
        self.assertEqual(auto.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("INSUFFICIENT_EVENT_RESOURCE_EVIDENCE", auto.blockers)

    def test_pro_size_estimator_does_not_reuse_serverless_dbu_curve(self):
        c=self.by_knob["warehouse_size"]
        self.assertNotEqual(c.metadata.get("capacity_ratio_source"),"DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR_RATIO")
        if c.metadata.get("estimate_blocker")!="PRO_CLASSIC_SIZE_RATE_UNVERIFIED":
            self.assertEqual(c.metadata.get("capacity_ratio_source"),"WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR_RATIO")
