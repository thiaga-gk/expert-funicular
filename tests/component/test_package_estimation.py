"""Package -> Package Estimator: the package economics are the scenario envelope, never a sum or a probability interval."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.component.pipeline import demo_run  # noqa: E402


class PackageEstimationTests(unittest.TestCase):
    def test_package_savings_are_the_envelope_of_its_scenarios(self):
        _, _, _, _, packaging = demo_run()
        checked = 0
        for p in packaging.packages:
            econ = p.to_dict()["package_economics"]
            self.assertFalse(econ["semantics"]["is_additive"])
            self.assertFalse(econ["semantics"]["is_probability_interval"])
            env, savings = econ["scenario_envelope"], econ["estimated_savings_365d"]
            if savings is None or env is None or env.get("recommended_base") is None:
                continue
            self.assertEqual(savings["base"], env["recommended_base"]["savings_365d"]["total"])
            self.assertEqual(savings["low"], env["minimum_savings"]["savings_365d"]["total"])
            self.assertEqual(savings["high"], env["maximum_savings"]["savings_365d"]["total"])
            self.assertLessEqual(savings["low"], savings["base"])
            self.assertLessEqual(savings["base"], savings["high"])
            checked += 1
        self.assertGreater(checked, 10)


if __name__ == "__main__":
    unittest.main()
