"""Warehouse-type migration priced at the target's own best auto-stop, and held when even its best case loses money."""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402


class MigrationOwnOptimumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.fleet = {e.current_epoch.tags["golden_scenario"]: e for e in p.synthetic_demo_evidence()}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def migration(self, e):
        return next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "warehouse_type")

    def test_mostly_idle_pro_warehouse_shows_serverless_as_strongly_positive(self):
        # C51: about 95% of running time idle at auto-stop 120. Serverless at its own 5-minute auto-stop runs ~8% of
        # today's cluster-minutes, so even with its higher DBU price it saves most of the bill (was negative).
        c = self.migration(self.fleet["C51"])
        r = c.total_365d_savings_range
        self.assertGreater(r.base, 0.8 * c.total_365d_baseline_cost)
        self.assertLess(r.low, r.base)  # a real range, not a collapsed point
        self.assertEqual(c.metadata["migration_model"]["resource_model"]["target_auto_stop_option"]["target"], 5)

    def test_always_on_and_nightly_warehouses_are_priced_at_the_target_optimum(self):
        for scenario in ("C01", "C05", "C53"):
            rm = self.migration(self.fleet[scenario]).metadata["migration_model"]["resource_model"]
            self.assertLess(rm["target_running_fraction"], 0.75, scenario)

    def test_migration_whose_best_case_loses_money_is_held(self):
        e = copy.deepcopy(self.fleet["C45"])
        e.sql_sku_prices["PREMIUM_SQL_SERVERLESS_COMPUTE"]["effective_list_price"] = 5.0  # a price no saving survives
        c = self.migration(e)
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertIn("MIGRATION_NOT_COST_EFFECTIVE", c.blockers)
        self.assertLess(c.metadata["value_gate"]["modeled_savings_range_365"]["high"], 0)
        w = p.evaluate_warehouses([e], self.t)[0]
        self.assertFalse([s for s in w.scenarios if any(st.knob == "warehouse_type" for st in s.steps)])


class MigrationTargetBudgetTests(unittest.TestCase):
    def test_target_auto_stop_respects_the_target_cold_start_budget(self):
        # Serverless at 30 -> Pro: every lower Pro value adds restarts for 50% of queries (over the 2% budget), so the
        # Pro alternative keeps 30 or raises it, instead of being priced at a lower value the policy rejects.
        logging.disable(logging.CRITICAL)
        try:
            e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C02")
            q = e.current_epoch.gaps["running_idle_queries"]
            for t in p.auto_stop_candidates(p.Thresholds(), "PRO"):
                if t < 30:
                    row = e.current_epoch.gaps["running_idle_by_target"].setdefault(str(t), dict.fromkeys(p.RUNNING_IDLE_TARGET_FIELDS, 0))
                    row["additional_starts"] = q // 2
            a = p.Analyzer(p.Thresholds()).analyze(e)["warehouse_type"].facts
        finally:
            logging.disable(logging.NOTSET)
        self.assertGreaterEqual(a["migration_target_option"]["target"], 30)  # never a lower value over the budget


class MigrationTargetRestartTests(unittest.TestCase):
    def test_pro_target_pays_its_own_restarts(self):
        # Serverless at 5 restarting for most queries: the Pro alternative keeps paying ~4 cluster-minutes per restart
        # it still has (only 0.1 today), so its running time is above today's even before idle is added.
        logging.disable(logging.CRITICAL)
        try:
            e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C02")
            a = p.Analyzer(p.Thresholds()).analyze(e)["warehouse_type"].facts
        finally:
            logging.disable(logging.NOTSET)
        g = e.current_epoch.gaps
        o = a["migration_target_option"]
        restarts = g["restarts_for_queries"] + o["added_starts"]
        self.assertGreater(restarts, 0)
        self.assertGreaterEqual(o["target_running_cluster_minutes"], g["observed_running_cluster_minutes"] - o["gain_cluster_minutes"] + restarts * 3.9 - 1e-6)


class MigrationBasisTests(unittest.TestCase):
    def test_today_basis_carries_today_restarts_at_today_cost(self):
        # Measured running time excludes starting time, so the fraction is target cost over (today's running + today's
        # restarts at today's restart cost), never today's running minus restarts it never contained.
        logging.disable(logging.CRITICAL)
        try:
            e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C49")
            a = p.Analyzer(p.Thresholds()).analyze(e)["warehouse_type"].facts
        finally:
            logging.disable(logging.NOTSET)
        g = e.current_epoch.gaps
        basis = g["observed_running_cluster_minutes"] + g["restarts_for_queries"] * 4.0
        self.assertAlmostEqual(a["migration_running_fraction"], round(a["migration_target_option"]["target_running_cluster_minutes"] / basis, 6), places=6)


if __name__ == "__main__":
    unittest.main()
