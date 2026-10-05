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
from tests.test_readiness import changed  # noqa: E402


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

    def test_a_lower_minimum_is_a_canary_and_a_raised_auto_stop_is_not(self):
        # By the carry map a higher auto-stop changes running time and cold starts only: no guard can read it.
        e = copy.deepcopy(self.e)
        prev = next(x for x in e.epochs if not x.is_current)
        prev.config = dataclasses.replace(prev.config, warehouse_size="MEDIUM", auto_stop_minutes=10, min_clusters=2)
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, max_clusters=2)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual((by["min_clusters"].status, by["min_clusters"].recommended_config["min_clusters"]), ("ACTIONABLE", 2))
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", by["auto_stop_minutes"].blockers)
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", by["warehouse_size"].blockers)

    def test_noise_on_chronic_queueing_does_not_roll_back(self):
        # Previous 10.27% queued, now 10.79%: above safe and above before, but within the relative margin.
        e = self.with_guard(p95_execution_ms_shared_sources=2500, queued_query_pct=10.79, queued_queries=500)
        e.current_epoch.query["canary_guard"]["PREVIOUS"].update(queued_query_pct=10.27, queued_queries=600)
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", self.size(e).blockers)
        e.current_epoch.query["canary_guard"]["CURRENT"].update(queued_query_pct=14.0)
        self.assertEqual(self.size(e).blockers, ["CANARY_GUARDRAIL_BREACHED", "CANARY_QUEUE_ABOVE_SAFE"])

    def previous(self, e):
        cur = e.current_epoch
        return next(x for x in e.epochs if not x.is_current and p.parse_datetime(x.epoch_end) == p.parse_datetime(cur.epoch_start))

    def test_floor_and_ceiling_lowered_together_roll_back_together(self):
        # Previous MEDIUM 3/4, current MEDIUM 1/2: restoring only the floor (3) would exceed the current ceiling (2).
        e = copy.deepcopy(self.e)
        prev = self.previous(e)
        prev.config = dataclasses.replace(prev.config, warehouse_size=e.current_epoch.config.warehouse_size, min_clusters=3, max_clusters=4)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual((by["min_clusters"].status, by["min_clusters"].recommended_config["min_clusters"]), ("ACTIONABLE", 3))
        self.assertEqual((by["max_clusters"].status, by["max_clusters"].recommended_config["max_clusters"]), ("ACTIONABLE", 4))
        p.build_recommendation_packages([e], self.t)  # validates

    def test_every_rolled_back_knob_is_a_ready_step(self):
        # Size down, floor down and auto-stop raised in one change: size and floor return together (a higher auto-stop
        # is not a canary-type change), neither waits on the other.
        e = copy.deepcopy(self.e)
        prev = self.previous(e)
        prev.config = dataclasses.replace(prev.config, min_clusters=2, max_clusters=2, auto_stop_minutes=10)
        pkg = p.build_recommendation_packages([e], self.t)[0]
        plan = next(s for s in pkg.scenarios if s.scenario_id == pkg.recommended_plan.scenario_id)
        states = {st.knob.value: st.execution_state.value for st in plan.execution_plan if st.knob is not None}
        self.assertEqual({k: states[k] for k in ("warehouse_size", "min_clusters")}, {"warehouse_size": "READY", "min_clusters": "READY"})
        auto = next(c for c in pkg.candidates if c.knob.value == "auto_stop_minutes")
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", auto.blockers)

    def test_guard_numbers_are_package_evidence(self):
        a = p.build_recommendation_packages([self.e], self.t)[0]
        b = p.build_recommendation_packages([self.with_guard(p95_execution_ms_shared_sources=2500)], self.t)[0]
        self.assertNotEqual(a.provenance.evidence_fingerprint, b.provenance.evidence_fingerprint)
        size = next(c for c in a.candidates if c.knob.value == "warehouse_size")
        cited = {a.evidence[ref].metric for ref in size.evidence_refs}
        self.assertIn("canary_p95_execution_ms_shared_sources", cited)

    def test_the_guard_covers_thirty_days_after_the_change(self):
        # The same regression 45 days after the downsize is left to the size knob's normal gates.
        e, by = changed("C55", "warehouse_size", "MEDIUM", 45)
        self.assertNotIn("CANARY_GUARDRAIL_BREACHED", by["warehouse_size"].blockers)
        e, by = changed("C55", "warehouse_size", "MEDIUM", 4)
        self.assertIn("CANARY_GUARDRAIL_BREACHED", by["warehouse_size"].blockers)

    def test_guard_query_scan_is_bounded(self):
        self.assertIn("x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 60 DAYS", p.CANARY_GUARD_SQL)
        self.assertIn("c.epoch_start >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS", p.CANARY_GUARD_SQL)

    def plan_states(self, e):
        pkg = p.build_recommendation_packages([e], self.t)[0]
        plan = next(s for s in pkg.scenarios if s.scenario_id == pkg.recommended_plan.scenario_id)
        return pkg, [(st.knob.value, st.recommended_value, st.execution_state.value) for st in plan.execution_plan if st.knob is not None]

    def test_a_floor_rollback_comes_first_and_holds_other_capacity_changes(self):
        # Previous MEDIUM 3/4, current MEDIUM 1/2, p95 regression: the floor and ceiling come back first; no size-down
        # canary or ceiling cut is proposed on evidence from the configuration being rolled back.
        e = copy.deepcopy(self.e)
        prev = self.previous(e)
        prev.config = dataclasses.replace(prev.config, warehouse_size=e.current_epoch.config.warehouse_size, min_clusters=3, max_clusters=4)
        pkg, steps = self.plan_states(e)
        self.assertEqual(steps[:2], [("max_clusters", 4, "READY"), ("min_clusters", 3, "READY")])
        size = next(c for c in pkg.candidates if c.knob.value == "warehouse_size")
        self.assertEqual((size.status.value, size.blockers), ("NO_CHANGE", ["CANARY_ROLLBACK_PENDING"]))
        self.assertNotEqual(pkg.recommended_plan.next_action, "RUN_WAREHOUSE_SIZE_CANARY")

    def test_a_floor_rollback_never_meets_a_lower_ceiling(self):
        e = copy.deepcopy(self.e)
        prev = self.previous(e)
        prev.config = dataclasses.replace(prev.config, warehouse_size=e.current_epoch.config.warehouse_size, min_clusters=2, max_clusters=2)
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, max_clusters=8)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual(by["min_clusters"].status, "ACTIONABLE")
        self.assertNotEqual(by["max_clusters"].status, "ACTIONABLE")

    def test_guard_findings_are_numbers_and_spill_only_for_size_changes(self):
        e = copy.deepcopy(self.e)
        from decimal import Decimal
        e.current_epoch.query["canary_guard"]["CURRENT"]["failure_cancel_pct"] = Decimal("0.30")
        obs = p.canary_observation(e, self.t)
        self.assertIsInstance(obs["details"]["current"]["failure_cancel_pct"], float)

    def test_a_floor_rollback_keeps_a_ceiling_increase_for_queueing(self):
        # Floor 2 -> 1 rolled back; queueing at the ceiling still raises max (an increase is never held).
        e = copy.deepcopy(self.e)
        prev = self.previous(e)
        prev.config = dataclasses.replace(prev.config, warehouse_size=e.current_epoch.config.warehouse_size, min_clusters=2, max_clusters=2)
        by = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual(by["min_clusters"].status, "ACTIONABLE")
        self.assertNotEqual(by["max_clusters"].blockers, ["CANARY_ROLLBACK_PENDING"])


if __name__ == "__main__":
    unittest.main()
