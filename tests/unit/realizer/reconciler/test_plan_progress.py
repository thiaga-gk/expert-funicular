"""Plan progress while application episodes are open (src/realizer/reconciler/lifecycle.py ``_plan_progress``).

A later step of the same plan applied after an earlier one is plan progress: its own application episode on the same
realization package, from the epoch that brought it, while the earlier episode stays open and goes on being measured
(the auto-stop replay holds the other knobs at their new values). A step the package holds for re-evaluation applied
early is an independent change, with no credit, and requests a fresh analysis; open episodes whose knobs are all back at
their source value are rolled back, the others go on; only a change no step explains is a drift, which closes every
open episode.
"""
import unittest

from tests import realizer_world as world
from tests.golden.realizer import e2e
from tests.unit.realizer.helpers import fixture_package
from tests.unit.realizer.reconciler.test_episodes import Runs, later
from realizer.analyzer.collector.collector import demo_observation
from realizer.analyzer.projector import project_observation

AUTO_STOP = {"at": "2026-10-08T00:00:00Z", "set": {"auto_stop": 10}}


class LaterStepTests(unittest.TestCase):
    """GE-12's world (wh-demo-005): step 1 auto-stop 45 -> 10, step 2 a canary size cut LARGE -> MEDIUM."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = e2e.stored_package("ge12-full-scope-auto-stop")

    def daily(self, changes, days):
        spec = e2e.world_spec(5, changes)
        return Runs(self.pkg, [project_observation(self.pkg, world.measure(self.pkg, spec, 5, as_of=f"{day}T12:00:00+00:00")) for day in days])

    def test_a_later_step_is_its_own_application_and_the_first_keeps_measuring(self):
        runs = self.daily([AUTO_STOP, {"at": "2026-10-22T09:00:00Z", "set": {"size": "MEDIUM"}}],
                          ["2026-10-21", "2026-10-24", "2026-10-31", "2026-11-01"])
        first = next(e for e in runs.run_events(0) if e["event_type"] == "APPLICATION_OBSERVED")
        # Before: DRIFT_DETECTED (effective Oct 8) and a CONFIGURATION_DRIFT regeneration request; the realization stopped.
        self.assertEqual(runs.kinds(1), ["APPLICATION_OBSERVED"])
        (second,) = runs.run_events(1)
        self.assertEqual((second["payload"]["step"], second["payload"]["knob"], second["payload"]["effective_from_utc"]),
                         (2, "warehouse_size", "2026-10-22T09:00:00Z"))
        self.assertNotEqual(second["application_episode_id"], first["application_episode_id"])
        # The auto-stop episode goes on: its measurement on the size epoch's whole week.
        (measurement,) = [e for e in runs.run_events(2) if e["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED"]
        self.assertEqual(measurement["application_episode_id"], first["application_episode_id"])
        self.assertEqual((measurement["payload"]["window_start_utc"], measurement["payload"]["window_end_utc"]),
                         ("2026-10-23T00:00:00Z", "2026-10-30T00:00:00Z"))
        snapshot = runs.snapshots[2]
        self.assertEqual(snapshot["application"]["state"], "APPLIED")
        self.assertEqual(snapshot["application"]["active_episode_id"], first["application_episode_id"])
        self.assertEqual((snapshot["application"]["plan_progress"]["state"], snapshot["application"]["plan_progress"]["applied_steps"]), ("COMPLETED", 2))
        self.assertEqual(snapshot["drift"], {"state": "NONE"})
        self.assertNotIn("REGENERATION_REQUESTED", [e["event_type"] for e in runs.lines])
        # A daily run of the same configuration adds no application.
        self.assertNotIn("APPLICATION_OBSERVED", runs.kinds(3))

    def test_rolling_back_the_later_step_alone_leaves_the_first_open(self):
        runs = self.daily([AUTO_STOP, {"at": "2026-10-22T00:00:00Z", "set": {"size": "MEDIUM"}}, {"at": "2026-10-29T09:00:00Z", "set": {"size": "LARGE"}}],
                          ["2026-10-21", "2026-10-24", "2026-10-30", "2026-10-31"])
        first = next(e for e in runs.run_events(0) if e["event_type"] == "APPLICATION_OBSERVED")
        second = next(e for e in runs.run_events(1) if e["event_type"] == "APPLICATION_OBSERVED")
        self.assertEqual(runs.kinds(2), ["ROLLBACK_OBSERVED"])
        (rollback,) = runs.run_events(2)
        self.assertEqual(rollback["application_episode_id"], second["application_episode_id"])
        self.assertEqual(rollback["payload"]["rolled_back_episode_ids"], [second["application_episode_id"]])
        self.assertEqual(rollback["payload"]["effective_from_utc"], "2026-10-29T09:00:00Z")
        snapshot = runs.snapshots[2]
        self.assertEqual((snapshot["application"]["state"], snapshot["application"]["active_episode_id"]), ("APPLIED", first["application_episode_id"]))
        self.assertEqual(runs.kinds(3), [])

    def test_a_change_no_step_explains_is_a_drift_that_closes_every_open_episode(self):
        runs = self.daily([AUTO_STOP, {"at": "2026-10-22T00:00:00Z", "set": {"size": "MEDIUM"}}, {"at": "2026-10-29T09:00:00Z", "set": {"min": 2}}],
                          ["2026-10-21", "2026-10-24", "2026-10-30"])
        self.assertEqual(runs.kinds(2)[0], "DRIFT_DETECTED")
        self.assertIn("REGENERATION_REQUESTED", runs.kinds(2))
        snapshot = runs.snapshots[2]
        self.assertEqual((snapshot["application"]["state"], snapshot["drift"]["state"]), ("DRIFTED", "DRIFTED"))


class HeldStepTests(unittest.TestCase):
    """``inplace-size-canary`` (wh-demo-006): step 1 the size canary, step 3 max clusters 4 -> 2 held for re-evaluation."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)   # applies step 1 exactly

    def test_a_held_step_applied_early_is_independent_and_requests_regeneration(self):
        early = later(self.base, 10, config={**self.base["config"], "max_clusters": 2}, economics=None)
        runs = Runs(self.pkg, [self.base, early, later(early, 11)])
        # Before: DRIFT_DETECTED and a CONFIGURATION_DRIFT regeneration request; the size episode closed.
        self.assertEqual(runs.kinds(1)[:2], ["INDEPENDENT_CHANGE_OBSERVED", "REGENERATION_REQUESTED"])
        independent, request = runs.run_events(1)[:2]
        self.assertEqual((independent["payload"]["knob"], independent["payload"]["independent_reason"]), ("max_clusters", "PENDING_REEVALUATION"))
        self.assertEqual(request["payload"]["reasons"], ["PENDING_STEP_APPLIED"])
        self.assertNotIn("DRIFT_DETECTED", [e["event_type"] for e in runs.lines])
        self.assertEqual(runs.snapshots[1]["application"]["state"], "APPLIED")
        # Seen again the next day: the change and its request are not recorded again.
        self.assertNotIn("INDEPENDENT_CHANGE_OBSERVED", runs.kinds(2))
        self.assertNotIn("REGENERATION_REQUESTED", runs.kinds(2))


if __name__ == "__main__":
    unittest.main()
