"""Scenario -> Scenario Estimator: stateful economics are non-additive and internally consistent."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.component.publisher.pipeline import demo_run  # noqa: E402


class ScenarioEstimationTests(unittest.TestCase):
    def test_savings_are_baseline_minus_estimated_cost_and_never_additive(self):
        _, _, _, _, packaging = demo_run()
        checked = 0
        for p in packaging.packages:
            for s in p.to_dict()["scenarios"]:
                econ = s["portfolio_economics"]
                self.assertFalse(econ["is_additive"])
                base, cost, saving = econ["baseline_365d"]["total_cost"], econ["estimated_cost_365d"], econ["savings_365d"]
                if base is None or cost is None or saving is None or cost["total"] is None or saving["total"] is None:
                    continue
                for bound in ("low", "base", "high"):
                    self.assertAlmostEqual(saving["total"][bound], base - cost["total"][{"low": "high", "base": "base", "high": "low"}[bound]], delta=0.02)
                checked += 1
        self.assertGreater(checked, 20)


if __name__ == "__main__":
    unittest.main()
