"""Validation guardrails from the package (src/realizer/governor/policy.py validate_performance, readiness,
canary_window_open).

The package's cost profile sets the limits (balanced: p95 +25 percent, aggressive +40, conservative +15; cold-start
budget 2 / 10 / 1 percent of queries), with the Publisher canary guard's two-part rule: a metric fails only above its
safe level AND above the value before the change times (1 + limit). The Realizer policy can only tighten the p95 limit.
A rule without enough evidence is UNKNOWN, which keeps the measurement not ready, never failed. A canary-type step stays
provisional (not claimable, recognition PROVISIONAL) for the Publisher's 30-day guard window.
"""
import copy
import unittest
from decimal import Decimal

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.collector.collector import demo_observation
from realizer.analyzer.projector import project_observation
from realizer.governor.policy import RealizerPolicy, canary_window_open, readiness, validate_performance

PRO = world.package("autostop-scoped-pro")
SIZE = world.package("inplace-size-canary")
GUARD = {"queries": "1000", "failure_cancel_pct": "0.3", "queued_queries": "0", "queued_query_pct": "0", "decision_queries": "900",
         "spill_queries": "0", "spill_query_pct": "0", "shared_source_queries": "900", "p95_execution_ms_shared_sources": "4000"}


def live(knob="warehouse_size", pre=None, post=None, cold=None, warehouse_type="PRO"):
    """A measured observation's guard inputs."""
    return {"config": {"warehouse_type": warehouse_type}, "measurement": {"knob": knob},
            "performance": {"normalized_p95_runtime_regression_pct": None, "reliability_pass": None, "correctness_pass": None,
                            "guards": {"pre": {**GUARD, **(pre or {})}, "post": {**GUARD, **(post or {})}, "cold_start": cold}}}


def caller(p95="2", reliability=True, correctness=True):
    return {"performance": {"normalized_p95_runtime_regression_pct": p95, "reliability_pass": reliability, "correctness_pass": correctness, "guards": None}}


def results(observation, package=SIZE, policy=None):
    out = validate_performance(observation, policy or RealizerPolicy(), package)
    return out["state"], {r["rule_id"]: r["result"] for r in out["rules"]}


def profiled(package, **thresholds):
    p = copy.deepcopy(package)
    p["cost_profile"]["applied_thresholds"]["cross_technique"].update(thresholds)
    return p


class CallerSuppliedTests(unittest.TestCase):
    def test_p95_limit_is_the_packages_profile_and_the_policy_can_only_tighten_it(self):
        self.assertEqual(results(caller(p95="12")), ("PASSED", {"REAL-PERF-P95-001": "PASS", "REAL-REL-001": "PASS", "REAL-CORRECT-001": "PASS"}))
        self.assertEqual(results(caller(p95="26"))[0], "FAILED")  # balanced: 25
        self.assertEqual(results(caller(p95="30"), profiled(SIZE, canary_p95_regression_pct=40.0))[0], "PASSED")  # aggressive
        self.assertEqual(results(caller(p95="16"), profiled(SIZE, canary_p95_regression_pct=15.0))[0], "FAILED")  # conservative
        self.assertEqual(results(caller(p95="12"), policy=RealizerPolicy(max_normalized_p95_runtime_regression_pct=Decimal("10")))[0], "FAILED")
        self.assertEqual(results(caller(p95="12"), policy=RealizerPolicy(max_normalized_p95_runtime_regression_pct=Decimal("60")))[0], "PASSED")  # never looser

    def test_unknown_is_not_failed_and_keeps_the_measurement_not_ready(self):
        for obs in (caller(p95=None), caller(reliability=None)):
            with self.subTest(obs=obs):
                verdict = validate_performance(obs, RealizerPolicy(), SIZE)
                self.assertEqual(verdict["state"], "UNKNOWN")
                self.assertFalse(verdict["hard_guardrails_pass"])
        base = demo_observation(SIZE)
        self.assertTrue(readiness(base, RealizerPolicy(), validate_performance(base, RealizerPolicy(), SIZE))["ready"])
        unknown = {**base, "performance": {**base["performance"], "reliability_pass": None}}
        ready = readiness(unknown, RealizerPolicy(), validate_performance(unknown, RealizerPolicy(), SIZE))
        self.assertFalse(ready["ready"])
        self.assertEqual(ready["checks"]["guardrail_evidence"], {"unknown_rules": ["REAL-REL-001"], "pass": False})
        self.assertEqual(results(caller(reliability=False))[0], "FAILED")
        self.assertEqual(results(caller(correctness=None))[0], "PASSED")  # correctness fails only on evidence of a failure


