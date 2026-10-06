import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.reviewer.reviewer import Reviewer, review_plans  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class ReviewerCases(PublisherCase):
    def test_reviewer_gate_2_rejects_pending_steps_priced_as_authoritative(self):
        w = copy.deepcopy(self.w)
        plan = next(s for s in w.scenarios if s.recommended)
        next(x for x in plan.steps if x.execution_state == "PENDING_REEVALUATION").pricing_basis = "AUTHORITATIVE"
        with self.assertRaisesRegex(ValueError, "pricing basis"):
            Reviewer(self.t).review_scenarios(self.evidence[0], w.candidates, w.scenarios, plan.scenario_id)

    def test_reviewer_gate_2_rejects_contingent_upside_leaking_into_authoritative_savings(self):
        w = copy.deepcopy(self.w)
        plan = next(s for s in w.scenarios if s.recommended)
        plan.portfolio_estimate.estimated_savings_range_365 = plan.portfolio_estimate.contingent.estimated_savings_range_365
        with self.assertRaisesRegex(ValueError, "Authoritative scenario savings"):
            Reviewer(self.t).review_scenarios(self.evidence[0], w.candidates, w.scenarios, plan.scenario_id)

    def test_a_gate_rejection_is_logged_as_a_warning_with_the_rule_and_reraised(self):
        w = copy.deepcopy(self.w)
        plan = next(s for s in w.scenarios if s.recommended)
        plan.portfolio_estimate.estimated_savings_range_365 = plan.portfolio_estimate.contingent.estimated_savings_range_365
        with self.assertLogs("publisher", "WARNING") as logs, self.assertRaises(ValueError):
            review_plans(SimpleNamespace(thresholds=self.t), [w])
        line = logs.output[0]
        self.assertIn("reviewer.gate.reject", line)
        self.assertIn("gate=scenario", line)
        self.assertIn("violation=Authoritative scenario savings", line)

    def test_reviewer_gate_2_accepts_the_unmodified_plan(self):
        plan = next(s for s in self.w.scenarios if s.recommended)
        Reviewer(self.t).review_scenarios(self.evidence[0], self.w.candidates, self.w.scenarios, plan.scenario_id)

    def test_reviewer_rejects_cross_workspace_candidate(self):
        candidates = list(self.w.candidates)
        bad = copy.deepcopy(candidates[0])
        bad.workspace_id = "wrong"
        candidates[0] = bad
        with self.assertRaises(ValueError):
            Reviewer(self.t).review(self.evidence[0], candidates)
