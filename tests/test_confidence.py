"""Candidate confidence measures the evidence for the decision itself (LLD section 16, PRD FR-CONF-005).

The sample component uses the sample each knob decides on (non-cache queries for size, busy minutes for the ceiling,
busy periods for the floor, queries over the running-idle window for auto-stop, 30-day queries for migration); a
candidate that changes nothing is never HIGH and one without enough evidence is LOW; a scenario with no steps has no
score (UNKNOWN).
"""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402


class ConfidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        fleet = p.synthetic_demo_evidence()
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
        # Mostly idle: 88 queries in 30 days, but the auto-stop decision is measured on 1,044 over its idle window.
        c = self.cands["C51"]["auto_stop_minutes"]
        self.assertEqual(c.confidence["components"]["sample"], 1.0)
        self.assertEqual((c.confidence["score"], c.confidence["level"]), (0.93, "HIGH"))

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
        a = p.Analyzer(self.t).analyze(e)["warehouse_size"]
        self.assertEqual(p.Estimator(self.t)._confidence(e, a)["components"]["sample"], 0.0)

    def test_empty_scenario_has_no_score(self):
        pkg = self.packages["C19"]  # configuration too new: Plan A has no steps
        plan_a = next(s for s in pkg["scenarios"] if s["scenario_id"] == pkg["recommended_plan"]["scenario_id"])
        self.assertEqual(plan_a["execution_plan"], [])
        self.assertEqual((plan_a["confidence"]["score"], plan_a["confidence"]["level"]), (None, "UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
