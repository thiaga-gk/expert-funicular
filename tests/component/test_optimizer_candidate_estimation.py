"""Optimizer -> Candidate Estimator boundary: every decision becomes exactly one candidate with consistent economics."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402


class CandidateEstimationTests(unittest.TestCase):
    def test_candidates_mirror_decisions(self):
        t = Thresholds()
        for w in analyzer.analyze(synthetic_demo_evidence(), t):
            result = optimizer.optimize(w, t)
            for d, c in zip(result.decisions, result.candidates):
                with self.subTest(warehouse=w.evidence.warehouse_id, knob=d.knob):
                    self.assertEqual((c.knob, c.current_config), (d.knob, d.current_config))
                    if c.status == "NO_CHANGE":
                        self.assertIn(c.estimated_savings_365, (None, 0, 0.0))
                    for r in (c.estimated_cost_range_365,):
                        if r is not None:
                            self.assertTrue(0 <= r.low <= r.base <= r.high)


if __name__ == "__main__":
    unittest.main()
