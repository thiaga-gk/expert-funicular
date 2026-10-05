"""Plan B: the migration branch is named after the type it evaluated, and its compatibility follows the checks.

A migration that was never evaluated (too little evidence) has no target: Plan B says so and runs no checks against
the warehouse's own type. A migration held because it does not pay keeps the evaluated target in its name. A migration
whose every compatibility check passes is compatible (no separate validation step); an empty Plan B is never marked
compatible, so its scenario ID cannot equal an empty Plan A's. Sizes without a published DBU rate (5X-Large) are never
priced, and an upsize to one is a canary at most.
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


class PlanBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        fleet = p.synthetic_demo_evidence()
        cls.by = {e.current_epoch.tags["golden_scenario"]: e for e in fleet}
        # The whole fleet at once: workspace-level evidence (other warehouses' types) feeds the compatibility checks.
        cls.warehouses = {w.warehouse_id: w for w in p.evaluate_warehouses(fleet, cls.t)}
        cls.packages = {pkg.warehouse.warehouse_id: pkg.to_dict() for pkg in p.build_recommendation_packages(fleet, cls.t)}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def package(self, e):
        return self.packages[e.warehouse_id]

    def plan_b(self, pkg):
        return next(s for s in pkg["scenarios"] if not s["scenario_id"] == pkg["recommended_plan"]["scenario_id"] and s["scenario_name"].startswith("Plan B"))

    def test_migration_not_evaluated_names_no_target_and_runs_no_checks(self):
        e = self.by["C19"]  # Pro, configuration too new: migration INSUFFICIENT_EVIDENCE
        pkg = self.package(e)
        b = self.plan_b(pkg)
        self.assertEqual(b["scenario_name"], "Plan B — Migration not evaluated")
        self.assertEqual(b["compatibility"]["status"], "UNKNOWN")
        self.assertEqual(b["compatibility"]["checks"], [])
        self.assertEqual(b["execution_plan"], [])

    def test_held_migration_keeps_the_evaluated_target(self):
        e = self.by["C10"]  # Serverless: the Pro migration was evaluated and held as not cost-effective
        w = self.warehouses[e.warehouse_id]
        mig = next(c for c in w.candidates if c.knob == "warehouse_type")
        self.assertIn("MIGRATION_NOT_COST_EFFECTIVE", mig.blockers)
        b = next(s for s in w.scenarios if s.name.startswith("Plan B"))
        self.assertEqual(b.name, "Plan B — Pro migration experiment")
        self.assertEqual(b.compatibility["target_warehouse_type"], "PRO")
        self.assertEqual(b.steps, [])

    def test_fully_passing_compatibility_is_compatible_without_a_validation_step(self):
        pkg = self.package(self.by["C02"])  # Serverless -> Pro canary; every Pro check passes
        b = self.plan_b(pkg)
        self.assertEqual({c["status"] for c in b["compatibility"]["checks"]}, {"PASS"})
        self.assertEqual(b["compatibility"]["status"], "COMPATIBLE")
        self.assertNotIn("COMPATIBILITY_VALIDATION", [s["step_type"] for s in b["execution_plan"]])
        self.assertEqual(b["scenario_type"], "WAREHOUSE_TYPE_MIGRATION")

    def test_unknown_check_still_requires_validation(self):
        pkg = self.package(self.by["C01"])  # Pro -> Serverless with two UNKNOWN checks
        b = self.plan_b(pkg)
        self.assertEqual(b["compatibility"]["status"], "REQUIRES_VALIDATION")
        self.assertEqual(b["execution_plan"][0]["step_type"], "COMPATIBILITY_VALIDATION")

    def test_empty_plan_b_never_shares_the_empty_plan_a_scenario_id(self):
        # Every check of the held Pro migration passes, and Plan A is empty too: the two IDs must still differ.
        pkg = self.package(self.by["C10"])
        ids = [s["scenario_id"] for s in pkg["scenarios"]]
        self.assertEqual(len(ids), len(set(ids)))
        b = self.plan_b(pkg)
        self.assertNotEqual(b["compatibility"]["status"], "COMPATIBLE")

    def test_upsize_to_5x_large_is_an_unpriced_canary(self):
        e = copy.deepcopy(self.by["C07"])  # broad spill: upsize
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, warehouse_size="4X_LARGE")
        c = next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "warehouse_size")
        self.assertEqual((c.status, c.recommended_config["warehouse_size"]), ("TEST_REQUIRED", "5X_LARGE"))
        self.assertIsNone(c.estimated_savings_365)
        self.assertEqual(c.metadata["estimate_blocker"], "PRO_CLASSIC_SIZE_RATE_UNVERIFIED")

    def test_5x_large_warehouse_changes_are_never_priced_from_a_guessed_rate(self):
        e = copy.deepcopy(self.by["C06"])  # over-sized: downsize canary
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, warehouse_size="5X_LARGE")
        by_knob = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}
        self.assertEqual(by_knob["warehouse_size"].recommended_config["warehouse_size"], "4X_LARGE")
        self.assertIsNone(by_knob["warehouse_size"].estimated_savings_365)
        self.assertIsNone(by_knob["warehouse_type"].estimated_savings_365)  # no published Serverless 5X-Large rate


if __name__ == "__main__":
    unittest.main()