class MeasuredGuardTests(unittest.TestCase):
    def test_a_size_canary_fails_only_above_the_safe_level_and_the_margin(self):
        self.assertEqual(results(live())[0], "PASSED")
        # p95 +30 percent on the same sources: above balanced 25 -> FAIL; aggressive 40 -> PASS.
        self.assertEqual(results(live(post={"p95_execution_ms_shared_sources": "5200"}))[1]["REAL-P95-001"], "FAIL")
        self.assertEqual(results(live(post={"p95_execution_ms_shared_sources": "5200"}), profiled(SIZE, canary_p95_regression_pct=40.0))[0], "PASSED")
        # Failures 4 percent (below the 5 percent safe level) pass even at ten times before; 6 percent at 2x fail.
        self.assertEqual(results(live(pre={"failure_cancel_pct": "0.4"}, post={"failure_cancel_pct": "4"}))[1]["REAL-FAIL-001"], "PASS")
        self.assertEqual(results(live(pre={"failure_cancel_pct": "3"}, post={"failure_cancel_pct": "6"}))[1]["REAL-FAIL-001"], "FAIL")
        # Queueing above the safe 0.5 percent and above before x 1.25, with at least 20 queued queries.
        self.assertEqual(results(live(post={"queued_query_pct": "3", "queued_queries": "30"}))[1]["REAL-QUEUE-001"], "FAIL")
        self.assertEqual(results(live(post={"queued_query_pct": "3", "queued_queries": "5"}))[1]["REAL-QUEUE-001"], "PASS")  # too few to matter
        self.assertEqual(results(live(post={"spill_query_pct": "2", "spill_queries": "25"}))[1]["REAL-SPILL-001"], "FAIL")

    def test_too_small_a_sample_is_unknown_for_rates_over_all_queries(self):
        state, rules = results(live(post={"shared_source_queries": "250"}))
        self.assertEqual((state, rules["REAL-P95-001"]), ("UNKNOWN", "UNKNOWN"))  # the p95 guard needs 300 queries
        self.assertEqual(results(live(post={"queries": "60"}))[1]["REAL-FAIL-001"], "UNKNOWN")
        self.assertEqual(results(live(post={"p95_execution_ms_shared_sources": None}))[1]["REAL-P95-001"], "UNKNOWN")

    def test_auto_stop_is_judged_on_failures_and_added_cold_starts(self):
        within = live("auto_stop_minutes", cold={"pre_restarts": 280, "pre_queries": 280, "post_restarts": 280, "post_queries": 280})
        self.assertEqual(results(within, PRO), ("PASSED", {"REAL-FAIL-001": "PASS", "REAL-COLD-001": "PASS"}))
        # 10 more restarts per 280 queries: +3.6 percent of queries against the balanced 2 percent budget.
        over = live("auto_stop_minutes", cold={"pre_restarts": 100, "pre_queries": 280, "post_restarts": 110, "post_queries": 280})
        self.assertEqual(results(over, PRO)[1]["REAL-COLD-001"], "FAIL")
        aggressive = copy.deepcopy(PRO)
        aggressive["cost_profile"]["applied_thresholds"]["auto_stop"]["cold_start_added_share_max_pct"] = 10.0
        self.assertEqual(results(over, aggressive)[1]["REAL-COLD-001"], "PASS")
        self.assertEqual(results(live("auto_stop_minutes", cold={"pre_restarts": 1, "pre_queries": 2, "post_restarts": 40, "post_queries": 40},
                                      warehouse_type="SERVERLESS"), PRO)[1]["REAL-COLD-001"], "PASS")  # no budget for Serverless
        self.assertEqual(results(live("auto_stop_minutes", cold=None), PRO)[1]["REAL-COLD-001"], "UNKNOWN")

    def test_the_simulated_change_passes_its_guardrails_and_is_ready(self):
        obs = project_observation(PRO, world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13))
        verdict = validate_performance(obs, RealizerPolicy(), PRO)
        self.assertEqual(verdict["state"], "PASSED")
        self.assertTrue(readiness(obs, RealizerPolicy(), verdict)["ready"])


class CanaryWindowTests(unittest.TestCase):
    def test_a_canary_step_is_provisional_for_thirty_days(self):
        canary, ready = {"execution_state": "READY_FOR_CANARY"}, {"execution_state": "READY"}
        self.assertTrue(canary_window_open(canary, {"elapsed_days": 29}))
        self.assertFalse(canary_window_open(canary, {"elapsed_days": 30}))
        self.assertFalse(canary_window_open(ready, {"elapsed_days": 8}))


if __name__ == "__main__":
    unittest.main()
