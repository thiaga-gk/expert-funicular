"""Realizer collector (src/realizer/analyzer/collector/collector.py): observation replay and the demo observation.

The Realizer has no live collection, so the collector only loads a normalized observation file and builds the
deterministic demo observation; both equal the frozen oracle's behavior, including their errors.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome  # noqa: E402
from realizer.analyzer.collector import collector  # noqa: E402


class LoadObservationTests(unittest.TestCase):
    def test_engine_reexports_the_collector_names(self):
        self.assertIs(engine.demo_observation, collector.demo_observation)
        self.assertIs(engine.load_observation, collector.load_observation)

    def test_loads_every_scenario_observation_like_the_oracle_json_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in canonical.scenario_names():
                for i, run in enumerate(canonical.load_scenario(name)["runs"]):
                    path = Path(tmp) / f"{name}-{i}.json"
                    path.write_text(json.dumps(run.get("observation"), sort_keys=True), encoding="utf-8")
                    self.assertEqual(collector.load_observation(path), oengine.load_json(path))
                    self.assertEqual(collector.load_observation(str(path)), run.get("observation"))

    def test_errors_equal_the_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing, bad, empty = Path(tmp) / "missing.json", Path(tmp) / "bad.json", Path(tmp) / "empty.json"
            bad.write_text("{not json", encoding="utf-8")
            empty.write_text("", encoding="utf-8")
            for path in (missing, bad, empty, Path(tmp)):
                with self.subTest(path=path.name):
                    new, old = outcome(collector.load_observation, path), outcome(oengine.load_json, path)
                    self.assertEqual(new[0], old[0])
                    self.assertNotEqual(new[0], "ok")
        self.assertEqual(outcome(collector.load_observation, None)[0], outcome(oengine.load_json, None)[0])


class DemoObservationTests(unittest.TestCase):
    def test_equals_the_oracle_for_every_fixture_package(self):
        packages = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(canonical.PACKAGES.glob("*.json"))}
        self.assertGreaterEqual(len(packages), 2)
        for name, package in packages.items():
            for modified in (False, True):
                with self.subTest(package=name, modified=modified):
                    new = collector.demo_observation(copy.deepcopy(package), modified=modified)
                    self.assertEqual(new, oengine.demo_observation(copy.deepcopy(package), modified=modified))
                    self.assertEqual(json.dumps(new, sort_keys=True), json.dumps(oengine.demo_observation(package, modified=modified), sort_keys=True))

    def test_malformed_packages_raise_the_oracle_exception(self):
        good = fixture_package()
        variants = [
            {}, {"current_config": None}, {"current_config": "x"}, {**good, "provenance": {"generated_at": "not a time"}},
            {**good, "provenance": {"generated_at": 5}}, {**good, "provenance": None}, {**good, "provenance": "x"},
            {**good, "provenance": {"generated_at": ""}}, {**good, "scenarios": 5}, {**good, "scenarios": ["x"]},
            {**good, "recommended_plan": {"scenario_id": "none"}}, {k: v for k, v in good.items() if k != "scenarios"},
            {**good, "scenarios": [{"scenario_id": good["recommended_plan"]["scenario_id"], "execution_plan": [{"step_type": "CANDIDATE", "knob": None}]}]},
            {**good, "scenarios": [{"scenario_id": good["recommended_plan"]["scenario_id"], "execution_plan": [{"step_type": "CANDIDATE", "execution_state": "BLOCKED"}]}]},
        ]
        failures = 0
        for variant in variants:
            for modified in (False, True):
                with self.subTest(variant=sorted(variant), modified=modified):
                    new = outcome(collector.demo_observation, copy.deepcopy(variant), modified=modified)
                    old = outcome(oengine.demo_observation, copy.deepcopy(variant), modified=modified)
                    self.assertEqual(new, old)
                    failures += new[0] != "ok"
        self.assertGreater(failures, 6)

    def test_demo_observation_is_deterministic_and_does_not_mutate_the_package(self):
        package = fixture_package()
        frozen = copy.deepcopy(package)
        self.assertEqual(collector.demo_observation(package), collector.demo_observation(package))
        self.assertEqual(package, frozen)


if __name__ == "__main__":
    unittest.main()
