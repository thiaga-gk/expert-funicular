"""The billed cross-check of a replay (src/realizer/analyzer/crosscheck.py) and its consequence for the claim.

The replay saving is checked against the billed cost before and after the change, scaled to the post-change volume
and length: AGREES within 25 percent and with the same sign, INCONCLUSIVE when the comparison cannot resolve it (no
blocking), DISAGREES otherwise, which keeps the measurement out of CLAIMABLE until reviewed.
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.crosscheck import billed_cross_check, weekly_costs
from realizer.analyzer.projector import project_observation
from realizer.realizer import realize

D = Decimal
WEEKS = [D("254"), D("249"), D("254"), D("254")]


def check(replay="609.61", pre_cost="1011.20", post_cost="396.25", weekly=WEEKS, volume="1", others=(), pre_days=28, post_days=28):
    return billed_cross_check(replay_dbu_saving=None if replay is None else D(replay),
                              pre=None if pre_cost is False else {"days": pre_days, "billed_cost": None if pre_cost is None else D(pre_cost)},
                              post={"days": post_days, "billed_cost": D(post_cost)}, pre_weekly=weekly,
                              volume_ratio=None if volume is None else D(volume), other_knob_changes=others)


class CrossCheckRuleTests(unittest.TestCase):
    def test_agrees_within_a_quarter_of_the_replay(self):
        out = check()
        self.assertEqual((out["outcome"], out["reason"]), ("AGREES", None))
        self.assertEqual(out["billed_dbu_reduction"], D("614.95"))  # 1,011.20 x 1 - 396.25
        self.assertLess(out["difference_pct"], 1)

    def test_a_restart_assumption_the_bill_does_not_show_disagrees(self):
        # Illustration: billed reduction 46 percent above the replay.
        out = check(replay="609.61", post_cost="121.00")
        self.assertEqual(out["outcome"], "DISAGREES")
        self.assertEqual(check(replay="-50", post_cost="396.25")["outcome"], "DISAGREES")  # opposite sign

    def test_the_bill_is_scaled_to_the_post_change_volume_and_length(self):
        out = check(volume="1.2", pre_days=14, weekly=WEEKS[:2], post_cost="396.25")
        self.assertEqual(out["billed_dbu_reduction"], D("1011.20") / 14 * D("1.2") * 28 - D("396.25"))

    def test_inconclusive_reasons_never_block(self):
        self.assertEqual(check(pre_cost=False)["reason"], "NO_PRE_WINDOW")
        self.assertEqual(check(pre_cost=None)["reason"], "BILLING_INCOMPLETE")
        self.assertEqual(check(weekly=None)["reason"], "BILLING_INCOMPLETE")
        self.assertEqual(check(others=("warehouse_size",))["reason"], "OTHER_KNOB_CHANGED")
        self.assertEqual(check(volume="1.30")["reason"], "VOLUME_SHIFT")
        self.assertEqual(check(volume=None)["reason"], "VOLUME_SHIFT")
        self.assertEqual(check(weekly=WEEKS[:1], pre_days=7)["reason"], "NOISE_UNKNOWN")
        # A 44 saving against weekly bills that swing by about 45: below twice the window's noise.
        swing = check(replay="44", pre_cost="2207", post_cost="2175", weekly=[D("772"), D("767"), D("668"), D("700")])
        self.assertEqual((swing["outcome"], swing["reason"]), ("INCONCLUSIVE", "BELOW_NOISE"))
        self.assertIsNone(check(replay=None))

    def test_weekly_costs_count_missing_days_as_zero_and_refuse_unpriced(self):
        rows = [{"usage_date": "2026-09-10", "list_cost": 10.0, "unpriced_dbu_quantity": 0.0},
                {"usage_date": "2026-09-18", "list_cost": 5.0, "unpriced_dbu_quantity": 0.0}]
        self.assertEqual(weekly_costs(rows, "2026-09-10", 14), [D("10.0"), D("5.0")])
        self.assertIsNone(weekly_costs(rows + [{"usage_date": "2026-09-11", "list_cost": None, "unpriced_dbu_quantity": 3.0}], "2026-09-10", 14))


class CrossCheckOnTheLivePathTests(unittest.TestCase):
    def test_a_simulated_change_agrees_and_an_extra_size_cut_is_inconclusive(self):
        pkg = world.package("autostop-scoped-pro")
        agrees = project_observation(pkg, world.measure(pkg, world.world_spec("C13", {"auto_stop": 10}), 13))["measurement"]["cross_check"]
        self.assertEqual((agrees["outcome"], agrees["billed_dbu_reduction"], agrees["replay_dbu_saving"]), ("AGREES", "614.95", "609.61"))
        both = project_observation(pkg, world.measure(pkg, world.world_spec("C13", {"auto_stop": 10, "size": "X_SMALL"}), 13))["measurement"]
        self.assertEqual((both["cross_check"]["outcome"], both["cross_check"]["reason"]), ("INCONCLUSIVE", "OTHER_KNOB_CHANGED"))

    def test_disagreement_keeps_the_measurement_out_of_claimable_and_both_numbers_reach_the_rpkg(self):
        pkg = world.package("autostop-scoped-pro")
        obs = project_observation(pkg, world.measure(pkg, world.world_spec("C13", {"auto_stop": 10}), 13))
        obs["origin_attribution"] = "CONFIRMED"  # isolate the cross-check from the origin rule
        obs["performance"] = {"normalized_p95_runtime_regression_pct": "0", "reliability_pass": True, "correctness_pass": True, "guards": None}
        disagree = {**obs, "measurement": {**obs["measurement"], "cross_check": {**obs["measurement"]["cross_check"], "outcome": "DISAGREES"}}}
        with tempfile.TemporaryDirectory() as tmp:
            agreed = realize(pkg, obs, Path(tmp) / "a").to_dict()
            disputed = realize(pkg, disagree, Path(tmp) / "b").to_dict()
        self.assertNotIn("BILLED_CROSS_CHECK_DISAGREES", agreed["attribution"]["hard_disqualifiers"])
        self.assertIn("BILLED_CROSS_CHECK_DISAGREES", disputed["attribution"]["hard_disqualifiers"])
        self.assertNotEqual(disputed["attribution"]["attribution_claim_status"], "CLAIMABLE")
        self.assertEqual(agreed["economics"]["measurement"]["cross_check"]["billed_dbu_reduction"], "614.95")
        self.assertEqual(agreed["economics"]["measurement"]["cross_check"]["replay_dbu_saving"], "609.61")


if __name__ == "__main__":
    unittest.main()
