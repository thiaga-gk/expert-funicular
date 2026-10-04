"""Auto-stop evidence that contradicts itself is rejected, not clamped and priced.

Idle time removable by a lower auto-stop (inter-query gaps x clusters kept at the minimum) cannot exceed the running
cluster-minutes the warehouse events recorded. When it does, one of the two sources is wrong (missing events, a
gap sample from another period), so the candidate is INSUFFICIENT_EVIDENCE instead of a capped, priced saving.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


class AutoStopEvidenceTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = p.Thresholds()
        self.e = copy.deepcopy(p.demo_evidence()[0])  # auto-stop 120, min 2

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def auto_stop(self, e):
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["auto_stop_minutes"]

    def with_gaps(self, saved_idle, cluster_minutes):
        e = copy.deepcopy(self.e)
        e.current_epoch.events["cluster_minutes"] = cluster_minutes
        for k in list(e.current_epoch.gaps):
            if k.startswith("saved_idle_minutes_if_"):
                e.current_epoch.gaps[k] = saved_idle
        return e

    def test_idle_larger_than_running_time_is_insufficient_evidence(self):
        # Events record 500 cluster-minutes; gaps claim 15,840 idle minutes at min 2 (31,680 cluster-minutes).
        c = self.auto_stop(self.with_gaps(15840.0, 500.0))
        self.assertEqual(c.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("IDLE_EVIDENCE_EXCEEDS_RUNTIME", c.blockers)
        self.assertEqual(c.recommended_config, c.current_config)
        self.assertIsNone(c.estimated_savings_365)

    def test_consistent_evidence_is_still_priced(self):
        c = self.auto_stop(self.with_gaps(5000.0, 30000.0))  # 10,000 removable of 30,000 running
        self.assertEqual(c.status, "ACTIONABLE")
        self.assertGreater(c.estimated_savings_365, 0)


if __name__ == "__main__":
    unittest.main()
