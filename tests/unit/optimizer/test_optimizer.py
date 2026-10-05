"""Optimizer facade (src/publisher/optimizer): one decision per active knob, technique order, batching, conflicts."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS  # noqa: E402
from publisher.config.thresholds import Thresholds  # noqa: E402
from publisher.optimizer import conflicts, optimizer  # noqa: E402


class OptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = Thresholds()
        cls.analyzed = analyzer.analyze(synthetic_demo_evidence(), cls.t)

    def test_one_decision_and_candidate_per_active_knob_in_technique_order(self):
        for w in self.analyzed:
            result = optimizer.optimize(w, self.t)
            self.assertEqual([d.knob for d in result.decisions], list(ACTIVE_KNOBS))
            self.assertEqual([c.knob for c in result.candidates], list(ACTIVE_KNOBS))
            self.assertIs(result.evidence, w.evidence)

    def test_batching_keeps_order_and_results(self):
        one = optimizer.optimize_eligible(self.analyzed, self.t, batch_size=1)
        many = optimizer.optimize_eligible(self.analyzed, self.t, batch_size=1000)
        self.assertEqual([w.evidence.warehouse_id for w in one.warehouses], [w.evidence.warehouse_id for w in self.analyzed])
        self.assertEqual([[c.recommendation_id for c in w.candidates] for w in one.warehouses],
                         [[c.recommendation_id for c in w.candidates] for w in many.warehouses])

    def test_conflicts_leave_decisions_alone_without_a_tripped_canary_guard(self):
        w = next(x for x in self.analyzed if not (x.analyses["warehouse_size"].facts.get("canary") or {}).get("guards"))
        out = optimizer.Optimizer(self.t).optimize(w.evidence, w.analyses)
        before = copy.deepcopy(out)
        conflicts.apply_canary_rollback(optimizer.Optimizer(self.t), w.evidence, w.analyses, w.evidence.current_epoch.config, False, 0, out)
        self.assertEqual(out, before)


if __name__ == "__main__":
    unittest.main()
