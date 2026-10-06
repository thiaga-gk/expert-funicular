"""Candidate confidence measures the evidence for the decision itself (LLD section 16, PRD FR-CONF-005).

The sample component uses the sample each knob decides on (non-cache queries for size, busy minutes for the ceiling,
busy periods for the floor, queries in the running-idle basis window for auto-stop, 30-day queries for migration); a
candidate that changes nothing is never HIGH and one without enough evidence is LOW; a scenario with no steps has no
score (UNKNOWN).
"""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.findings import Analyzer  # noqa: E402
from publisher.analyzer.metrics import running_idle_rates  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.optimizer.estimator.candidate import Estimator  # noqa: E402


class ConfidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        fleet = synthetic_demo_evidence()
        cls.by = {e.current_epoch.tags["golden_scenario"]: e for e in fleet}
        scenario_of = {e.warehouse_id: e.current_epoch.tags["golden_scenario"] for e in fleet}
        cls.cands = {scenario_of[w.warehouse_id]: {c.knob: c for c in w.candidates} for w in p.evaluate_warehouses(fleet, cls.t)}
        cls.packages = {scenario_of[pkg.warehouse.warehouse_id]: pkg.to_dict() for pkg in p.build_recommendation_packages(fleet, cls.t)}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_policy_values(self):
        self.assertEqual(self.t.confidence_cap_default, 0.79)
        self.assertEqual(self.t.confidence_cap_insufficient_evidence, 0.59)
        self.assertEqual(self.t.confidence_strong_busy_minutes, 1000)
        self.assertEqual(self.t.confidence_strong_busy_periods, 100)

    def test_caps_follow_the_cut_offs_and_weights_must_sum_to_one(self):
        import dataclasses
        moved = dataclasses.replace(self.t, confidence_high_threshold=0.85, confidence_medium_threshold=0.65)
        self.assertEqual((moved.confidence_cap_default, moved.confidence_cap_insufficient_evidence,
                          moved.confidence_cap_serverless_ceiling_increase), (0.84, 0.64, 0.64))
        with self.assertRaises(ValueError):
            dataclasses.replace(self.t, confidence_base_weight=0.2)

    def test_no_change_is_never_high_and_insufficient_evidence_is_low(self):
        for scenario, by_knob in self.cands.items():
            for c in by_knob.values():
                with self.subTest(scenario=scenario, knob=c.knob):
                    if c.status not in {"ACTIONABLE", "TEST_REQUIRED"}:
                        self.assertNotEqual(c.confidence["level"], "HIGH")
                    if c.status == "INSUFFICIENT_EVIDENCE":
                        self.assertEqual(c.confidence["level"], "LOW")

    def test_month_end_migration_is_priced_from_its_billing_cycles(self):
        # One billed day a month: the forward cost comes from whole billing cycles, which have no day-to-day
        # stability figure, so confidence rests on coverage, sample, maturity and signal.
        c = self.cands["C52"]["warehouse_type"]
        self.assertEqual((c.status, c.confidence["score"], c.confidence["level"]), ("TEST_REQUIRED", 0.78, "MEDIUM"))
        self.assertIsNone(c.confidence["components"]["stability"])

    def test_auto_stop_sample_is_its_idle_window(self):
        # Mostly idle: 88 queries in 30 days, but the auto-stop decision is measured on the queries of its basis window
        # (the forecast's last 84 days), so the sample earns more than the 30-day count would.
        c = self.cands["C51"]["auto_stop_minutes"]
        basis = running_idle_rates(self.by["C51"], self.t)["basis_queries"]
        self.assertGreater(basis, 88)
        self.assertEqual(c.confidence["components"]["sample"], round(min(1.0, basis / self.t.strong_query_sample_size), 3))
        self.assertGreater(c.confidence["components"]["sample"], 88 / self.t.strong_query_sample_size)

    def test_floor_sample_is_busy_periods(self):
        c = self.cands["C10"]["min_clusters"]  # 23 busy periods
        self.assertEqual(c.confidence["components"]["sample"], 0.23)
        self.assertEqual(self.cands["C52"]["min_clusters"].confidence["level"], "LOW")  # one busy period: month-end

    def test_ceiling_sample_is_busy_minutes(self):
        self.assertEqual(self.cands["C52"]["max_clusters"].confidence["components"]["sample"], 0.72)  # 720 busy minutes

    def test_size_sample_excludes_result_cache_hits(self):
        e = self.by["C21"]  # result-cache-heavy BI
        decision = e.current_epoch.query["decision_queries_30"]
        self.assertLess(decision, e.current_epoch.query["queries_30"])
        self.assertEqual(self.cands["C21"]["warehouse_size"].confidence["components"]["sample"], round(min(1.0, decision / 500), 3))

    def test_unknown_sample_earns_no_credit(self):
        import copy
        e = copy.deepcopy(self.by["C06"])
        e.current_epoch.query["decision_queries_30"] = None
        a = Analyzer(self.t).analyze(e)["warehouse_size"]
        self.assertEqual(Estimator(self.t)._confidence(e, a)["components"]["sample"], 0.0)

    def test_empty_scenario_has_no_score(self):
        pkg = self.packages["C19"]  # configuration too new: Plan A has no steps
        plan_a = next(s for s in pkg["scenarios"] if s["scenario_id"] == pkg["recommended_plan"]["scenario_id"])
        self.assertEqual(plan_a["execution_plan"], [])
        self.assertEqual((plan_a["confidence"]["score"], plan_a["confidence"]["level"]), (None, "UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
