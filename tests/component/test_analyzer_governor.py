"""Analyzer -> Governor boundary: every collected warehouse is either eligible or excluded, never both, never lost."""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import save_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts.request import MODE_REPLAY, PublisherRequest, RunContext  # noqa: E402
from publisher.governor import governor  # noqa: E402


class AnalyzerGovernorTests(unittest.TestCase):
    def test_partition_preserves_identity(self):
        fleet = synthetic_demo_evidence(8)
        for ep in fleet[3].epochs:
            ep.config = dataclasses.replace(ep.config, warehouse_type="SERVERLESS_V2")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            save_evidence(path, fleet)
            context = RunContext(PublisherRequest(mode=MODE_REPLAY, output_dir=Path(tmp), evidence_files=(str(path),)), Thresholds())
            analysis = analyzer.run(context)
            governance = governor.run(context, analysis)
        collected = {(e.workspace_id, e.warehouse_id) for e in analysis.evidence}
        eligible = {(w.evidence.workspace_id, w.evidence.warehouse_id) for w in governance.eligible}
        excluded = {(x["workspace_id"], x["warehouse_id"]) for x in governance.exclusions}
        self.assertEqual(eligible | excluded, collected)
        self.assertEqual(eligible & excluded, set())
        self.assertEqual(excluded, {(fleet[3].workspace_id, fleet[3].warehouse_id)})
        self.assertEqual([w.evidence.warehouse_id for w in governance.eligible], [w.evidence.warehouse_id for w in analysis.warehouses])


if __name__ == "__main__":
    unittest.main()
