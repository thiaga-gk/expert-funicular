"""A measured window with running time and no billed day has an unknown billed cost, never a known zero.

Billing that has not arrived (ingestion lag, a workspace or SKU mismatch) leaves a window without daily rows while the
warehouse ran. The projector then states no billed cost, no economics, no financial-quality percentages and the not-measured
reason ``BILLING_INCOMPLETE``, so the measurement can neither agree with billing nor be claimed. A window in which the
warehouse did not run (an idle weekend) legitimately bills nothing: that is a known zero.
"""
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer.analyzer.collector import demo_world
from realizer.analyzer.projector import billed, project_observation
from realizer.config.loader import validate_measurement_evidence, validate_observation
from realizer.realizer import realize

PRO = world.package("autostop-scoped-pro")
SIZE = world.package("inplace-size-canary")


def without_billing(evidence, *labels):
    """*evidence* with the daily billed rows of the named windows (``post``, ``pre``) removed."""
    out = copy.deepcopy(evidence)
    for label in labels:
        window = out["windows"][label]
        out["daily_cost"] = [r for r in out["daily_cost"] if not window["first_day"] <= r["usage_date"] <= window["last_day"]]
    return out


class BilledTests(unittest.TestCase):
    def test_no_rows_with_running_time_is_unknown_and_without_is_a_known_zero(self):
        self.assertIsNone(billed([], running=Decimal("120"))["cost"])
        self.assertEqual(billed([], running=Decimal("0"))["cost"], Decimal("0"))  # an idle window bills nothing
        self.assertEqual(billed([])["cost"], Decimal("0"))  # no running-time evidence: nothing says the warehouse ran
        row = {"dbu_quantity": 10, "unpriced_dbu_quantity": 0, "list_cost": 5, "currency_code": "USD"}
        self.assertEqual(billed([row], running=Decimal("120"))["cost"], Decimal("5"))


class AutoStopReplayTests(unittest.TestCase):
    def setUp(self):
        self.evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13)

    def test_a_post_window_without_billing_is_not_measured(self):
        obs = project_observation(PRO, without_billing(self.evidence, "post"))
        validate_observation(obs, PRO)
        meas = obs["measurement"]
        self.assertIsNone(obs["economics"])
        self.assertEqual((meas["method"], meas["not_measured_reason"], meas["cross_check"]), (None, "BILLING_INCOMPLETE", None))
        self.assertIsNone(meas["post_window"]["billed_cost"])
        self.assertIsNone(meas["post_window"]["billed_dbu"])
        quality = obs["financial_quality"]
        self.assertEqual([quality[k] for k in ("price_coverage_pct", "currency_consistency_pct", "attributable_spend_pct")], [None, None, None])
        self.assertIsNone(quality["correction_chain_complete"])

    def test_no_billing_at_all_is_not_measured(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["daily_cost"] = []
        obs = project_observation(PRO, evidence)
        self.assertIsNone(obs["economics"])
        self.assertEqual(obs["measurement"]["not_measured_reason"], "BILLING_INCOMPLETE")

    def test_a_pre_window_without_billing_keeps_the_replay_and_the_cross_check_is_incomplete(self):
        obs = project_observation(PRO, without_billing(self.evidence, "pre"))
        validate_observation(obs, PRO)
        check = obs["measurement"]["cross_check"]
        self.assertEqual((check["outcome"], check["reason"]), ("INCONCLUSIVE", "BILLING_INCOMPLETE"))
        self.assertIsNone(check["billed_dbu_reduction"])
        self.assertEqual(obs["economics"]["dbu_realized"], "609.61")  # the replay prices the post-change days alone

    def test_an_idle_window_without_billing_is_a_known_zero(self):
        evidence = copy.deepcopy(self.evidence)
        post = evidence["windows"]["post"]
        post["running"] = {**post["running"], "observed_running_cluster_minutes": 0, "observed_running_wall_minutes": 0}
        evidence["daily_cost"] = [r for r in evidence["daily_cost"] if not post["first_day"] <= r["usage_date"] <= post["last_day"]]
        obs = project_observation(PRO, evidence)
        self.assertEqual(obs["measurement"]["post_window"]["billed_cost"], "0.00")
        self.assertIsNone(obs["measurement"]["not_measured_reason"])

    def test_the_realization_is_not_claimable(self):
        evidence = without_billing(self.evidence, "post")
        validate_measurement_evidence(evidence)
        obs = project_observation(PRO, evidence)
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(PRO, obs, Path(tmp)).to_dict()
        self.assertEqual(snapshot["attribution"]["attribution_claim_status"], "NOT_ATTRIBUTABLE")
        self.assertFalse(snapshot["economics"]["available"])
        self.assertEqual(snapshot["current_state"]["realization_state"], "APPLIED")  # applied, savings not measured yet


class SizeCounterfactualTests(unittest.TestCase):
    def test_a_size_change_without_post_billing_is_not_measured(self):
        spec = demo_world.world_spec(6, [{"at": demo_world.change_at(SIZE).isoformat(), "set": {"size": "MEDIUM"}}])
        evidence = without_billing(world.measure(SIZE, spec, 6), "post")
        obs = project_observation(SIZE, evidence)
        validate_observation(obs, SIZE)
        self.assertIsNone(obs["economics"])
        self.assertEqual((obs["measurement"]["method"], obs["measurement"]["not_measured_reason"]), (None, "BILLING_INCOMPLETE"))
        self.assertIsNone(obs["measurement"]["billed_change"])


class FloorCutTests(unittest.TestCase):
    def test_a_floor_cut_without_post_billing_is_not_measured(self):
        package = world.package("min-cut-scoped")
        evidence = without_billing(world.measure(package, world.world_spec("C11", {"min": 1}), 11), "post")
        obs = project_observation(package, evidence)
        validate_observation(obs, package)
        self.assertIsNone(obs["economics"])
        self.assertEqual((obs["measurement"]["method"], obs["measurement"]["not_measured_reason"]), (None, "BILLING_INCOMPLETE"))
        self.assertIsNone(obs["measurement"]["levels"]["dbu_cost"])  # priced removed minutes need a billed rate


if __name__ == "__main__":
    unittest.main()
