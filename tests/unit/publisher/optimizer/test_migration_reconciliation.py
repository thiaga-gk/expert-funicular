"""Migration is priced relative to billing, and only when the cost model reproduces the current bill.

The target cost is modeled from running time x DBU/hour x price. The same formula applied to the current warehouse must
land within 0.7-1.3x of its billed DBU forecast; otherwise the model is not trusted and the estimate is withheld.
Inside the band the target is scaled by billed / modeled, so systematic model error cancels out.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402


def migration(evidence, scenario, t):
    for w in p.evaluate_warehouses(evidence, t):
        e = next(x for x in evidence if x.warehouse_id == w.warehouse_id and x.workspace_id == w.workspace_id)
        if e.current_epoch.tags["golden_scenario"] == scenario:
            return {c.knob: c for c in w.candidates}["warehouse_type"]


class MigrationReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.fleet = synthetic_demo_evidence()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_reconciled_model_is_priced_relative_to_billing(self):
        c = migration(self.fleet, "C01", self.t)  # Pro -> Serverless, billing follows the model
        model = c.metadata["migration_model"]
        self.assertTrue(model["available"])
        self.assertLess(abs(model["reconciliation_ratio"] - 1.0), 0.05)
        self.assertIsNotNone(c.estimated_savings_365)

    def test_unreconciled_model_withholds_the_migration_estimate(self):
        fleet = copy.deepcopy(self.fleet)
        e = next(x for x in fleet if x.current_epoch.tags["golden_scenario"] == "C01")
        for d in e.current_epoch.resource_daily:  # events show a third of the running time the bill pays for
            for k in ("uptime_minutes", "cluster_minutes", "minutes_at_min", "minutes_at_max"):
                d[k] = d[k] / 3.0
        c = migration(fleet, "C01", self.t)
        self.assertIsNone(c.estimated_savings_365)
        self.assertEqual(c.metadata["estimate_blocker"], "MIGRATION_MODEL_NOT_RECONCILED")
        ratio = c.metadata["migration_model"]["reconciliation_ratio"]
        self.assertLess(ratio, self.t.migration_reconciliation_min)


if __name__ == "__main__":
    unittest.main()
