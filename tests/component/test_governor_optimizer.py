"""Governor -> Optimizer boundary: the Optimizer considers exactly the eligible warehouses, in the Governor's order."""
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts.analysis import FleetAnalysis  # noqa: E402
from publisher.contracts.request import MODE_DEMO, PublisherRequest, RunContext  # noqa: E402
from publisher.governor import governor  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402


class GovernorOptimizerTests(unittest.TestCase):
    def test_only_eligible_warehouses_are_optimized(self):
        t = Thresholds()
        fleet = synthetic_demo_evidence(6)
        for ep in fleet[0].epochs:
            ep.config = dataclasses.replace(ep.config, warehouse_type="SERVERLESS_V2")
        analysis = FleetAnalysis(fleet, analyzer.analyze(fleet, t))
        context = RunContext(PublisherRequest(mode=MODE_DEMO, output_dir=Path("."), demo_count=6, batch_size=2), t)
        governance = governor.run(context, analysis)
        result = optimizer.run(context, analysis, governance)
        self.assertEqual([w.evidence.warehouse_id for w in result.warehouses], [w.evidence.warehouse_id for w in governance.eligible])
        self.assertNotIn(fleet[0].warehouse_id, {w.evidence.warehouse_id for w in result.warehouses})
        for w in result.warehouses:
            self.assertTrue(all(c.workspace_id == w.evidence.workspace_id and c.warehouse_id == w.evidence.warehouse_id for c in w.candidates))


if __name__ == "__main__":
    unittest.main()
