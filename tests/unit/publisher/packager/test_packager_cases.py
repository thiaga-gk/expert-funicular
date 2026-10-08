import copy
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.contracts.scenario import PlanStep  # noqa: E402
from publisher.packager.adapter import compute_reevaluation  # noqa: E402
from publisher.packager.estimator.scenario import StatefulBundleEstimator  # noqa: E402
from publisher.packager.scenarios import ScenarioPlanner  # noqa: E402
from tests.golden.publisher import load_case  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class PackagerCases(PublisherCase):
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

    def test_bundle_prices_min_cluster_with_the_same_realization_as_the_candidate(self):
        c = self.by_knob["min_clusters"]
        step = PlanStep(1, c.recommendation_id, "min_clusters", c.current_config, c.recommended_config,
                          c.status, "A_B_CANARY", 7, [], [])
        port = StatefulBundleEstimator(self.t).estimate(self.evidence[0], [step], {c.recommendation_id: c})
        got, want = port.estimated_savings_range_365, c.estimated_savings_range_365
        for g, w in ((got.low, want.low), (got.base, want.base), (got.high, want.high)):
            self.assertAlmostEqual(g, w, delta=1.0)

    def test_plan_a_authoritative_savings_stop_at_the_first_invalidating_step(self):
        plan = self.plan_a()
        states = {x.knob: x.execution_state for x in plan.steps}
        self.assertEqual(states["auto_stop_minutes"], "READY_FOR_HITL")  # independent of size: never held
        self.assertEqual(states["warehouse_size"], "READY_FOR_CANARY")
        for knob in ("max_clusters", "min_clusters"):
            self.assertEqual(states[knob], "PENDING_REEVALUATION")
        size = next(x for x in plan.steps if x.knob == "warehouse_size")
        auth = plan.portfolio_estimate.estimated_savings_range_365
        # The authoritative figure is the running total through the size step; pending steps add nothing to it.
        self.assertAlmostEqual(auth.base, size.running_estimated_savings_range_365.base, places=2)
        self.assertAlmostEqual(auth.high, size.running_estimated_savings_range_365.high, places=2)

    def test_pending_steps_are_priced_as_a_labeled_contingent_upside(self):
        plan = self.plan_a()
        basis = {x.knob: x.pricing_basis for x in plan.steps}
        for knob in ("auto_stop_minutes", "warehouse_size"):
            self.assertEqual(basis[knob], "AUTHORITATIVE")
        for knob in ("max_clusters", "min_clusters"):
            self.assertEqual(basis[knob], "CONTINGENT_PENDING_REEVALUATION")
        contingent = plan.portfolio_estimate.contingent
        self.assertIsNotNone(contingent)
        last = plan.steps[-1]
        self.assertAlmostEqual(contingent.estimated_savings_range_365.base, last.running_estimated_savings_range_365.base, places=2)
        # The big-picture upside is larger than the authoritative base, and clearly separate from it.
        self.assertGreater(contingent.estimated_savings_range_365.base, plan.portfolio_estimate.estimated_savings_range_365.base)
        self.assertEqual(contingent.estimate_status, "PROVISIONAL_MODELED_RANGE")

    def test_authoritative_chain_skips_pending_effects_even_when_a_later_step_is_ready(self):
        pending, pc = self.step_for("min_clusters", "PENDING_REEVALUATION")
        ready, rcand = self.step_for("auto_stop_minutes", "READY_FOR_HITL")
        by_id = {pc.recommendation_id: pc, rcand.recommendation_id: rcand}
        est = StatefulBundleEstimator(self.t)
        both = est.estimate(self.evidence[0], [pending, ready], by_id)
        only_ready = est.estimate(self.evidence[0], [self.step_for("auto_stop_minutes", "READY_FOR_HITL")[0]], by_id)
        # Authoritative: auto-stop is priced on the *unchanged* floor because the min-cluster step is pending.
        self.assertAlmostEqual(both.estimated_savings_range_365.base, only_ready.estimated_savings_range_365.base, places=2)
        # Contingent: the full chain, where min-cluster consumed minutes first, so auto-stop has less left to remove.
        self.assertIsNotNone(both.contingent)
        self.assertNotAlmostEqual(both.contingent.estimated_savings_range_365.base, both.estimated_savings_range_365.base, places=2)

    def test_no_contingent_economics_when_nothing_is_pending(self):
        ready, c = self.step_for("auto_stop_minutes", "READY_FOR_HITL")
        port = StatefulBundleEstimator(self.t).estimate(self.evidence[0], [ready], {c.recommendation_id: c})
        self.assertIsNone(port.contingent)
        self.assertEqual(ready.pricing_basis, "AUTHORITATIVE")
        plan_b = next(s for s in self.w.scenarios if s.name.startswith("Plan B"))
        self.assertIsNone(plan_b.portfolio_estimate.contingent)

    def test_unpriceable_pending_step_does_not_poison_authoritative_economics(self):
        ready, rcand = self.step_for("auto_stop_minutes", "READY_FOR_HITL")
        # A pending step whose effect only a canary can measure withholds only the contingent chain.
        photon = PlanStep(2, "REC-UNMEASURABLE", "max_clusters", {"max_clusters": 4}, {"max_clusters": 3},
                            "TEST_REQUIRED", "GATE", 0, [], [], "PENDING_REEVALUATION")
        photon_c = copy.deepcopy(self.by_knob["max_clusters"])
        photon_c.recommendation_id = "REC-UNMEASURABLE"
        photon_c.metadata = {**photon_c.metadata, "bundle_effect": {"type": "requires_canary"}}
        by_id = {rcand.recommendation_id: rcand, "REC-UNMEASURABLE": photon_c}
        port = StatefulBundleEstimator(self.t).estimate(self.evidence[0], [ready, photon], by_id)
        only_ready = StatefulBundleEstimator(self.t).estimate(self.evidence[0], [self.step_for("auto_stop_minutes", "READY_FOR_HITL")[0]], by_id)
        self.assertEqual(port.estimate_status, only_ready.estimate_status)
        self.assertAlmostEqual(port.estimated_savings_range_365.base, only_ready.estimated_savings_range_365.base, places=2)
        self.assertIsNone(port.contingent)  # contingent cannot be priced, so it is withheld rather than guessed
        self.assertIsNone(photon.running_estimated_savings_range_365)

    def test_scenario_contract_and_order(self):
        self.assertEqual([s.name for s in self.w.scenarios], [
            "Plan A — Optimize current warehouse type", "Plan B — Serverless migration experiment"
        ])
        plan_a = next(s for s in self.w.scenarios if s.name.startswith("Plan A"))
        plan_b = next(s for s in self.w.scenarios if s.name.startswith("Plan B"))
        self.assertTrue(plan_a.recommended)
        self.assertEqual([x.knob for x in plan_a.steps], ["auto_stop_minutes", "warehouse_size", "min_clusters", "max_clusters"])
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

    def test_unknown_cv_packages_validate(self):
        self.evidence[0].cost_cv_84 = None
        pkgs = p.build_recommendation_packages(self.evidence, self.t)
        self.assertTrue(pkgs)

    def test_unpriceable_serverless_size_step_withholds_scenario_economics(self):
        ev = self.serverless_size_evidence("5X_LARGE")
        w = p.evaluate_warehouses(ev, self.t)[0]
        scenario = next(s for s in w.scenarios if any(st.knob == "warehouse_size" for st in s.steps))
        self.assertEqual(scenario.portfolio_estimate.estimate_status, "REQUIRES_CANARY")
        self.assertIsNone(scenario.portfolio_estimate.total_365d_savings_range)
        self.assertIsNone(scenario.portfolio_estimate.dbu_365d_savings_range)
        self.assertTrue(p.build_recommendation_packages(ev, self.t))

    def test_withheld_serverless_size_candidate_is_published_as_unavailable(self):
        pkg = p.build_recommendation_packages(self.serverless_size_evidence("5X_LARGE"), self.t)[0]
        size = next(c for c in pkg.candidates if c.knob.value == "warehouse_size")
        self.assertEqual(size.status.value, "TEST_REQUIRED")
        self.assertEqual(size.independent_economics.estimation_status.value, "UNAVAILABLE")
        self.assertIsNone(size.independent_economics.savings_365d.total)

    def test_short_cost_history_withholds_scenario_economics_and_packages_validate(self):
        ev = self.with_cost_history(6, "concurrency-constrained")
        w = p.evaluate_warehouses(ev, self.t)[0]
        plan_a = next(s for s in w.scenarios if s.steps and any(st.knob == "max_clusters" for st in s.steps))
        self.assertEqual(plan_a.portfolio_estimate.estimate_status, "REQUIRES_COST_HISTORY")
        self.assertIsNone(plan_a.portfolio_estimate.total_365d_savings_range)
        pkg = p.build_recommendation_packages(ev, self.t)[0]
        size = next(c for c in pkg.candidates if c.knob.value == "max_clusters")
        self.assertEqual(size.independent_economics.estimation_status.value, "UNAVAILABLE")

    def test_correction_share_is_package_evidence(self):
        pkg = p.build_recommendation_packages([self.with_correction_share(0.02)], self.t)[0].to_dict()
        found = [x for x in pkg["evidence"].values() if x["metric"] == "billing_correction_dbu_share_365d"]
        # Package evidence only: candidates do not reference it, so it cannot move candidate IDs.
        for c in pkg["candidates"]:
            self.assertNotIn(found[0]["evidence_id"], c["evidence_refs"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["value"], 0.02)
        self.assertEqual(found[0]["source"], "system.billing.usage")
        self.assertEqual(found[0]["source_field"], "record_type")
        unknown = p.build_recommendation_packages([self.with_correction_share(None)], self.t)[0].to_dict()
        self.assertIsNone(next(x for x in unknown["evidence"].values() if x["metric"] == "billing_correction_dbu_share_365d")["value"])

    def test_reevaluation_method_names_the_settle_buffer(self):
        e = copy.deepcopy(self.evidence[0])
        e.cost_history_days = 3
        win = compute_reevaluation(e, self.t, start_time="2026-09-29T00:00:00Z", epoch_age=2)
        self.assertIn("billing ingestion lag", win.method)
        self.assertIn("next settled day", win.method)

    def test_single_step_auto_stop_scenario_matches_the_candidate(self):
        # Running idle worth 90% of all cluster-minutes is priced in full (it is measured running time, so no cap), and
        # the one-step plan claims exactly what its own step does.
        # (Idle above 100% of running time is contradictory evidence and is not priced at all: tests/unit/publisher/optimizer/test_auto_stop_evidence.)
        for multiple in (0.2, 0.9):
            cand, plan = self.auto_stop_only(multiple)
            self.assertAlmostEqual(plan.portfolio_estimate.dbu_365d_savings_range.base, cand.dbu_365d_savings_estimate, places=2, msg=multiple)
            self.assertAlmostEqual(plan.portfolio_estimate.dbu_365d_savings_range.high, cand.dbu_365d_savings_range.high, places=2, msg=multiple)
        uncapped, _ = self.auto_stop_only(0.9)
        self.assertAlmostEqual(uncapped.metadata["gain_share"], 0.9, places=6)

    def test_guardrail_changes_with_no_direct_savings_are_labeled_no_delta_modeled(self):
        # Demo max-cluster tightening (4 -> 3) is a guardrail: it carries no priced delta, so it is not a "modeled range".
        pkg = self.packages[0]
        mx = next(c for c in pkg.candidates if c.knob.value == "max_clusters")
        self.assertEqual(mx.status.value, "ACTIONABLE")
        self.assertEqual(mx.independent_economics.estimation_status.value, "NO_DELTA_MODELED")
        self.assertEqual(mx.independent_economics.savings_365d.total.base, 0)

    def test_priced_changes_keep_their_modeled_label(self):
        up = self.increase_candidate_package()
        self.assertEqual(up.status.value, "ACTIONABLE")
        self.assertEqual(up.independent_economics.estimation_status.value, "MODELED_RANGE")
        for c in self.packages[0].candidates:
            if c.knob.value in {"auto_stop_minutes", "min_clusters", "warehouse_size"} and c.status.value in {"ACTIONABLE", "TEST_REQUIRED"}:
                self.assertNotEqual(c.independent_economics.estimation_status.value, "NO_DELTA_MODELED", c.knob.value)

    def test_planner_ready_for_hitl_maps_explicitly_to_contract_ready(self):
        payload = load_case("concurrency-constrained")
        ev = [evidence_from_dict(payload["evidence"][0])]
        w = p.evaluate_warehouses(ev, self.t)[0]
        internal = {st.knob: st.execution_state for s in w.scenarios for st in s.steps}
        self.assertEqual(internal["max_clusters"], "READY_FOR_HITL")
        pkg = p.build_recommendation_packages(ev, self.t)[0]
        states = {st.knob.value: st.execution_state.value for sc in pkg.scenarios for st in sc.execution_plan if getattr(st, "knob", None)}
        self.assertEqual(states["max_clusters"], "READY")

    def test_unknown_planner_execution_state_fails_loudly_instead_of_becoming_ready(self):
        payload = load_case("concurrency-constrained")
        ev = [evidence_from_dict(payload["evidence"][0])]
        real = ScenarioPlanner.scenarios
        def corrupt(planner, e, candidates, scope, materiality_floor=None):
            scenarios, recommended = real(planner, e, candidates, scope, materiality_floor)
            scenarios[0].steps[0].execution_state = "NOT_A_STATE"
            return scenarios, recommended
        with patch.object(ScenarioPlanner, "scenarios", corrupt):
            with self.assertRaises(ValueError) as ctx:
                p.build_recommendation_packages(ev, self.t)
        self.assertIn("NOT_A_STATE", str(ctx.exception))

    def test_unpriced_alternative_is_not_blamed_on_the_recommended_scenario(self):
        e, scenarios, priced, unpriced = self.envelope_cases()
        why, econ = self.reasons(e, scenarios, priced)
        self.assertEqual(why, {unpriced.scenario_id: "SCENARIO_ECONOMICS_UNAVAILABLE"})
        self.assertEqual(econ.included_scenario_ids, [priced.scenario_id])

    def test_envelope_stays_empty_when_the_recommended_scenario_is_unpriced(self):
        e, scenarios, priced, unpriced = self.envelope_cases()
        why, econ = self.reasons(e, scenarios, unpriced)
        self.assertEqual(why, {
            unpriced.scenario_id: "SCENARIO_ECONOMICS_UNAVAILABLE",
            priced.scenario_id: "RECOMMENDED_SCENARIO_ECONOMICS_UNAVAILABLE",
        })
        self.assertIsNone(econ.estimated_savings_365d)
        self.assertEqual(econ.included_scenario_ids, [])

    def test_priced_scenario_with_no_recommendation_gets_its_own_reason(self):
        e, scenarios, priced, unpriced = self.envelope_cases()
        why, econ = self.reasons(e, scenarios, None)
        self.assertEqual(why[priced.scenario_id], "NO_RECOMMENDED_SCENARIO")
        self.assertEqual(why[unpriced.scenario_id], "SCENARIO_ECONOMICS_UNAVAILABLE")
