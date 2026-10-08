"""Reviewer candidate gate (src/publisher/reviewer/candidate.py): fails fast on broken candidate invariants."""
import copy
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402
from publisher.reviewer.reviewer import Reviewer  # noqa: E402


class CandidateGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = Thresholds()
        w = analyzer.analyze(synthetic_demo_evidence(1), cls.t)[0]
        cls.result = optimizer.optimize(w, cls.t)

    def candidates(self):
        return copy.deepcopy(self.result.candidates)

    def assertRejected(self, candidates, message):
        with self.assertRaisesRegex(ValueError, message):
            Reviewer(self.t).review(self.result.evidence, candidates)

    def test_valid_candidates_pass(self):
        Reviewer(self.t).review(self.result.evidence, self.candidates())

    def test_one_candidate_per_active_knob(self):
        self.assertRejected(self.candidates()[:-1], "Expected exactly one candidate per in-scope knob")

    def test_identity_must_match_the_warehouse(self):
        c = self.candidates()
        c[0] = dataclasses.replace(c[0], warehouse_id="other")
        self.assertRejected(c, "Candidate identity mismatch")

    def test_duplicate_ids_are_rejected(self):
        c = self.candidates()
        c[1] = dataclasses.replace(c[1], recommendation_id=c[0].recommendation_id)
        self.assertRejected(c, "Duplicate recommendation id")

    def test_confidence_must_be_a_probability(self):
        c = self.candidates()
        c[0] = dataclasses.replace(c[0], confidence={**c[0].confidence, "score": 1.5})
        self.assertRejected(c, "Confidence out of range")

    def test_blocked_needs_blockers(self):
        c = self.candidates()
        c[0] = dataclasses.replace(c[0], status="BLOCKED", blockers=[])
        self.assertRejected(c, "BLOCKED without blockers")


if __name__ == "__main__":
    unittest.main()
