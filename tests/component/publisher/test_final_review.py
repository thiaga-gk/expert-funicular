"""Package -> Reviewer package gate: every demo package passes unchanged and in order."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.component.publisher.pipeline import demo_run  # noqa: E402
from publisher.reviewer import reviewer  # noqa: E402


class FinalReviewTests(unittest.TestCase):
    def test_package_gate_passes_the_demo_fleet_unchanged(self):
        context, analysis, governance, _, packaging = demo_run()
        reviewed = reviewer.review_package(context, analysis, governance, packaging)
        self.assertEqual([p.recommendation_package_id for p in reviewed.packages], [p.recommendation_package_id for p in packaging.packages])
        self.assertEqual(len(reviewed.packages), 41)


if __name__ == "__main__":
    unittest.main()
