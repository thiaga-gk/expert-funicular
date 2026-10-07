"""Realizer analyzer logic (src/realizer/analyzer/analysis.py): configuration normalization, hashes, the source-package
view and the observation analysis.

Every function returns, and raises, what is stored in ``tests/fixtures/realizer/expectations/`` (generated from the
single-file release), including on malformed input; ``analyze_observation`` reproduces the evidence the stored
baseline holds for every observation of the 41 scenarios.
"""
import copy
import dataclasses
import json
import re
import unittest

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, canonical, fixture_package, outcome  # noqa: E402
from realizer.analyzer import analysis  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402


def label_of(*args):
    """A stable, readable label for a case: the arguments' repr without memory addresses."""
    return re.sub(r" at 0x[0-9a-f]+", "", repr(args if len(args) != 1 else args[0]))


class FunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.plan_scenario = (cls.pkg["recommended_plan"] or {}).get("scenario_id")

    def check(self, name, *args, label=None, **kwargs):
        """The ``(status, value or message)`` outcome of ``analysis.<name>(*args)``, checked against the stored one.
        *label* names a case whose arguments are too large to print (the fixture package)."""
        new = outcome(getattr(analysis, name), *copy.deepcopy(args), **kwargs)
        expect.check(self, f"{name}({label if label is not None else label_of(*args)})", new)
        return new

    def test_normalize_value(self):
        for knob in ["warehouse_size", "warehouse_type", "warehouse_channel", "auto_stop_minutes", "enable_photon"]:
            for value in ["medium", " large ", "LARGE", "X-Large", "2X-Large", "pro", "Classic", "serverless", "current", "PREVIEW", "xx", "", None, 4, True, 2.5]:
                with self.subTest(knob=knob, value=value):
                    self.check("normalize_value", knob, value)
        # One spelling per value, the Publisher's: sizes upper-cased with "-" as "_", type and channel upper-cased, spaces dropped.
        self.assertEqual(analysis.normalize_value("warehouse_size", "X-Large"), "X_LARGE")
        self.assertEqual(analysis.normalize_value("warehouse_size", " medium "), "MEDIUM")
        self.assertEqual(analysis.normalize_value("warehouse_size", "2x-large"), "2X_LARGE")
        self.assertEqual(analysis.normalize_value("warehouse_type", " pro "), "PRO")
        self.assertEqual(analysis.normalize_value("warehouse_channel", "preview"), "PREVIEW")
        # Other knobs and non-strings pass through unchanged.
        self.assertEqual(analysis.normalize_value("enable_photon", True), True)
        self.assertEqual(analysis.normalize_value("auto_stop_minutes", 0), 0)
        self.assertIsNone(analysis.normalize_value("warehouse_size", None))
        self.assertEqual(analysis.normalize_value("auto_stop_minutes", " 60 "), " 60 ")

    def test_package_and_observed_config_share_one_spelling(self):
        package = copy.deepcopy(self.pkg)
        package["current_config"]["warehouse_size"] = "X-Large"
        package["current_config"]["warehouse_type"] = "pro"
        self.assertEqual(analysis.package_config(package)["warehouse_size"], "X_LARGE")
        self.assertEqual(analysis.package_config(package)["warehouse_type"], "PRO")
        observed = analysis.observed_config({"config": {"warehouse_size": " X-Large ", "warehouse_type": "Pro"}}, package)
        self.assertEqual((observed["warehouse_size"], observed["warehouse_type"]), ("X_LARGE", "PRO"))
        self.assertEqual(analysis.config_hash({"warehouse_size": "X-Large"}), analysis.config_hash({"warehouse_size": " x_large "}))

    def test_config_hash_on_valid_and_malformed_input(self):
        configs = [{}, {"warehouse_size": "small"}, {"warehouse_size": "SMALL", "enable_photon": None}, analysis.package_config(self.pkg),
                   {"min_clusters": 1, "unknown": object()}]
        for cfg in configs:
            with self.subTest(config=cfg):
                self.assertEqual(self.check("config_hash", cfg)[0], "ok")
        for bad in [None, [], "x", 5, {"warehouse_size": {1, 2}}, {"warehouse_size": object()}]:
            with self.subTest(bad=bad):
                self.check("config_hash", bad)
        self.assertEqual(self.check("config_hash", {"warehouse_size": object()})[0], "TypeError")

    def test_package_config_and_photon(self):
        self.assertEqual(self.check("package_config", self.pkg, label="fixture package")[0], "ok")
        variants = [
            {}, {"current_config": None}, {"current_config": {}}, {"current_config": []}, {"current_config": "x"},
            {"current_config": {"photon": "ENABLED"}}, {"current_config": {"photon": {"status": "enabled"}}},
            {"current_config": {"photon": {"status": "DISABLED"}}}, {"current_config": {"photon": {"status": None}}},
            {"current_config": {"photon": {"status": 5}}}, {"current_config": {"photon": []}},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.check("package_config", variant)
                self.check("photon_bool_from_package", variant)
        self.assertEqual(self.check("package_config", {})[0], "KeyError")
        self.assertEqual(self.check("package_config", {"current_config": []})[0], "AttributeError")
        self.assertEqual(self.check("photon_bool_from_package", {"current_config": {"photon": "ENABLED"}})[0], "AttributeError")
        # ``photon_bool_from_package`` is lenient where ``package_config`` is strict: an absent current_config is fine.
        self.assertEqual(self.check("photon_bool_from_package", {}), ("ok", None))

    def test_observed_config(self):
        for observation in [
            {}, {"config": None}, {"config": {}}, {"config": {"warehouse_size": "small", "min_clusters": 3}},
            {"config": {"enable_photon": False, "warehouse_type": "serverless", "auto_stop_minutes": None}},
            {"config": [("warehouse_size", "x")]}, {"config": []}, {"config": ["a"]}, {"config": "ab"}, {"config": 5}, {"config": True},
        ]:
            with self.subTest(observation=observation):
                self.check("observed_config", observation, self.pkg, label=f"{label_of(observation)}, fixture package")
        for package in [{}, {"current_config": None}, {"current_config": "x"}]:
            with self.subTest(package=package):
                self.check("observed_config", {"config": {"warehouse_size": "x"}}, package)
        self.assertEqual(self.check("observed_config", {"config": 5}, self.pkg, label="config 5, fixture package")[0], "TypeError")
        # Omitted knobs inherit the source package's, not a recommendation.
        self.assertEqual(analysis.observed_config({}, self.pkg), analysis.package_config(self.pkg))

    def test_plan_lookups_on_the_fixture_and_on_malformed_packages(self):
        plan = self.pkg["recommended_plan"]["scenario_id"]
        candidate = self.pkg["candidates"][0]["candidate_id"]
        for sid in [None, plan, "SCN-MISSING", "", 5]:
            self.check("scenario_by_id", self.pkg, sid, label=f"fixture package, {sid!r}")
            self.check("candidate_steps", self.pkg, sid, label=f"fixture package, {sid!r}")
        for cid in [None, candidate, "CAND-MISSING"]:
            self.check("candidate_by_id", self.pkg, cid, label=f"fixture package, {cid!r}")
        self.check("recommended_scenario", self.pkg, label="fixture package")
        self.assertEqual(self.check("candidate_steps", self.pkg, label="fixture package")[0], "ok")
        self.assertTrue(analysis.candidate_steps(self.pkg), "the fixture plan has candidate steps")
        for variant in [
            {}, {"scenarios": None}, {"scenarios": 5}, {"scenarios": ["x"]}, {"scenarios": [None]}, {"scenarios": [{"scenario_id": "S"}]},
            {"scenarios": [{"scenario_id": "S", "execution_plan": 5}]}, {"scenarios": [{"scenario_id": "S", "execution_plan": [1]}]},
            {"scenarios": [{"scenario_id": "S", "execution_plan": [{"step_type": "CANDIDATE"}, {"step_type": "X"}]}], "recommended_plan": {"scenario_id": "S"}},
            {"scenarios": [{"scenario_id": "S"}], "recommended_plan": None}, {"scenarios": [{"scenario_id": "S"}], "recommended_plan": "x"},
            {"candidates": None}, {"candidates": [3]}, {"candidates": [{"candidate_id": "C"}]},
        ]:
            with self.subTest(variant=variant):
                self.check("recommended_scenario", variant)
                self.check("scenario_by_id", variant, "S")
                self.check("candidate_steps", variant)
                self.check("candidate_steps", variant, "S")
                self.check("candidate_by_id", variant, "C")
        self.assertEqual(self.check("scenario_by_id", {"scenarios": 5}, "S")[0], "TypeError")
        self.assertEqual(self.check("scenario_by_id", {"scenarios": ["x"]}, "S")[0], "AttributeError")
        # Lazy: an unknown scenario id never reads the scenarios list, and a missing key defaults to empty.
        self.assertEqual(analysis.scenario_by_id({"scenarios": 5}, None), None)
        self.assertEqual(analysis.candidate_by_id({"candidates": 5}, None), None)
        self.assertEqual(analysis.candidate_steps({}), [])


class AnalyzeObservationTests(unittest.TestCase):
    """``analyze_observation`` gathers the engine's first steps (times, configurations, hashes, evidence record)."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.obs = demo_observation(cls.pkg)

    @staticmethod
    def flatten(a):
        return {"times": (a.observed_at_utc, a.recorded_at_utc, a.effective_at_utc), "obs_cfg": a.observed_config,
                "source_cfg": a.source_config, "hashes": (a.source_config_hash, a.observed_config_hash),
                "revid": a.evidence_id, "record": a.evidence_record}

    def analyzed(self, package, observation):
        return outcome(lambda: self.flatten(analysis.analyze_observation(package, observation)))

    def test_normalized_view_of_valid_observations(self):
        variants = [
            {}, {"observed_at_utc": None}, {"recorded_at_utc": "2026-10-10T01:00:00+05:30"}, {"applied_at_utc": None, "effective_at_utc": "2026-10-03T00:00:00Z"},
            {"source": "LIVE"}, {"window": {"start_utc": "a", "end_utc": "b"}}, {"evidence_quality": "partial"}, {"evidence_quality": None},
            {"config": {"warehouse_size": "small"}}, {"config": {}}, {"observed_at_utc": "2026-10-09T12:00:00"},
        ]
        for patch in variants:
            observation = {**copy.deepcopy(self.obs), **patch}
            with self.subTest(patch=patch):
                got = self.analyzed(self.pkg, observation)
                self.assertEqual(got[0], "ok")
                expect.check(self, label_of(patch), got)

    def test_malformed_observations_raise_the_stored_exception(self):
        malformed = [
            {"observed_at_utc": None, "recorded_at_utc": None}, {"observed_at_utc": "not a time"}, {"recorded_at_utc": 5},
            {"applied_at_utc": "bad"}, {"config": 5}, {"config": ["x"]}, {"source": object()}, {"source": {1, 2}},
            {"window": object()}, {"evidence_quality": Opaque()},
        ]
        # ``evidence_quality`` is not an error: any value is turned into text, so it is an ``Opaque`` (stable text), not an ``object()``.
        for patch in malformed:
            observation = {**copy.deepcopy(self.obs), **patch}
            with self.subTest(patch=patch):
                expect.check(self, f"observation {label_of(patch)}", self.analyzed(self.pkg, observation))
        packages = [("empty package", {}), ("current_config is text", {**self.pkg, "current_config": "x"}),
                    ("no warehouse", {k: v for k, v in self.pkg.items() if k != "warehouse"}),
                    ("warehouse without a warehouse_id", {**self.pkg, "warehouse": {"workspace_id": "w"}})]
        for label, package in packages:
            with self.subTest(package=label):
                got = self.analyzed(package, self.obs)
                self.assertNotEqual(got[0], "ok")
                expect.check(self, f"package {label}", got)

    def test_result_is_frozen_and_keeps_the_raw_observation(self):
        result = analysis.analyze_observation(self.pkg, self.obs)
        self.assertIs(result.raw, self.obs)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.evidence_id = "x"

    def test_evidence_of_every_scenario_observation_equals_the_stored_baseline(self):
        checked = 0
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            for run in scenario["runs"]:
                if not run.get("observation"):
                    continue
                with self.subTest(scenario=name):
                    result = analysis.analyze_observation(scenario["package_doc"], run["observation"])
                    stored = canonical.EXPECTED / name / "realization" / "evidence" / f"{result.evidence_id}.json"
                    self.assertTrue(stored.exists(), stored)
                    self.assertEqual(json.loads(stored.read_text(encoding="utf-8")), result.evidence_record)
                    self.assertEqual(json.dumps(result.evidence_record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                     stored.read_text(encoding="utf-8"))
                    checked += 1
        self.assertGreater(checked, 40)


if __name__ == "__main__":
    unittest.main()
