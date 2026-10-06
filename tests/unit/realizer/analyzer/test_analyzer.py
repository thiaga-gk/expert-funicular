"""Realizer analyzer capability (src/realizer/analyzer/analyzer.py): ``run`` and its ``RealizationAnalysis``.

``run`` reads the ledger a run starts from and analyzes the observation. Its history and drift flag equal what the
oracle computes inline at the same moment for every run of the 41 scenarios, a lineage-only run has no observation, and
it writes nothing.
"""
import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome, tree  # noqa: E402
from realizer.analyzer import analyzer  # noqa: E402
from realizer.contracts.analysis import ObservationAnalysis, RealizationAnalysis, RealizationHistory  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402


def oracle_reference(package, observation, out, rpkg):
    """The oracle's inline history and drift computation (``process_observation``) at the start of a run."""
    entries = oengine.load_ledger(oengine.ledger_path(out, rpkg))
    previous = oengine.latest_event(entries, oengine.rc.EventType.APPLICATION_OBSERVED, oengine.rc.EventType.MODIFIED_APPLICATION_OBSERVED)
    prior = oengine.latest_event(entries, oengine.rc.EventType.ECONOMIC_MEASUREMENT_RECORDED)
    obs_time = oengine.utc_iso(observation.get("observed_at_utc") or observation.get("recorded_at_utc"))
    observed_hash = oengine.config_hash(oengine.observed_config(observation, package))
    drift = bool(previous and previous.payload.get("observed_config_hash") != observed_hash and obs_time > previous.recorded_at_utc)
    return entries, previous, prior, drift


class RunTests(unittest.TestCase):
    def test_history_and_drift_equal_the_oracle_for_every_run_of_every_scenario(self):
        seen = {"runs": 0, "drift": 0, "previous": 0, "prior_measurement": 0}
        original = analyzer.run
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            package = scenario["package_doc"]
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                for step in scenario["runs"]:
                    observation = step.get("observation")
                    checked = []

                    def checking_run(context, _original=original, _out=out, _checked=checked, _package=package, _observation=observation):
                        result = _original(context)
                        _checked.append((context, result))
                        if _observation:
                            entries, previous, prior, drift = oracle_reference(_package, _observation, _out, context.realization_package_id)
                            self.assertEqual([e.to_dict() for e in result.history.entries], [e.to_dict() for e in entries])
                            self.assertEqual(None if previous is None else previous.ledger_entry_id,
                                             None if result.history.previous_application is None else result.history.previous_application.ledger_entry_id)
                            self.assertEqual(None if prior is None else prior.ledger_entry_id,
                                             None if result.history.prior_measurement is None else result.history.prior_measurement.ledger_entry_id)
                            self.assertEqual(result.config_drift_observed, drift)
                        return result

                    with mock.patch.object(engine.analyzer, "run", checking_run):
                        engine.realize(copy.deepcopy(package), copy.deepcopy(observation), out,
                                       policy=None if step.get("policy") is None else engine.RealizerPolicy.from_mapping(step["policy"]),
                                       replay_through=step.get("replay_through"))
                    context, result = checked[0]
                    self.assertIsInstance(result, RealizationAnalysis)
                    if not observation:
                        self.assertIsNone(result.observation)
                        self.assertFalse(result.config_drift_observed)
                        continue
                    # The ledger at that moment holds the earlier runs and the lineage entry; the run's own appends are later.
                    self.assertIsInstance(result.observation, ObservationAnalysis)
                    seen["runs"] += 1
                    seen["drift"] += result.config_drift_observed
                    seen["previous"] += result.history.previous_application is not None
                    seen["prior_measurement"] += result.history.prior_measurement is not None
        self.assertGreater(seen["runs"], 40)
        self.assertGreater(seen["drift"], 0, "the scenarios include a configuration drift")
        self.assertGreater(seen["previous"], 0)
        self.assertGreater(seen["prior_measurement"], 0, "the scenarios include a repeated measurement")

    def test_run_equals_the_oracle_inline_computation_on_a_prepared_ledger(self):
        package = fixture_package()
        base = oengine.demo_observation(package)
        sequence = [base, {**base, "observed_at_utc": "2026-10-10T12:00:00Z", "recorded_at_utc": "2026-10-10T12:00:00Z"},
                    {**base, "observed_at_utc": "2026-10-11T12:00:00Z", "recorded_at_utc": "2026-10-11T12:00:00Z", "config": {**base["config"], "warehouse_size": "SMALL"}}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for observation in sequence:
                context = engine.build_context(RealizationRequest(package, observation, out))
                result = analyzer.run(context)
                entries, previous, prior, drift = oracle_reference(package, observation, out, context.realization_package_id)
                self.assertEqual([e.to_dict() for e in result.history.entries], [e.to_dict() for e in entries])
                self.assertEqual(None if previous is None else previous.to_dict(),
                                 None if result.history.previous_application is None else result.history.previous_application.to_dict())
                self.assertEqual(None if prior is None else prior.to_dict(),
                                 None if result.history.prior_measurement is None else result.history.prior_measurement.to_dict())
                self.assertEqual(result.config_drift_observed, drift)
                engine.realize(package, observation, out)
        self.assertTrue(drift, "the last observation drifts from the recorded application")

    def test_lineage_only_run_has_history_but_no_observation(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            engine.realize(package, oengine.demo_observation(package), out)
            for observation in (None, {}):
                result = analyzer.run(engine.build_context(RealizationRequest(package, observation, out)))
                self.assertIsNone(result.observation)
                self.assertFalse(result.config_drift_observed)
                self.assertTrue(result.history.entries)
                self.assertIsNotNone(result.history.previous_application)

    def test_run_is_read_only(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            engine.realize(package, oengine.demo_observation(package), out)
            before = tree(out)
            analyzer.run(engine.build_context(RealizationRequest(package, oengine.demo_observation(package), out)))
            self.assertEqual(tree(out), before)
            empty = Path(tmp) / "never-written"
            result = analyzer.run(engine.build_context(RealizationRequest(package, oengine.demo_observation(package), empty)))
            self.assertEqual(result.history, RealizationHistory(entries=(), previous_application=None, prior_measurement=None))
            self.assertFalse(empty.exists())

    def test_malformed_observation_raises_before_any_write(self):
        package = fixture_package()
        for patch in ({"observed_at_utc": None, "recorded_at_utc": None}, {"config": 5}, {"observed_at_utc": "nope"}):
            observation = {**oengine.demo_observation(package), **patch}
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                new = outcome(analyzer.run, engine.build_context(RealizationRequest(package, observation, out)))
                self.assertNotEqual(new[0], "ok")
                self.assertEqual(tree(out), {})

    def test_corrupt_ledger_raises_the_oracle_exception(self):
        package = fixture_package()
        observation = oengine.demo_observation(package)
        for text in ('{"event_type": "NOPE"}\n', "not json\n", '{"event_type": "REALIZATION_CREATED"}\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                ctx = engine.build_context(RealizationRequest(package, observation, Path(a)))
                path = engine.ledger_path(Path(a), ctx.realization_package_id)
                path.parent.mkdir(parents=True)
                path.write_text(text, encoding="utf-8")
                new = outcome(analyzer.run, ctx)
                old = outcome(oengine.load_ledger, path)
                self.assertEqual(new, old)
                self.assertNotEqual(new[0], "ok")

    def test_analysis_is_frozen(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            result = analyzer.run(engine.build_context(RealizationRequest(package, oengine.demo_observation(package), Path(tmp))))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.config_drift_observed = True
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.history.entries = ()


if __name__ == "__main__":
    unittest.main()
