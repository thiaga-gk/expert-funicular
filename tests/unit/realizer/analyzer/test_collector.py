"""Realizer collector (src/realizer/analyzer/collector/collector.py): observation replay and the demo observation.

The Realizer has no live collection, so the collector only loads a normalized observation file and builds the
deterministic demo observation; both give the results stored in ``tests/fixtures/realizer/expectations/`` (generated
from the single-file release), including their errors.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import canonical, fixture_package, outcome  # noqa: E402
from realizer.analyzer.collector import collector  # noqa: E402
from realizer.config import loader  # noqa: E402
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION, observation_schema  # noqa: E402


class LoadObservationTests(unittest.TestCase):
    def test_loads_every_scenario_observation_from_its_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in canonical.scenario_names():
                for i, run in enumerate(canonical.load_scenario(name)["runs"]):
                    path = Path(tmp) / f"{name}-{i}.json"
                    path.write_text(json.dumps(run.get("observation"), sort_keys=True), encoding="utf-8")
                    self.assertEqual(collector.load_observation(path), run.get("observation"))
                    self.assertEqual(collector.load_observation(str(path)), run.get("observation"))

    def test_errors(self):
        """The exception type is stored (its message names the path, which a temporary directory makes unstable)."""
        with tempfile.TemporaryDirectory() as tmp:
            missing, bad, empty = Path(tmp) / "missing.json", Path(tmp) / "bad.json", Path(tmp) / "empty.json"
            bad.write_text("{not json", encoding="utf-8")
            empty.write_text("", encoding="utf-8")
            for label, path in (("missing file", missing), ("invalid JSON", bad), ("empty file", empty), ("a directory", Path(tmp))):
                with self.subTest(path=label):
                    new = outcome(collector.load_observation, path)
                    self.assertNotEqual(new[0], "ok")
                    expect.check(self, label, new[0])
        expect.check(self, "None", outcome(collector.load_observation, None)[0])


class DemoObservationTests(unittest.TestCase):
    def test_for_every_fixture_package(self):
        packages = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(canonical.PACKAGES.glob("*.json"))}
        self.assertGreaterEqual(len(packages), 2)
        for name, package in packages.items():
            for modified in (False, True):
                with self.subTest(package=name, modified=modified):
                    new = collector.demo_observation(copy.deepcopy(package), modified=modified)
                    expect.check(self, f"{name} modified={modified}", new)

    def test_the_demo_observation_is_a_complete_valid_2_0_0_observation_of_the_package(self):
        for path in sorted(canonical.PACKAGES.glob("*.json")):
            package = json.loads(path.read_text(encoding="utf-8"))
            for modified in (False, True):
                with self.subTest(package=path.name, modified=modified):
                    observation = collector.demo_observation(package, modified=modified)
                    self.assertEqual(observation["observation_format_version"], OBSERVATION_FORMAT_VERSION)
                    self.assertEqual(sorted(observation), observation_schema()["required"])
                    self.assertEqual((observation["workspace_id"], observation["warehouse_id"]),
                                     (package["warehouse"]["workspace_id"], package["warehouse"]["warehouse_id"]))
                    self.assertEqual(observation["economics"]["currency"], package["package_economics"]["currency"])
                    self.assertEqual(sorted(observation["config"]), sorted(["warehouse_type", "warehouse_size", "min_clusters", "max_clusters",
                                                                           "auto_stop_minutes", "warehouse_channel", "enable_photon"]))
                    loader.validate_observation(observation, package)

    def test_the_demo_costs_stay_positive_for_a_large_published_saving(self):
        """Savings of 365,000 a year are 30,000 over the demo's 30 days: the old cost is twice them, the new cost stays positive."""
        package = fixture_package()
        step = next(s for s in package["scenarios"][0]["execution_plan"] if s["step_type"] == "CANDIDATE")
        candidate = next(c for c in package["candidates"] if c["candidate_id"] == step["candidate_id"])
        candidate["independent_economics"]["savings_365d"]["total"]["base"] = 365000
        economics = collector.demo_observation(package)["economics"]
        self.assertEqual(economics["counterfactual_old_cost"], "60000.00")
        self.assertEqual(economics["observed_new_cost"], "30000.00")

    def test_malformed_packages_raise_the_stored_exception(self):
        good = fixture_package()
        plan = good["recommended_plan"]["scenario_id"]
        variants = [
            ("empty package", {}), ("current_config None", {"current_config": None}), ("current_config text", {"current_config": "x"}),
            ("generated_at not a time", {**good, "provenance": {"generated_at": "not a time"}}),
            ("generated_at a number", {**good, "provenance": {"generated_at": 5}}), ("provenance None", {**good, "provenance": None}),
            ("provenance text", {**good, "provenance": "x"}), ("generated_at empty", {**good, "provenance": {"generated_at": ""}}),
            ("scenarios a number", {**good, "scenarios": 5}), ("scenarios text items", {**good, "scenarios": ["x"]}),
            ("recommended plan names no scenario", {**good, "recommended_plan": {"scenario_id": "none"}}),
            ("no scenarios key", {k: v for k, v in good.items() if k != "scenarios"}),
            ("candidate step without knob", {**good, "scenarios": [{"scenario_id": plan, "execution_plan": [{"step_type": "CANDIDATE", "knob": None}]}]}),
            ("candidate step blocked", {**good, "scenarios": [{"scenario_id": plan, "execution_plan": [{"step_type": "CANDIDATE", "execution_state": "BLOCKED"}]}]}),
        ]
        failures = 0
        for label, variant in variants:
            for modified in (False, True):
                with self.subTest(variant=label, modified=modified):
                    new = outcome(collector.demo_observation, copy.deepcopy(variant), modified=modified)
                    expect.check(self, f"{label} modified={modified}", new)
                    failures += new[0] != "ok"
        self.assertGreater(failures, 6)

    def test_demo_observation_is_deterministic_and_does_not_mutate_the_package(self):
        package = fixture_package()
        frozen = copy.deepcopy(package)
        self.assertEqual(collector.demo_observation(package), collector.demo_observation(package))
        self.assertEqual(package, frozen)


if __name__ == "__main__":
    unittest.main()
