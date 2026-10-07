"""Realizer analyzer capability (src/realizer/analyzer/analyzer.py): ``run`` and its ``RealizationAnalysis``.

``run`` reads the ledger a run starts from and analyzes the observation. Its history and drift flag equal what is
computed independently from the raw ledger lines at the same moment for every run of the 41 scenarios, a lineage-only
run has no observation, and it writes nothing.
"""
import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import stage_policy, as_line, canonical, fixture_package, ledger_lines, observation_gate_open, outcome, tree  # noqa: E402
from realizer.analyzer import analysis, analyzer  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.utils import parse_utc, utc_iso  # noqa: E402
from realizer.contracts.analysis import ObservationAnalysis, RealizationAnalysis, RealizationHistory  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.packager.ledger import ledger_path  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402

APPLICATIONS = ("APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED")


def reference(package, observation, out):
    """The history and drift a run starts from, computed from the raw ledger lines on disk (recorded time, then the file
    order, governs the order), independently of the ledger loader, ``summarize_history`` and ``config_drift``."""
    lines = sorted(ledger_lines(out), key=lambda line: parse_utc(line["recorded_at_utc"]))
    previous = next((line for line in reversed(lines) if line["event_type"] in APPLICATIONS), None)
    prior = next((line for line in reversed(lines) if line["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED"), None)
    obs_time = utc_iso(observation.get("observed_at_utc") or observation.get("recorded_at_utc"))
    observed_hash = analysis.config_hash(analysis.observed_config(observation, package))
    drift = bool(previous and previous["payload"].get("observed_config_hash") != observed_hash and parse_utc(obs_time) > parse_utc(previous["recorded_at_utc"]))
    return lines, previous, prior, drift


def line_of(entry):
    return None if entry is None else as_line(entry)


class RunTests(unittest.TestCase):
    def test_history_and_drift_equal_the_raw_ledger_reference_for_every_run_of_every_scenario(self):
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
                            lines, previous, prior, drift = reference(_package, _observation, _out)
                            self.assertEqual([as_line(e) for e in result.history.entries], lines)
                            self.assertEqual(None if previous is None else previous["ledger_entry_id"],
                                             None if result.history.previous_application is None else result.history.previous_application.ledger_entry_id)
                            self.assertEqual(None if prior is None else prior["ledger_entry_id"],
                                             None if result.history.prior_measurement is None else result.history.prior_measurement.ledger_entry_id)
                            self.assertEqual(result.config_drift_observed, drift)
                        return result

                    with mock.patch.object(analyzer, "run", checking_run):
                        realize(copy.deepcopy(package), copy.deepcopy(observation), out,
                                policy=None if step.get("policy") is None else stage_policy(step["policy"]),
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

    def test_run_equals_the_raw_ledger_reference_on_a_prepared_ledger(self):
        package = fixture_package()
        base = demo_observation(package)
        sequence = [base, {**base, "observed_at_utc": "2026-10-10T12:00:00Z", "recorded_at_utc": "2026-10-10T12:00:00Z"},
                    {**base, "observed_at_utc": "2026-10-11T12:00:00Z", "recorded_at_utc": "2026-10-11T12:00:00Z", "config": {**base["config"], "warehouse_size": "SMALL"}}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for observation in sequence:
                context = build_context(RealizationRequest(package, observation, out))
                result = analyzer.run(context)
                lines, previous, prior, drift = reference(package, observation, out)
                self.assertEqual([as_line(e) for e in result.history.entries], lines)
                self.assertEqual(previous, line_of(result.history.previous_application))
                self.assertEqual(prior, line_of(result.history.prior_measurement))
                self.assertEqual(result.config_drift_observed, drift)
                realize(package, observation, out)
        self.assertTrue(drift, "the last observation drifts from the recorded application")

    def test_lineage_only_run_has_history_but_no_observation(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(package, demo_observation(package), out)
            for observation in (None, {}):
                result = analyzer.run(build_context(RealizationRequest(package, observation, out)))
                self.assertIsNone(result.observation)
                self.assertFalse(result.config_drift_observed)
                self.assertTrue(result.history.entries)
                self.assertIsNotNone(result.history.previous_application)

    def test_run_is_read_only(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(package, demo_observation(package), out)
            before = tree(out)
            analyzer.run(build_context(RealizationRequest(package, demo_observation(package), out)))
            self.assertEqual(tree(out), before)
            empty = Path(tmp) / "never-written"
            result = analyzer.run(build_context(RealizationRequest(package, demo_observation(package), empty)))
            self.assertEqual(result.history, RealizationHistory(entries=(), previous_application=None, prior_measurement=None))
            self.assertFalse(empty.exists())

    def test_malformed_observation_raises_before_any_write(self):
        """The Analyzer's own handling: the loader's observation gate refuses these first (see ``tests/unit/realizer/config``)."""
        package = fixture_package()
        for patch in ({"observed_at_utc": None, "recorded_at_utc": None}, {"config": 5}, {"observed_at_utc": "nope"}):
            observation = {**demo_observation(package), **patch}
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                with observation_gate_open():
                    context = build_context(RealizationRequest(package, observation, out))
                new = outcome(analyzer.run, context)
                self.assertNotEqual(new[0], "ok")
                self.assertEqual(tree(out), {})

    def test_corrupt_ledger_raises_the_stored_exception(self):
        package = fixture_package()
        observation = demo_observation(package)
        for text in ('{"event_type": "NOPE"}\n', "not json\n", '{"event_type": "REALIZATION_CREATED"}\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as a:
                ctx = build_context(RealizationRequest(package, observation, Path(a)))
                path = ledger_path(Path(a), ctx.realization_package_id)
                path.parent.mkdir(parents=True)
                path.write_text(text, encoding="utf-8")
                new = outcome(analyzer.run, ctx)
                self.assertNotEqual(new[0], "ok")
                expect.check(self, text.strip(), new)

    def test_analysis_is_frozen(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            result = analyzer.run(build_context(RealizationRequest(package, demo_observation(package), Path(tmp))))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.config_drift_observed = True
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.history.entries = ()


if __name__ == "__main__":
    unittest.main()
