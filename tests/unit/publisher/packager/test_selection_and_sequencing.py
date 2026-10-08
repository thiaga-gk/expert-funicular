import dataclasses
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.packager.selection import _overall_status  # noqa: E402
from publisher.reviewer.reviewer import Reviewer  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402


class OverallStatusTests(unittest.TestCase):
    """The package status follows the recommended plan, not every candidate (the migration experiment is always a canary)."""

    @classmethod
    def setUpClass(cls):
        import logging
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.pkg = p.build_recommendation_packages(demo_evidence(), cls.t)[0]
        cls.fleet = p.build_recommendation_packages(synthetic_demo_evidence(), cls.t)

    @classmethod
    def tearDownClass(cls):
        import logging
        logging.disable(logging.NOTSET)

    def with_plans(self, plan_a_steps=None, plan_b_total="keep"):
        scenarios = []
        for s in self.pkg.scenarios:
            if s.status == rc.ScenarioStatus.RECOMMENDED and plan_a_steps is not None:
                s = dataclasses.replace(s, execution_plan=plan_a_steps)
            if s.status == rc.ScenarioStatus.ALTERNATIVE and plan_b_total != "keep":
                econ = s.portfolio_economics
                s = dataclasses.replace(s, portfolio_economics=dataclasses.replace(econ, savings_365d=dataclasses.replace(econ.savings_365d, total=plan_b_total)))
            scenarios.append(s)
        return _overall_status(self.pkg.candidates, scenarios)

    def test_recommended_plan_with_steps_is_optimization_available(self):
        self.assertEqual(self.pkg.assessment.overall_status, rc.AssessmentStatus.OPTIMIZATION_AVAILABLE)

    def test_empty_plan_with_priced_positive_experiment_needs_review(self):
        self.assertEqual(self.with_plans([], rc.Range3(-500.0, 1200.0, 4000.0)), rc.AssessmentStatus.REVIEW_REQUIRED)

    def test_empty_plan_with_negative_or_unpriced_experiment_is_not_an_optimization(self):
        for total in (rc.Range3(-3000.0, -1443.0, -200.0), None):
            status = self.with_plans([], total)
            self.assertNotIn(status, {rc.AssessmentStatus.OPTIMIZATION_AVAILABLE, rc.AssessmentStatus.REVIEW_REQUIRED}, total)

    def test_incompatible_experiment_is_not_a_reason_to_review(self):
        e = demo_evidence()[0]
        e.compatibility_evidence = {"mode": "B_READ_ONLY_API", "status": "AVAILABLE", "enable_serverless_compute": False,
                                    "external_legacy_hive_metastore_detected": True}
        pkg = p.build_recommendation_packages([e], self.t)[0]
        plan_b = next(s for s in pkg.scenarios if s.status == rc.ScenarioStatus.ALTERNATIVE)
        self.assertEqual(plan_b.compatibility.status, rc.CompatibilityStatus.INCOMPATIBLE)
        migration = [x for x in plan_b.execution_plan if x.candidate_id]
        self.assertEqual([x.execution_state for x in migration], [rc.ExecutionState.BLOCKED])
        scenarios = [dataclasses.replace(s, execution_plan=[]) if s.status == rc.ScenarioStatus.RECOMMENDED else s for s in pkg.scenarios]
        self.assertGreater(plan_b.portfolio_economics.savings_365d.total.base, 0)
        self.assertNotEqual(_overall_status(pkg.candidates, scenarios), rc.AssessmentStatus.REVIEW_REQUIRED)

    def test_fleet_status_matches_the_recommended_plan(self):
        for pkg in self.fleet:
            plan = next(s for s in pkg.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED)
            if not plan.execution_plan:
                self.assertNotEqual(pkg.assessment.overall_status, rc.AssessmentStatus.OPTIMIZATION_AVAILABLE, pkg.warehouse.warehouse_name)
        self.assertTrue(any(not next(s for s in pkg.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED).execution_plan for pkg in self.fleet))


class PlanOrderAndInvalidationTests(unittest.TestCase):
    """Only true invalidations hold a step; steps run in order of value, never before their invalidator."""

    def setUp(self):
        self.t = Thresholds()

    def plan(self, evidence):
        w = p.evaluate_warehouses(evidence, self.t)[0]
        plan_a = next(s for s in w.scenarios if s.recommended)
        return w, plan_a

    def test_only_true_invalidations_remain(self):
        w, _ = self.plan(demo_evidence())
        by = {c.knob: c for c in w.candidates}
        self.assertEqual(sorted(by["warehouse_size"].invalidates), ["max_clusters", "min_clusters"])
        self.assertEqual(by["max_clusters"].invalidates, [])  # a ceiling cut above the peak changes nothing observed
        self.assertEqual(by["min_clusters"].invalidates, [])
        self.assertEqual(by["auto_stop_minutes"].dependencies, [])

    def test_plan_runs_by_value_and_respects_invalidations(self):
        # BI - Sales: auto-stop 10,933 x 0.93 / 1 ranks first; min (6,074 x 0.78 / 2) outranks size (5,345 x 0.78 / 2)
        # but size invalidates it, so size comes first; the zero-value ceiling cut comes last.
        _, plan_a = self.plan(demo_evidence())
        self.assertEqual([s.knob for s in plan_a.steps], ["auto_stop_minutes", "warehouse_size", "min_clusters", "max_clusters"])
        self.assertEqual([s.execution_state for s in plan_a.steps], ["READY_FOR_HITL", "READY_FOR_CANARY", "PENDING_REEVALUATION", "PENDING_REEVALUATION"])
        pe = plan_a.portfolio_estimate
        self.assertGreater(pe.total_365d_savings_range.base, 13000)  # auto-stop + size are both authoritative now

    def test_auto_stop_then_min_never_removes_more_than_recorded(self):
        e = demo_evidence()[0]
        _, plan_a = self.plan([e])
        contingent = plan_a.portfolio_estimate.contingent
        self.assertIsNotNone(contingent)
        # With the overlap rule the floor cluster's idle minutes that auto-stop already removed are not removed again:
        # the pending min-cluster cut adds far less than its standalone saving (about 1,520 of 6,074).
        w, _ = self.plan([e])
        standalone = next(c for c in w.candidates if c.knob == "min_clusters").estimated_savings_365
        added = contingent.estimated_savings_range_365.base - plan_a.portfolio_estimate.estimated_savings_range_365.base
        self.assertLess(added, 0.5 * standalone)

    def test_reviewer_rejects_a_step_before_its_invalidator(self):
        w, plan_a = self.plan(demo_evidence())
        steps = list(plan_a.steps)
        size = next(i for i, s in enumerate(steps) if s.knob == "warehouse_size")
        mn = next(i for i, s in enumerate(steps) if s.knob == "min_clusters")
        steps[size], steps[mn] = steps[mn], steps[size]
        broken = dataclasses.replace(plan_a, steps=steps)
        others = [s for s in w.scenarios if not s.recommended]
        with self.assertRaises(ValueError):
            Reviewer(self.t).review_scenarios(demo_evidence()[0], w.candidates, [broken] + others, broken.scenario_id)

    def test_risk_thresholds_default(self):
        self.assertEqual((self.t.plan_risk_hitl_change, self.t.plan_risk_canary_change), (1.0, 2.0))
