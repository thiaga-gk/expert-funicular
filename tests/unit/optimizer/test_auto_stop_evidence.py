"""Auto-stop evidence that contradicts itself is rejected, not clamped and priced.

Running idle time removable by a lower auto-stop (idle gaps intersected with running time) cannot exceed the running
cluster-minutes the warehouse events recorded. When it does, one of the two sources is wrong (missing events, a
gap sample from another period), so the candidate is INSUFFICIENT_EVIDENCE instead of a capped, priced saving.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402


class AutoStopEvidenceTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Thresholds()
        self.e = copy.deepcopy(demo_evidence()[0])  # auto-stop 120, min 2

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def auto_stop(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["auto_stop_minutes"]

    def with_gaps(self, removable_cluster_minutes, cluster_minutes):
        """Every forecast window records ``cluster_minutes`` running and ``removable_cluster_minutes`` removable per value."""
        e = copy.deepcopy(self.e)
        for window in e.current_epoch.gaps["running_idle_windows"].values():
            window["observed_running_cluster_minutes"] = cluster_minutes
            for row in window["running_idle_by_target"].values():
                row["removable_running_cluster_minutes"] = removable_cluster_minutes
        return e

    def test_idle_larger_than_running_time_is_insufficient_evidence(self):
        # Each window records 500 running cluster-minutes; the running-idle rows claim 31,680 removable cluster-minutes.
        c = self.auto_stop(self.with_gaps(31680.0, 500.0))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("IDLE_EVIDENCE_EXCEEDS_RUNTIME", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)
        self.assertIsNone(c.estimated_savings_365)

    def test_consistent_evidence_is_still_priced(self):
        c = self.auto_stop(self.with_gaps(10000.0, 30000.0))  # 10,000 removable of 30,000 running
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertGreater(c.estimated_savings_365, 0)


if __name__ == "__main__":
    unittest.main()
