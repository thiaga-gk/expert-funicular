"""Warehouse-type migration priced at the target's own best auto-stop, and held when even its best case loses money."""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
