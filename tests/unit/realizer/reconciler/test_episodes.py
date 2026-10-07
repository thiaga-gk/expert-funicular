"""Application episodes over daily runs (src/realizer/reconciler/lifecycle.py, the Analyzer's history and flag check, the
snapshot's application state).

A rollback is read from the configuration: every knob the application changed back at its source value, after the
application. It is recorded on the episode it rolls back, with no drift and no regeneration request (the warehouse is at
the package's own configuration). The caller's ``rollback_observed`` flag is only checked against the configuration: a
flag the configuration contradicts is refused before anything for the observation is written. A package's own
safety-rollback step is an application of that package; rolling it back means returning to the value it replaced.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import fixture_package, outcome
from realizer.analyzer.analysis import config_hash, package_config
from realizer.analyzer.collector.collector import demo_observation
from realizer.realizer import realize


def stamp(day, hour=12):
    return f"2026-10-{day:02d}T{hour:02d}:00:00Z"


def later(observation, day, **patch):
    """*observation* seen again on October *day* (its window the week before), with *patch* applied."""
    return {**copy.deepcopy(observation), "observed_at_utc": stamp(day), "recorded_at_utc": stamp(day),
            "window_start_utc": stamp(day - 7), "window_end_utc": stamp(day), **patch}


class Runs:
    """Every observation realized into one output directory, the way daily runs share one."""

    def __init__(self, package, observations):
        self.outcomes, self.snapshots, self.lines_after = [], [], []
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for observation in observations:
                result = outcome(realize, copy.deepcopy(package), copy.deepcopy(observation), out)
                self.outcomes.append(result[:2] if result[0] != "ok" else ("ok", None))
                self.snapshots.append(result[1].to_dict() if result[0] == "ok" else None)
                self.lines_after.append([json.loads(line) for p in sorted((out / "realization" / "ledger").glob("*.jsonl"))
                                         for line in p.read_text(encoding="utf-8").splitlines()])
            self.evidence = sorted(p.name for p in (out / "realization" / "evidence").glob("*.json")) if (out / "realization" / "evidence").exists() else []

    @property
    def lines(self):
        return self.lines_after[-1]

    def run_events(self, index):
        """The events run *index* appended."""
        before = len(self.lines_after[index - 1]) if index else 0
        return self.lines_after[index][before:]

    def kinds(self, index):
        return [e["event_type"] for e in self.run_events(index)]


class RollbackFromConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()            # step 1: size LARGE -> MEDIUM
        cls.base = demo_observation(cls.pkg)   # applies it exactly
        cls.source = package_config(cls.pkg)

    def test_the_configuration_back_at_its_source_value_is_a_rollback_without_the_flag(self):
        runs = Runs(self.pkg, [self.base, later(self.base, 11, config=dict(self.source), economics=None)])
        (app,) = [e for e in runs.lines if e["event_type"] == "APPLICATION_OBSERVED"]
        self.assertEqual(runs.kinds(1), ["ROLLBACK_OBSERVED"])
        (rollback,) = runs.run_events(1)
        self.assertEqual(rollback["application_episode_id"], app["application_episode_id"])
        self.assertEqual(rollback["payload"]["observed_config_hash"], config_hash(self.source))
        snapshot = runs.snapshots[1]
        self.assertEqual((snapshot["application"]["state"], snapshot["current_state"]["realization_state"]), ("ROLLED_BACK", "ROLLED_BACK"))
        self.assertEqual(snapshot["drift"], {"state": "NONE"})
        # the only regeneration request is the first run's (the size step invalidates the downstream steps)
        self.assertEqual([e["payload"]["reasons"] for e in runs.lines if e["event_type"] == "REGENERATION_REQUESTED"],
                         [["UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM"]])

    def test_the_flag_with_the_configuration_back_is_the_same_rollback(self):
        flagged = Runs(self.pkg, [self.base, later(self.base, 11, config=dict(self.source), economics=None, rollback_observed=True)])
        unflagged = Runs(self.pkg, [self.base, later(self.base, 11, config=dict(self.source), economics=None)])
        self.assertEqual(flagged.kinds(1), ["ROLLBACK_OBSERVED"])
        self.assertEqual(flagged.snapshots[1]["application"]["state"], "ROLLED_BACK")
        self.assertEqual([e["ledger_entry_id"] for e in flagged.lines], [e["ledger_entry_id"] for e in unflagged.lines])

    def test_a_flag_the_configuration_contradicts_is_refused_before_anything_is_written(self):
        # the applied configuration observed again, said to be rolled back
        runs = Runs(self.pkg, [self.base, later(self.base, 11, rollback_observed=True)])
        self.assertEqual(runs.outcomes[1][0], "InputError")
        self.assertIn("rollback_observed contradicts its configuration", runs.outcomes[1][1])
        self.assertEqual(runs.lines_after[1], runs.lines_after[0])
        self.assertEqual(len(runs.evidence), 1)   # the first run's only
        # with no application yet: only the lineage entry, no evidence file
        first = Runs(self.pkg, [{**self.base, "rollback_observed": True}])
        self.assertEqual(first.outcomes[0][0], "InputError")
        self.assertEqual([e["event_type"] for e in first.lines], ["REALIZATION_CREATED"])
        self.assertEqual(first.evidence, [])
        # nothing applied and nothing to roll back: accepted, nothing recorded for it
        nothing = Runs(self.pkg, [{**self.base, "config": dict(self.source), "rollback_observed": True, "economics": None}])
        self.assertEqual(nothing.outcomes[0][0], "ok")
        self.assertNotIn("ROLLBACK_OBSERVED", nothing.kinds(0))
        self.assertEqual(nothing.snapshots[0]["application"]["state"], "NOT_APPLIED")

    def test_a_change_that_is_not_back_at_the_source_value_is_still_a_drift(self):
        runs = Runs(self.pkg, [self.base, later(self.base, 11, config={**self.base["config"], "min_clusters": 2}, economics=None)])
        self.assertIn("DRIFT_DETECTED", runs.kinds(1))
        self.assertIn("REGENERATION_REQUESTED", runs.kinds(1))
        self.assertNotIn("ROLLBACK_OBSERVED", runs.kinds(1))
        self.assertEqual(runs.snapshots[1]["application"]["state"], "DRIFTED")

    def test_a_scenario_application_is_rolled_back_only_when_every_applied_step_is(self):
        package = fixture_package("two-ready-steps")
        base = demo_observation(package)
        source = package_config(package)
        steps = [s for s in package["scenarios"][0]["execution_plan"] if s.get("step_type") == "CANDIDATE"]
        both = {**base, "config": {**base["config"], **{s["knob"]: s["recommended_value"] for s in steps if s["execution_state"] in ("READY", "READY_FOR_CANARY")}}}
        runs = Runs(package, [both])
        self.assertEqual(runs.snapshots[0]["application"]["application_scope"], "SCENARIO")
        knobs = [ref["step"] for ref in runs.snapshots[0]["application"]["applied_step_refs"]]
        self.assertEqual(len(knobs), 2)
        first_knob = next(s["knob"] for s in steps if s["step"] == knobs[0])
        one_back = later(both, 11, config={**both["config"], first_knob: source[first_knob]}, economics=None)
        all_back = later(both, 11, config=dict(source), economics=None)
        self.assertIn("DRIFT_DETECTED", Runs(package, [both, one_back]).kinds(1))
        self.assertEqual(Runs(package, [both, all_back]).kinds(1), ["ROLLBACK_OBSERVED"])


class ClosedEpisodeTests(unittest.TestCase):
    """A drift or an observed rollback closes the episode: the applied configuration observed again after it is a new
    episode, measured from then on, and a later drift or rollback of that episode is recorded on it (each carries the time
    it took effect). The same observation of an open episode on later days stays in it."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)    # size MEDIUM applied 2026-10-01T12:00Z, observed 2026-10-09
        cls.source = package_config(cls.pkg)
        # seen at the observed time (no applied time of their own)
        cls.rollback = later(cls.base, 11, config=dict(cls.source), economics=None, applied_at_utc=None)
        cls.drift = later(cls.base, 11, config={**cls.base["config"], "min_clusters": 2}, economics=None, applied_at_utc=None)
        cls.reapplied = later(cls.base, 14, applied_at_utc=stamp(13))

    @staticmethod
    def apps(runs, index=None):
        lines = runs.lines if index is None else runs.run_events(index)
        return [e for e in lines if e["event_type"] in ("APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED")]

    def test_the_configuration_applied_again_after_a_rollback_is_a_new_episode(self):
        runs = Runs(self.pkg, [self.base, self.rollback, self.reapplied])
        (first,), (second,) = self.apps(runs, 0), self.apps(runs, 2)
        self.assertNotEqual(second["application_episode_id"], first["application_episode_id"])
        self.assertEqual(second["payload"]["effective_from_utc"], stamp(13))
        # measured from then on: the new run's measurement is the new episode's
        (measurement,) = [e for e in runs.run_events(2) if e["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED"]
        self.assertEqual(measurement["application_episode_id"], second["application_episode_id"])
        snapshot = runs.snapshots[2]
        self.assertEqual((snapshot["application"]["state"], snapshot["application"]["active_episode_id"]), ("APPLIED", second["application_episode_id"]))
        self.assertIsNone(snapshot["application"]["effective_to_utc"])
        self.assertEqual(snapshot["drift"], {"state": "NONE"})
        self.assertEqual(snapshot["history"]["application_episode_count"], 2)

    def test_the_configuration_applied_again_after_a_drift_is_a_new_episode(self):
        runs = Runs(self.pkg, [self.base, self.drift, self.reapplied])
        (first,), (second,) = self.apps(runs, 0), self.apps(runs, 2)
        self.assertNotEqual(second["application_episode_id"], first["application_episode_id"])
        self.assertEqual(runs.snapshots[2]["application"]["state"], "APPLIED")

    def test_daily_observations_of_the_new_episode_stay_in_it(self):
        runs = Runs(self.pkg, [self.base, self.rollback, self.reapplied, later(self.reapplied, 15), later(self.reapplied, 16)])
        self.assertEqual(len(self.apps(runs)), 2)
        self.assertEqual(self.apps(runs, 3) + self.apps(runs, 4), [])
        episode = self.apps(runs, 2)[0]["application_episode_id"]
        self.assertEqual({e["application_episode_id"] for e in runs.run_events(4)}, {episode})
        self.assertEqual(runs.snapshots[4]["application"]["state"], "APPLIED")

    def test_a_second_rollback_and_a_second_drift_are_recorded(self):
        again = later(self.rollback, 16)
        runs = Runs(self.pkg, [self.base, self.rollback, self.reapplied, again])
        rollbacks = [e for e in runs.lines if e["event_type"] == "ROLLBACK_OBSERVED"]
        self.assertEqual([e["application_episode_id"] for e in rollbacks],
                         [self.apps(runs, 0)[0]["application_episode_id"], self.apps(runs, 2)[0]["application_episode_id"]])
        self.assertEqual([e["payload"]["effective_from_utc"] for e in rollbacks], [stamp(11), stamp(16)])
        self.assertEqual(runs.snapshots[3]["application"]["state"], "ROLLED_BACK")
        drifted = Runs(self.pkg, [self.base, self.drift, self.reapplied, later(self.drift, 16)])
        drifts = [e for e in drifted.lines if e["event_type"] == "DRIFT_DETECTED"]
        self.assertEqual(len(drifts), 2)
        self.assertEqual([e["payload"]["effective_from_utc"] for e in drifts], [stamp(11), stamp(16)])
        self.assertEqual(drifted.snapshots[3]["application"]["state"], "DRIFTED")

    def test_the_closing_configuration_observed_again_records_nothing_new(self):
        for closing in (self.rollback, self.drift):
            with self.subTest(closing=closing["config"]):
                runs = Runs(self.pkg, [self.base, closing, later(closing, 12), later(closing, 13)])
                # (an independent change, here the drift's min_clusters, is recorded on each observation, as before)
                self.assertEqual([k for k in runs.kinds(2) + runs.kinds(3) if k != "INDEPENDENT_CHANGE_OBSERVED"], [])
                self.assertEqual(runs.snapshots[3]["application"], runs.snapshots[1]["application"])
                self.assertEqual(runs.snapshots[3]["drift"], runs.snapshots[1]["drift"])

    def test_a_rollback_request_is_recorded_once_while_it_is_pending(self):
        requested = {**self.base, "rollback_requested": True, "rollback_reason": "slow"}
        runs = Runs(self.pkg, [requested, later(requested, 10), later(requested, 11)])
        (request,) = [e for e in runs.lines if e["event_type"] == "ROLLBACK_REQUESTED"]
        self.assertEqual(request["payload"], {"reason": "slow", "effective_from_utc": self.base["observed_at_utc"]})
        # once the rollback was observed, a request on the application seen again is a new one
        again = Runs(self.pkg, [requested, self.rollback, {**self.reapplied, "rollback_requested": True, "rollback_reason": "slow"}])
        self.assertEqual(len([e for e in again.lines if e["event_type"] == "ROLLBACK_REQUESTED"]), 2)


class SafetyRollbackStepTests(unittest.TestCase):
    """wh-demo-040: the package's own canary rollback (size MEDIUM -> LARGE) is an application of the package."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = world.package("canary-rollback")
        cls.base = demo_observation(cls.pkg)   # applies LARGE

    def test_applying_it_is_an_application_and_its_flag_is_refused(self):
        refused = Runs(self.pkg, [{**self.base, "rollback_observed": True}])
        self.assertEqual(refused.outcomes[0][0], "InputError")
        runs = Runs(self.pkg, [self.base])
        self.assertIn("APPLICATION_OBSERVED", runs.kinds(0))
        self.assertNotIn("ROLLBACK_OBSERVED", runs.kinds(0))
        self.assertEqual(runs.snapshots[0]["current_state"]["realization_state"], "SAFETY_ACTION_APPLIED")

    def test_returning_to_the_value_it_replaced_rolls_it_back(self):
        back = later(self.base, 12, config={**self.base["config"], "warehouse_size": package_config(self.pkg)["warehouse_size"]}, economics=None)
        runs = Runs(self.pkg, [self.base, back])
        self.assertEqual(runs.kinds(1), ["ROLLBACK_OBSERVED"])
        self.assertEqual(runs.snapshots[1]["application"]["state"], "ROLLED_BACK")


if __name__ == "__main__":
    unittest.main()
