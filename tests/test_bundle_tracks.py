"""The stateful bundle keeps each pricing track's own state.

A minimum-cluster cut realizes a different share of its removable floor on each track (all of it on the optimistic
track, none on the conservative one), so a later auto-stop step scales its idle saving by that track's own floor, not
by the target floor on every track.
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


class BundleTrackFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        # Pinned Capacity: Pro MEDIUM pinned at 3 clusters; the floor cut 3 -> 1 is a canary, auto-stop is actionable.
        cls.e = next(e for e in p.synthetic_demo_evidence() if e.current_epoch.tags["golden_scenario"] == "C11")
        cls.by_knob = {c.knob: c for c in p.evaluate_warehouses([cls.e], cls.t)[0].candidates}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def step(self, n, knob, state):
        c = self.by_knob[knob]
        return p.PlanStep(n, c.recommendation_id, knob, c.current_config, c.recommended_config, c.status, "GATE", 0, [], [], state)

    def plan(self, *knobs):
        steps = [self.step(i + 1, k, "READY_FOR_CANARY" if k == "min_clusters" else "READY_FOR_HITL") for i, k in enumerate(knobs)]
        by_id = {c.recommendation_id: c for c in self.by_knob.values()}
        return p.StatefulBundleEstimator(self.t).estimate(self.e, steps, by_id), steps

    def test_preconditions(self):
        self.assertEqual((self.by_knob["min_clusters"].current_config["min_clusters"], self.by_knob["min_clusters"].recommended_config["min_clusters"]), (3, 1))
        self.assertEqual(self.by_knob["min_clusters"].total_365d_savings_range.low, 0.0)
        self.assertGreater(self.by_knob["auto_stop_minutes"].total_365d_savings_range.low, 0.0)

    def test_conservative_track_keeps_the_floor_the_min_cut_did_not_realize(self):
        # Conservative track: the floor cut realizes nothing, so the floor stays at 3 and auto-stop removes exactly what
        # it removes on its own. Before the fix every track used floor 1 and the low was a third of that.
        port, _ = self.plan("min_clusters", "auto_stop_minutes")
        alone = self.by_knob["auto_stop_minutes"].total_365d_savings_range
        self.assertAlmostEqual(port.total_365d_savings_range.low, alone.low, delta=1.0)

    def test_auto_stop_step_on_each_track_scales_with_that_tracks_floor(self):
        _, steps = self.plan("min_clusters", "auto_stop_minutes")
        own = steps[1].step_dbu_savings_range_365
        alone = self.by_knob["auto_stop_minutes"].dbu_365d_savings_range
        # Optimistic track: the floor is 1 of 3 after the cut, so auto-stop removes a third of what it removes alone; the
        # conservative track keeps floor 3 (same as alone), and the base track the midpoint floor 2.
        self.assertAlmostEqual(own.low, alone.high / 3, delta=1.0)
        self.assertAlmostEqual(own.base, alone.base * 2 / 3, delta=1.0)
        self.assertAlmostEqual(own.high, alone.low, delta=1.0)

    def test_order_does_not_change_the_conservative_low(self):
        first, _ = self.plan("min_clusters", "auto_stop_minutes")
        second, _ = self.plan("auto_stop_minutes", "min_clusters")
        self.assertAlmostEqual(first.total_365d_savings_range.low, second.total_365d_savings_range.low, delta=1.0)

    def assert_same_range(self, a, b):
        for track in ("low", "base", "high"):
            self.assertAlmostEqual(getattr(a, track), getattr(b, track), delta=1.0, msg=track)

    def test_plan_total_does_not_depend_on_order_on_any_track(self):
        first, _ = self.plan("min_clusters", "auto_stop_minutes")
        second, _ = self.plan("auto_stop_minutes", "min_clusters")
        self.assert_same_range(first.total_365d_savings_range, second.total_365d_savings_range)

    def test_raised_auto_stop_rescales_when_a_later_floor_cut_lowers_the_floor(self):
        # A raise saves restarts minus the idle it adds, both per floor cluster: priced first at floor 3, its saving must
        # shrink when the floor cut that follows lowers the track's floor, exactly as when it is priced after the cut.
        a = self.by_knob["auto_stop_minutes"]
        raised = copy.copy(a)
        raised.metadata = dict(a.metadata, bundle_effect=dict(a.metadata["bundle_effect"], gain_cluster_minutes=21600.0,
                                                               loss_cluster_minutes=6000.0))
        raised.recommended_config = dict(a.recommended_config, auto_stop_minutes=45)
        saved = self.by_knob["auto_stop_minutes"]
        self.by_knob["auto_stop_minutes"] = raised
        try:
            first, _ = self.plan("min_clusters", "auto_stop_minutes")
            second, _ = self.plan("auto_stop_minutes", "min_clusters")
        finally:
            self.by_knob["auto_stop_minutes"] = saved
        self.assert_same_range(first.total_365d_savings_range, second.total_365d_savings_range)


if __name__ == "__main__":
    unittest.main()
