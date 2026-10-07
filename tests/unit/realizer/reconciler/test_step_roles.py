"""Step roles (src/realizer/analyzer/analysis.py ``step_role``, the estimator and the snapshot).

A plan step is a saving, a performance fix, a capacity guardrail or a safety rollback. A canary rollback (the candidate
carries the Publisher's ``CANARY_GUARDRAIL_BREACHED`` blocker and restores the value its ``observed_change`` replaced) is a
safety action: its application is recorded with its conformance, its validation runs, but nothing is measured and
nothing is claimed, whatever costs the observation carries.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.analysis import candidate_steps, step_role
from realizer.analyzer.collector.collector import demo_observation
from realizer.realizer import realize

ROLLBACK = world.package("canary-rollback")
ADD = world.package("capacity-add-scoped")
CUT = world.package("capacity-cut-scoped")
PRO = world.package("autostop-scoped-pro")


def first_step(package):
    return candidate_steps(package)[0]


def run(package, observation):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        snapshot = realize(package, observation, out).to_dict()
        ledger = next((out / "realization" / "ledger").glob("*.jsonl"))
        return snapshot, [json.loads(line) for line in ledger.read_text().splitlines()]


class RoleTests(unittest.TestCase):
    def test_roles_of_the_fixture_steps(self):
        self.assertEqual(step_role(ROLLBACK, first_step(ROLLBACK)), "SAFETY_ROLLBACK")
        self.assertEqual(step_role(ADD, first_step(ADD)), "PERFORMANCE")
        self.assertEqual(step_role(CUT, first_step(CUT)), "CAPACITY")
        self.assertEqual(step_role(PRO, first_step(PRO)), "SAVINGS")


class SafetyRollbackTests(unittest.TestCase):
    """wh-demo-040: the size cut LARGE -> MEDIUM breached its p95 guard; the package restores LARGE. The demo observation
    applies it with a synthetic 1,000 'saving' (old 10,000, new 9,000): before, that was booked and claimable."""

    @classmethod
    def setUpClass(cls):
        cls.snapshot, cls.ledger = run(ROLLBACK, demo_observation(ROLLBACK))

    def test_the_application_is_recorded_with_its_role(self):
        app = self.snapshot["application"]
        self.assertEqual((app["implementation_conformance"], app["knob"], app["observed_applied_value"], app["step_role"]),
                         ("EXACT", "warehouse_size", "LARGE", "SAFETY_ROLLBACK"))
        self.assertEqual(self.snapshot["current_state"]["realization_state"], "SAFETY_ACTION_APPLIED")

    def test_validation_runs_but_nothing_is_measured_or_claimed(self):
        kinds = [e["event_type"] for e in self.ledger]
        self.assertIn("VALIDATION_COMPLETED", kinds)
        self.assertNotIn("COUNTERFACTUAL_RECORDED", kinds)
        self.assertNotIn("ECONOMIC_MEASUREMENT_RECORDED", kinds)
        self.assertEqual(self.snapshot["attribution"]["attribution_claim_status"], "NOT_ATTRIBUTABLE")
        self.assertEqual(self.snapshot["attribution"]["recommendation_attributed_realized_savings"], "0.00")
        self.assertIsNone(self.snapshot["economics"]["successful_realized_savings"])


if __name__ == "__main__":
    unittest.main()
