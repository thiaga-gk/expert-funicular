"""Changes to knobs no authority step explains (src/realizer/reconciler/matching.py independent_changes,
src/realizer/estimator/attribution.py).

Every changed decision knob that no authority step of the selected plan explains is an INDEPENDENT_CHANGE_OBSERVED
with its reason: OUT_OF_RUN_SCOPE (the run scope did not cover it), NOT_RECOMMENDED (no change recommended),
PENDING_REEVALUATION or BLOCKED_STEP (its step carries no authority). The applied step's claim stays claimable only
when the counterfactual holds the other changes at their new values (the live replay does); a caller-supplied
before/after cannot, so its claim is observable only. An out-of-scope change alone requests no regeneration.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.analysis import observed_config
from realizer.analyzer.collector.collector import demo_observation
from realizer.analyzer.projector import project_observation
from realizer.reconciler.matching import match
from realizer.realizer import realize

PRO = world.package("autostop-scoped-pro")
FULL = world.package("inplace-size-canary")


def events(tmp, name="out"):
    ledger = next((Path(tmp) / name / "realization" / "ledger").glob("*.jsonl"))
    return [json.loads(line) for line in ledger.read_text().splitlines()]


class ReasonTests(unittest.TestCase):
    def independents(self, package, **changes):
        obs = demo_observation(package)
        obs["config"].update(changes)
        result = match(package, obs, observed_config(obs, package))
        return {i["knob"]: i.get("independent_reason") for i in result.step_matches if not i.get("authority_eligible")
                and i.get("implementation_conformance") not in (None, "NOT_APPLIED")}

    def test_out_of_scope_and_not_recommended(self):
        self.assertEqual(self.independents(PRO, warehouse_size="X_SMALL"), {"warehouse_size": "OUT_OF_RUN_SCOPE"})
        self.assertEqual(self.independents(PRO, warehouse_channel="PREVIEW"), {"warehouse_channel": "OUT_OF_RUN_SCOPE"})
        # Full scope: min clusters is NO_CHANGE (not in the plan), the channel is never recommended.
        self.assertEqual(self.independents(FULL, min_clusters=3, warehouse_channel="PREVIEW"),
                         {"min_clusters": "NOT_RECOMMENDED", "warehouse_channel": "NOT_RECOMMENDED"})

    def test_a_step_without_authority_names_its_state(self):
        pending = next(s for sc in FULL["scenarios"] if sc["scenario_id"] == FULL["recommended_plan"]["scenario_id"]
                       for s in sc["execution_plan"] if s.get("execution_state") == "PENDING_REEVALUATION")
        self.assertEqual(self.independents(FULL, **{pending["knob"]: pending["recommended_value"]}), {pending["knob"]: "PENDING_REEVALUATION"})


class ClaimTests(unittest.TestCase):
    """The probe: a scoped auto-stop package (30 -> 10) applied, and the owner also cuts the size SMALL -> X_SMALL."""

    @classmethod
    def setUpClass(cls):
        cls.observation = project_observation(PRO, world.measure(PRO, world.world_spec("C13", {"auto_stop": 10, "size": "X_SMALL"}), 13))

    def test_the_replay_holds_the_size_cut_and_the_auto_stop_saving_stays_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(PRO, self.observation, Path(tmp) / "out").to_dict()
            ledger = events(tmp)
        independent = [e for e in ledger if e["event_type"] == "INDEPENDENT_CHANGE_OBSERVED"]
        self.assertEqual([(e["payload"]["knob"], e["payload"]["independent_reason"]) for e in independent], [("warehouse_size", "OUT_OF_RUN_SCOPE")])
        self.assertNotIn("REGENERATION_REQUESTED", {e["event_type"] for e in ledger})
        self.assertEqual(snapshot["attribution"]["attribution_claim_status"], "CLAIMABLE")
        # The auto-stop effect alone, at the X_SMALL rate: 304.81 DBU in 28 days (the bill fell 813.08 with both changes).
        self.assertEqual(snapshot["economics"]["dbu_realized"], "304.81")
        self.assertEqual(snapshot["economics"]["measurement"]["cross_check"]["reason"], "OTHER_KNOB_CHANGED")

    def test_a_before_after_that_cannot_hold_the_size_cut_is_observable_only(self):
        caller = copy.deepcopy(self.observation)
        caller["measurement"] = None  # a caller-supplied before/after: both changes are in its difference
        caller["performance"] = {"normalized_p95_runtime_regression_pct": "0", "reliability_pass": True, "correctness_pass": True, "guards": None}
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(PRO, caller, Path(tmp) / "out").to_dict()
        self.assertIn("INDEPENDENT_CHANGE_NOT_HELD", snapshot["attribution"]["hard_disqualifiers"])
        self.assertEqual(snapshot["attribution"]["attribution_claim_status"], "OBSERVABLE_ONLY")

    def test_two_independent_knobs_are_two_events(self):
        obs = copy.deepcopy(self.observation)
        obs["config"]["warehouse_channel"] = "PREVIEW"
        with tempfile.TemporaryDirectory() as tmp:
            realize(PRO, obs, Path(tmp) / "out")
            knobs = sorted(e["payload"]["knob"] for e in events(tmp) if e["event_type"] == "INDEPENDENT_CHANGE_OBSERVED")
        self.assertEqual(knobs, ["warehouse_channel", "warehouse_size"])


if __name__ == "__main__":
    unittest.main()
