"""Observed facts that are not configuration decisions (src/realizer/analyzer/analysis.py, src/realizer/estimator/).

Photon is a billing-mix fact and the warehouse channel has no priced effect: neither enters the configuration hash, so
neither drifts an application, and a channel change recorded as an independent change does not take the claim away. The
observed configuration still records both.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.analysis import config_hash
from realizer.analyzer.projector import project_observation
from realizer.realizer import realize

PRO = world.package("autostop-scoped-pro")


def events(out):
    ledger = next((out / "realization" / "ledger").glob("*.jsonl"))
    return [json.loads(line) for line in ledger.read_text().splitlines()]


class HashTests(unittest.TestCase):
    def test_photon_and_channel_do_not_enter_the_hash(self):
        base = {"warehouse_type": "PRO", "warehouse_size": "SMALL", "min_clusters": 1, "max_clusters": 2, "auto_stop_minutes": 10,
                "warehouse_channel": "CURRENT", "enable_photon": True}
        self.assertEqual(config_hash(base), config_hash({**base, "enable_photon": None}))
        self.assertEqual(config_hash(base), config_hash({**base, "warehouse_channel": "PREVIEW"}))
        self.assertNotEqual(config_hash(base), config_hash({**base, "auto_stop_minutes": 15}))


class LaterRunTests(unittest.TestCase):
    """GE-01's world (auto-stop 30 -> 10 applied exactly), then a later run sees a Photon billing-mix flip and a channel
    move: neither is drift, the realization continues and stays claimable."""

    @classmethod
    def setUpClass(cls):
        cls.first = project_observation(PRO, world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13))
        later = copy.deepcopy(cls.first)
        later["observed_at_utc"] = later["recorded_at_utc"] = "2026-11-08T12:00:00Z"
        later["config"].update({"enable_photon": None, "warehouse_channel": "PREVIEW"})
        cls.later = later

    def run_both(self, second):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            realize(PRO, self.first, out)
            snapshot = realize(PRO, second, out).to_dict()
            return snapshot, events(out)

    def test_no_drift_and_the_claim_stays(self):
        snapshot, ledger = self.run_both(self.later)
        kinds = [e["event_type"] for e in ledger]
        self.assertNotIn("DRIFT_DETECTED", kinds)
        self.assertNotIn("REGENERATION_REQUESTED", kinds)
        self.assertEqual(snapshot["current_state"]["realization_state"], "REALIZED")
        self.assertEqual(snapshot["attribution"]["attribution_claim_status"], "CLAIMABLE")
        independent = [(e["payload"]["knob"], e["payload"]["independent_reason"]) for e in ledger if e["event_type"] == "INDEPENDENT_CHANGE_OBSERVED"]
        self.assertEqual(independent, [("warehouse_channel", "OUT_OF_RUN_SCOPE")])

    def test_a_caller_before_after_with_a_channel_move_stays_claimable(self):
        caller = copy.deepcopy(self.first)
        caller["config"]["warehouse_channel"] = "PREVIEW"
        caller["measurement"] = None
        caller["performance"] = {"normalized_p95_runtime_regression_pct": "0", "reliability_pass": True, "correctness_pass": True, "guards": None}
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(PRO, caller, Path(tmp) / "out").to_dict()
        self.assertNotIn("INDEPENDENT_CHANGE_NOT_HELD", snapshot["attribution"]["hard_disqualifiers"])


if __name__ == "__main__":
    unittest.main()
