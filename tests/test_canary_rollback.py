"""Fail-fast canary rollback after a canary-type change (one size down, a lower minimum, a higher auto-stop).

Every run compares the new epoch's own evidence with the previous epoch's last 30 days. A failure/cancel rate, queued
share or (after a size-down) spill share above its safe level and above the previous epoch's, or a p95 execution time on
statement sources seen in both epochs more than canary_p95_regression_pct above the previous epoch's (with at least
canary_p95_min_queries such queries), recommends restoring the previous value: ACTIONABLE with
CANARY_GUARDRAIL_BREACHED, first in the plan, no savings claimed. Golden row C55.
"""
import copy
import dataclasses
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402
from contracts import reporter_contract as rc  # noqa: E402


class CanaryRollbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C55")

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def size(self, e):
        return next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "warehouse_size")

    def with_guard(self, **current):
        e = copy.deepcopy(self.e)
        e.current_epoch.query["canary_guard"]["CURRENT"].update(current)
        return e

    def test_c55_p95_regression_after_a_downsize_rolls_back(self):
        # LARGE -> MEDIUM four days ago; dashboards now take 4,000 ms at p95 against 2,500 ms on LARGE (+60%).
        c = self.size(self.e)
        self.assertEqual((c.status, c.current_config["warehouse_size"], c.recommended_config["warehouse_size"]), ("ACTIONABLE", "MEDIUM", "LARGE"))
        self.assertEqual(c.blockers, ["CANARY_GUARDRAIL_BREACHED", "CANARY_P95_REGRESSION"])
        self.assertIn("4,000 ms vs 2,500 ms", c.metadata["rationale"])
        self.assertIsNone(c.estimated_savings_365)  # no saving claimed

    def test_c55_package_puts_the_rollback_first_and_validates(self):
        pkg = p.build_recommendation_packages([self.e], self.t)[0]  # runs semantic and schema validation
        plan = next(s for s in pkg.scenarios if s.scenario_id == pkg.recommended_plan.scenario_id)
        first = plan.execution_plan[0]
        self.assertEqual((first.knob.value, first.recommended_value, first.execution_state.value), ("warehouse_size", "LARGE", "READY"))
        self.assertEqual([(c.knob.value, c.previous_value) for c in pkg.observed_change.changes], [("warehouse_size", "LARGE")])
        self.assertIsNone(plan.portfolio_economics.savings_365d.total)

    def test_contract_requires_the_rollback_to_restore_the_previous_value(self):
        pkg = p.build_recommendation_packages([self.e], self.t)[0]
        bad = [dataclasses.replace(c, recommended_value="X_LARGE") if "CANARY_GUARDRAIL_BREACHED" in c.blockers else c for c in pkg.candidates]
        with self.assertRaises(ValueError):
            dataclasses.replace(pkg, candidates=bad).validate()

    def test_a_p95_rise_within_the_limit_does_not_roll_back(self):
        c = self.size(self.with_guard(p95_execution_ms_shared_sources=3100))  # +24%
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", c.blockers)

    def test_too_few_shared_queries_do_not_roll_back(self):
        c = self.size(self.with_guard(shared_source_queries=self.t.canary_p95_min_queries - 1))
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", c.blockers)

    def test_failures_above_safe_and_above_before_roll_back(self):
        c = self.size(self.with_guard(p95_execution_ms_shared_sources=2500, failure_cancel_pct=6.0))
        self.assertEqual(c.blockers, ["CANARY_GUARDRAIL_BREACHED", "CANARY_FAILURES_ABOVE_SAFE"])

    def test_failures_that_were_already_there_do_not_roll_back(self):
        e = self.with_guard(p95_execution_ms_shared_sources=2500, failure_cancel_pct=6.0)
        e.current_epoch.query["canary_guard"]["PREVIOUS"]["failure_cancel_pct"] = 7.0
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", self.size(e).blockers)

    def test_spill_after_a_downsize_rolls_back(self):
        c = self.size(self.with_guard(p95_execution_ms_shared_sources=2500, spill_query_pct=4.0, spill_queries=40))
        self.assertEqual(c.blockers, ["CANARY_GUARDRAIL_BREACHED", "CANARY_SPILL_AFTER_DOWNSIZE"])

    def test_a_size_increase_is_not_a_canary(self):
        e = copy.deepcopy(self.e)
        prev = next(x for x in e.epochs if not x.is_current)
        prev.config = dataclasses.replace(prev.config, warehouse_size="SMALL")
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", self.size(e).blockers)

    def test_a_raised_auto_stop_and_a_lower_minimum_are_canaries(self):
        e = copy.deepcopy(self.e)
        prev = next(x for x in e.epochs if not x.is_current)
        prev.config = dataclasses.replace(prev.config, warehouse_size="MEDIUM", auto_stop_minutes=10, min_clusters=2)
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, max_clusters=2)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual((by["auto_stop_minutes"].status, by["auto_stop_minutes"].recommended_config["auto_stop_minutes"]), ("ACTIONABLE", 10))
        self.assertEqual((by["min_clusters"].status, by["min_clusters"].recommended_config["min_clusters"]), ("ACTIONABLE", 2))
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", by["warehouse_size"].blockers)


if __name__ == "__main__":
    unittest.main()
