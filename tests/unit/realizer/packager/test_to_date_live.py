"""Realized to date and savings after a rollback in the snapshot, over daily runs of GE-12's world (wh-demo-005, auto-stop
45 -> 10; package range DBU 2,339.24-4,678.47 a year), measured live into one output directory.

Each daily run measures the latest whole weeks after the change, so the windows overlap: realized to date sums each
covered instant once, from the latest measurement covering it, and says which days it covers. An episode a rollback
closed keeps what it realized, still claimable for the interval it was in force, with no run-rate and no ratio.
"""
import unittest

from tests import realizer_world as world
from tests.golden.realizer import e2e
from tests.unit.realizer.reconciler.test_episodes import Runs
from realizer.analyzer.projector import project_observation


class LiveToDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = e2e.stored_package("ge12-full-scope-auto-stop")

    def daily(self, changes, days):
        spec = e2e.world_spec(5, changes)
        return Runs(self.pkg, [project_observation(self.pkg, world.measure(self.pkg, spec, 5, as_of=f"{day}T12:00:00+00:00")) for day in days])

    def test_daily_runs_sum_each_measured_day_once(self):
        runs = self.daily([{"at": "2026-10-08T14:32:00Z", "set": {"auto_stop": 10}}],
                          ["2026-10-23", "2026-10-24", "2026-10-25", "2026-10-30", "2026-11-07"])
        oct30, nov7 = runs.snapshots[3]["economics"], runs.snapshots[4]["economics"]
        # Before: "since application" was the latest window, 282.71 (Oct 15-28), on Oct 30.
        self.assertEqual(oct30["realized_savings_period"], "282.71")
        self.assertEqual(oct30["realized_savings_since_application"], "403.88")
        self.assertEqual(oct30["realized_to_date"], {"first_day": "2026-10-09", "last_day": "2026-10-28", "days_covered": "20.00", "uncovered_days": 0})
        self.assertEqual(runs.snapshots[3]["attribution"]["recommendation_attributed_realized_savings"], "403.88")
        self.assertEqual(oct30["annualized_realized_savings"], "7370.65")   # the run-rate stays the latest window's
        self.assertEqual(nov7["realized_savings_since_application"], "564.91")
        self.assertIsNone(nov7["mature_ttm_realized_savings"])
        self.assertEqual(runs.snapshots[4]["current_state"]["maturity_state"], "REPRESENTATIVE")

    def test_a_rolled_back_episode_keeps_its_savings_without_a_run_rate(self):
        runs = self.daily([{"at": "2026-10-08T00:00:00Z", "set": {"auto_stop": 10}}, {"at": "2026-11-05T09:00:00Z", "set": {"auto_stop": 45}}],
                          ["2026-10-23", "2026-11-04", "2026-11-06", "2026-11-20"])
        before, after = runs.snapshots[1], runs.snapshots[3]
        self.assertEqual(before["economics"]["annualized_realized_savings"], "7348.32")
        # Before: DRIFTED, CLAIMABLE 422.78, annualized 7,348.32 and ratio 1.34 for a change that no longer exists.
        self.assertEqual((after["current_state"]["realization_state"], after["application"]["state"]), ("ROLLED_BACK", "ROLLED_BACK"))
        self.assertEqual(after["economics"]["realized_savings_since_application"], "523.54")
        self.assertEqual(after["economics"]["realized_to_date"], {"first_day": "2026-10-08", "last_day": "2026-11-02", "days_covered": "26.00", "uncovered_days": 0})
        self.assertIsNone(after["economics"]["annualized_realized_savings"])
        self.assertIsNone(after["economics"]["realization_ratio"])
        self.assertEqual((after["attribution"]["attribution_claim_status"], after["attribution"]["recommendation_attributed_realized_savings"]), ("CLAIMABLE", "523.54"))
        self.assertEqual(after["application"]["effective_to_utc"], "2026-11-05T09:00:00Z")
        self.assertEqual(after["regeneration"]["status"], "NOT_REQUIRED")


if __name__ == "__main__":
    unittest.main()
