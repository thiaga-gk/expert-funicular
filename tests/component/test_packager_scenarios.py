"""Reviewed candidates -> Packager: one package per reviewed warehouse, scenarios and plan consistent with the package."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.component.pipeline import demo_run  # noqa: E402


class PackagerScenarioTests(unittest.TestCase):
    def test_one_valid_package_per_reviewed_warehouse_in_order(self):
        _, _, _, reviewed, packaging = demo_run()
        keys = [(w.evidence.workspace_id, w.evidence.warehouse_id) for w in reviewed.accepted]
        self.assertEqual([(w.workspace_id, w.warehouse_id) for w in packaging.warehouses], keys)
        self.assertEqual([(p.warehouse.workspace_id, p.warehouse.warehouse_id) for p in packaging.packages], keys)
        for p in packaging.packages:
            p.validate()
            self.assertRegex(p.recommendation_package_id, r"^PKG-[0-9A-F]{24}$")

    def test_internal_and_public_scenarios_agree(self):
        _, _, _, _, packaging = demo_run()
        for w, p in zip(packaging.warehouses, packaging.packages):
            self.assertEqual(len(w.scenarios), len(p.scenarios))
            self.assertLessEqual(sum(1 for s in w.scenarios if s.recommended), 1)
            self.assertEqual(w.recommended_scenario is None, w.recommended_scenario_id is None)


if __name__ == "__main__":
    unittest.main()
