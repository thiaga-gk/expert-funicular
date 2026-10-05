"""Analyzer facade (src/publisher/analyzer/analyzer.py): fail-closed type gate, calibration scope, order, request modes."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import save_evidence  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS  # noqa: E402
from publisher.config.thresholds import Thresholds  # noqa: E402
from publisher.contracts.request import MODE_DEMO, MODE_REPLAY, PublisherRequest, RunContext  # noqa: E402
from publisher.contracts.source import WarehouseConfig  # noqa: E402

CALIBRATION_KEYS = (
    "workspace_serverless_observed_from_system_compute_warehouses",
    "workspace_pro_classic_observed_from_system_compute_warehouses",
    "workspace_pro_dbu_per_cluster_hour_by_size",
)


def _unsupported(e, kind="LAKEHOUSE_REAL_TIME"):
    for ep in e.epochs:
        ep.config = WarehouseConfig(kind, ep.config.warehouse_channel, ep.config.warehouse_size,
                                    ep.config.min_clusters, ep.config.max_clusters, ep.config.auto_stop_minutes)
    return e


class AnalyzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = synthetic_demo_evidence(6)

    def test_every_supported_warehouse_gets_one_analysis_per_active_knob_in_deterministic_order(self):
        fleet = copy.deepcopy(self.fleet)
        result = analyzer.analyze(list(reversed(fleet)), Thresholds())
        keys = [(w.evidence.workspace_id, w.evidence.warehouse_name.lower(), w.evidence.warehouse_id) for w in result]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(result), len(fleet))
        for w in result:
            self.assertEqual(set(w.analyses), set(ACTIVE_KNOBS))

    def test_unsupported_types_are_never_analyzed(self):
        fleet = copy.deepcopy(self.fleet)
        _unsupported(fleet[2])
        result = analyzer.analyze(fleet, Thresholds())
        self.assertNotIn(fleet[2].warehouse_id, {w.evidence.warehouse_id for w in result})
        self.assertEqual(len(result), len(fleet) - 1)
        # Not calibrated either: the excluded warehouse's evidence is left as collected.
        self.assertFalse(any(k in fleet[2].billing_features for k in CALIBRATION_KEYS))

    def test_calibration_covers_supported_warehouses_only(self):
        fleet = copy.deepcopy(self.fleet)
        baseline = copy.deepcopy(fleet)
        analyzer.analyze(baseline[:3] + baseline[4:], Thresholds())
        _unsupported(fleet[3])
        analyzer.analyze(fleet, Thresholds())
        for got, want in zip(fleet[:3] + fleet[4:], baseline[:3] + baseline[4:]):
            self.assertEqual({k: got.billing_features[k] for k in CALIBRATION_KEYS},
                             {k: want.billing_features[k] for k in CALIBRATION_KEYS})


class RunTests(unittest.TestCase):
    def test_demo_and_replay_produce_the_same_analysis(self):
        t = Thresholds()
        with tempfile.TemporaryDirectory() as tmp:
            demo = analyzer.run(RunContext(PublisherRequest(mode=MODE_DEMO, output_dir=Path(tmp), demo_count=4), t))
            self.assertIsNone(demo.unseen_billed_warehouses)
            self.assertIsNone(demo.aws_price_registry)
            path = Path(tmp) / "evidence.json"
            save_evidence(path, demo.evidence)
            replay = analyzer.run(RunContext(PublisherRequest(mode=MODE_REPLAY, output_dir=Path(tmp), evidence_files=(str(path),)), t))
        self.assertEqual([w.evidence.warehouse_id for w in replay.warehouses], [w.evidence.warehouse_id for w in demo.warehouses])
        for a, b in zip(demo.warehouses, replay.warehouses):
            self.assertEqual({k: v.facts for k, v in a.analyses.items()}, {k: v.facts for k, v in b.analyses.items()})

    def test_demo_count_is_bounded(self):
        with self.assertRaises(SystemExit):
            analyzer.run(RunContext(PublisherRequest(mode=MODE_DEMO, output_dir=Path("."), demo_count=0), Thresholds()))


if __name__ == "__main__":
    unittest.main()
