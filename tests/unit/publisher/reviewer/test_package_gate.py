"""Reviewer package gate (src/publisher/reviewer/package.py, scenario.py): fails fast on broken packages and scenarios."""
import copy
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests.component.publisher.pipeline import demo_run  # noqa: E402
from publisher.reviewer.package import review_contract, validate_locked_package_schema  # noqa: E402
from publisher.reviewer.reviewer import Reviewer  # noqa: E402


class PackageGateTests(unittest.TestCase):
    def setUp(self):
        context, _, _, _, packaging = demo_run()
        self.t = context.thresholds
        self.packaging = packaging

    def test_every_demo_package_passes(self):
        for p in self.packaging.packages:
            review_contract(p)

    def test_a_package_whose_id_does_not_match_its_content_is_rejected(self):
        p = self.packaging.packages[0]
        tampered = dataclasses.replace(p, recommendation_package_id="PKG-" + "0" * 24)
        with self.assertRaises(Exception):
            review_contract(tampered)

    def test_schema_rejects_a_payload_missing_a_required_field(self):
        payload = self.packaging.packages[0].to_dict()
        del payload["package_economics"]
        with self.assertRaises(Exception):
            validate_locked_package_schema(payload)

    def test_two_recommended_scenarios_are_rejected(self):
        w = next(x for x in self.packaging.warehouses if len(x.scenarios) >= 2)
        scenarios = copy.deepcopy(w.scenarios)
        for s in scenarios:
            s.recommended = True
        with self.assertRaises(ValueError):
            Reviewer(self.t).review_scenarios(w.evidence, w.candidates, scenarios, scenarios[0].scenario_id)


if __name__ == "__main__":
    unittest.main()
