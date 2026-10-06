"""The stateful bundle keeps each pricing track's own state.

A minimum-cluster cut realizes a different share of its removable floor on each track (all of it on the optimistic
track, none on the conservative one), so a later auto-stop step scales its idle saving by that track's own floor, not
by the target floor on every track.
"""
import copy
from types import SimpleNamespace
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.contracts.candidate import Range3  # noqa: E402
from publisher.contracts.scenario import PlanStep  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.packager.estimator.scenario import StatefulBundleEstimator  # noqa: E402


class BundleTrackFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        # Pinned Capacity: Pro MEDIUM pinned at 3 clusters; the floor cut 3 -> 1 is a canary, auto-stop is actionable.
        cls.e = next(e for e in synthetic_demo_evidence() if e.current_epoch.tags["golden_scenario"] == "C11")
        cls.by_knob = {c.knob: c for c in p.evaluate_warehouses([cls.e], cls.t)[0].candidates}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def step(self, n, knob, state):
        c = self.by_knob[knob]
        return PlanStep(n, c.recommendation_id, knob, c.current_config, c.recommended_config, c.status, "GATE", 0, [], [], state)

    def plan(self, *knobs):
        steps = [self.step(i + 1, k, "READY_FOR_CANARY" if k == "min_clusters" else "READY_FOR_HITL") for i, k in enumerate(knobs)]
        by_id = {c.recommendation_id: c for c in self.by_knob.values()}
        return StatefulBundleEstimator(self.t).estimate(self.e, steps, by_id), steps

    def test_preconditions(self):
        self.assertEqual((self.by_knob["min_clusters"].current_config["min_clusters"], self.by_knob["min_clusters"].recommended_config["min_clusters"]), (3, 1))
        self.assertGreater(self.by_knob["min_clusters"].total_365d_savings_range.low, 0.0)
        self.assertGreater(self.by_knob["auto_stop_minutes"].total_365d_savings_range.low, 0.0)

    def test_auto_stop_step_on_each_track_scales_with_that_tracks_floor(self):
        _, steps = self.plan("min_clusters", "auto_stop_minutes")
        own = steps[1].step_dbu_savings_range_365
        alone = self.by_knob["auto_stop_minutes"].dbu_365d_savings_range
        # The floor cut 3 -> 1 is realized at 1.0 / 0.75 / 0.5 per track, leaving floors 1 / 1.5 / 2: auto-stop then
        # removes 1/3, 1/2 and 2/3 of what it removes alone on the optimistic, base and conservative tracks, or more
        # where the floor cut removed less than that overlap (the overlap never exceeds what the cut removed).
        self.assertAlmostEqual(own.base, alone.base * 1.5 / 3, delta=1.0)
        self.assertGreaterEqual(own.low, min(alone.high / 3, alone.low * 2 / 3) - 1.0)
        self.assertLessEqual(own.high, alone.high + 1.0)

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

    def test_order_does_not_matter_when_auto_stop_removes_most_running_time(self):
        # Auto-stop removing 85% of running cluster-minutes leaves the floor cut little running time to act on: the
        # overlap is bounded by what the floor cut removes, in either order.
        a = self.by_knob["auto_stop_minutes"]
        cm = float(self.e.current_epoch.events["cluster_minutes"])
        for frac in (0.3, 0.6, 0.85):
            big = copy.copy(a)
            big.metadata = dict(a.metadata, bundle_effect=dict(a.metadata["bundle_effect"], gain_cluster_minutes=frac * cm, loss_cluster_minutes=0.0))
            self.by_knob["auto_stop_minutes"] = big
            try:
                first, _ = self.plan("min_clusters", "auto_stop_minutes")
                second, _ = self.plan("auto_stop_minutes", "min_clusters")
            finally:
                self.by_knob["auto_stop_minutes"] = a
            self.assert_same_range(first.total_365d_savings_range, second.total_365d_savings_range)


if __name__ == "__main__":
    unittest.main()


class BundleOrderTests(unittest.TestCase):
    """A size change slows or speeds busy time, and auto-stop removes idle time: in either order the plan costs the same."""

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        cls.e = next(e for e in synthetic_demo_evidence() if e.current_epoch.tags["golden_scenario"] == "C11")

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def candidates(self, busy_share):
        base, aws = self.e.dbu_365d_baseline_cost, self.e.aws_baseline_cost_365
        runtime = [busy_share * m + 1 - busy_share for m in (1.1, 1.3, 1.55)]
        size = SimpleNamespace(
            recommendation_id="size", knob="warehouse_size", status="ACTIONABLE", dbu_365d_baseline_cost=base, aws_365d_baseline_cost=aws,
            dbu_365d_estimated_cost_range=Range3(*(base * 0.6 * r for r in runtime)),
            aws_365d_estimated_cost_range=None if aws is None else Range3(*(aws * 0.6 * r for r in runtime)),
            metadata={"bundle_effect": {"type": "multiplicative_cost_factor", "busy_share": busy_share,
                                        "resource_runtime_factor_range": {"low": runtime[0], "base": runtime[1], "high": runtime[2]}}})
        auto = SimpleNamespace(recommendation_id="auto", knob="auto_stop_minutes", status="ACTIONABLE", metadata={"bundle_effect": {
            "type": "idle_cluster_minute_reduction", "realized_share": 0.5, "fixed_share": -0.02, "floor_clusters": self.e.current_epoch.config.min_clusters}})
        return {"size": size, "auto": auto}

    def total(self, order, busy_share):
        by_id = self.candidates(busy_share)
        steps = [PlanStep(i + 1, k, by_id[k].knob, {}, {}, "ACTIONABLE", "GATE", 0, [], [], "READY_FOR_HITL") for i, k in enumerate(order)]
        r = StatefulBundleEstimator(self.t).estimate(self.e, steps, by_id).dbu_365d_estimated_cost_range
        return (r.low, r.base, r.high)

    def test_size_and_auto_stop_commute_when_the_busy_share_is_known(self):
        for a, b in zip(self.total(["auto", "size"], 0.3), self.total(["size", "auto"], 0.3)):
            self.assertAlmostEqual(a, b, places=6)

    def test_unknown_busy_share_slows_only_the_running_time_left_after_idle_removal(self):
        # All running time counts as busy, but auto-stop already removed half of it as idle: the size change after it
        # slows what is left, never the removed idle (the plan then costs at most the size-first total).
        after, first = self.total(["auto", "size"], 1.0), self.total(["size", "auto"], 1.0)
        for a, b in zip(after, first):
            self.assertLessEqual(a, b + 1e-6)
