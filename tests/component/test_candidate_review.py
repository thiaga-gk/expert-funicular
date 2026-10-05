"""Optimizer -> Reviewer candidate gate: the whole demo fleet passes and keeps the Optimizer's order."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts.request import MODE_DEMO, PublisherRequest, RunContext  # noqa: E402
from publisher.governor import governor  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402
from publisher.reviewer import reviewer  # noqa: E402


class CandidateReviewTests(unittest.TestCase):
    def test_demo_fleet_passes_the_candidate_gate(self):
        context = RunContext(PublisherRequest(mode=MODE_DEMO, output_dir=Path("."), demo_count=41), Thresholds())
        analysis = analyzer.run(context)
        governance = governor.run(context, analysis)
        optimization = optimizer.run(context, analysis, governance)
        reviewed = reviewer.review_candidates(context, analysis, governance, optimization)
        self.assertEqual([w.evidence.warehouse_id for w in reviewed.accepted], [w.evidence.warehouse_id for w in optimization.warehouses])
        self.assertEqual(len(reviewed.accepted), 41)


if __name__ == "__main__":
    unittest.main()
