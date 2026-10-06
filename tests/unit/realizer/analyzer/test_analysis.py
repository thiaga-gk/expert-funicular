"""Realizer analyzer logic (src/realizer/analyzer/analysis.py): configuration normalization, hashes, the source-package
view and the observation analysis.

Every moved function returns, and raises, exactly what the frozen single-file oracle's copy did, including on malformed
input; ``analyze_observation`` reproduces the evidence the oracle wrote for every observation of the 41 scenarios.
"""
import copy
import dataclasses
import json
import unittest

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome  # noqa: E402
from realizer.analyzer import analysis  # noqa: E402

MOVED = (
    "normalize_scalar", "config_hash", "photon_bool_from_package", "package_config", "observed_config", "scenario_by_id",
    "recommended_scenario", "candidate_by_id", "candidate_steps",
)


class MovedFunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.plan_scenario = (cls.pkg["recommended_plan"] or {}).get("scenario_id")

    def assert_same(self, name, *args, **kwargs):
        new = outcome(getattr(analysis, name), *copy.deepcopy(args), **kwargs)
        old = outcome(getattr(oengine, name), *copy.deepcopy(args), **kwargs)
        self.assertEqual(new, old, f"{name}{args!r}")
        return new

    def test_names_are_the_engines_and_the_engine_reexports_them(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertIs(getattr(engine, name), getattr(analysis, name))
                self.assertTrue(callable(getattr(oengine, name)))

    def test_normalize_scalar(self):
        for value in ["medium", " large ", "LARGE", "pro", "Classic", "serverless", "current", "PREVIEW", "xx", "", None, 4, True, 2.5]:
            with self.subTest(value=value):
                self.assert_same("normalize_scalar", value)
        # Pinned oracle quirk, kept: the membership test is on the unstripped value, so a padded size stays padded.
        self.assertEqual(analysis.normalize_scalar(" large "), " large ")
        self.assertEqual(analysis.normalize_scalar("large"), "LARGE")

    def test_config_hash_on_valid_and_malformed_input(self):
        configs = [{}, {"warehouse_size": "small"}, {"warehouse_size": "SMALL", "enable_photon": None}, analysis.package_config(self.pkg),
                   {"min_clusters": 1, "unknown": object()}]
        for cfg in configs:
            with self.subTest(config=cfg):
                self.assertEqual(self.assert_same("config_hash", cfg)[0], "ok")
        for bad in [None, [], "x", 5, {"warehouse_size": {1, 2}}, {"warehouse_size": object()}]:
            with self.subTest(bad=bad):
                self.assert_same("config_hash", bad)
        self.assertEqual(self.assert_same("config_hash", {"warehouse_size": object()})[0], "TypeError")

    def test_package_config_and_photon(self):
        self.assertEqual(self.assert_same("package_config", self.pkg)[0], "ok")
        variants = [
            {}, {"current_config": None}, {"current_config": {}}, {"current_config": []}, {"current_config": "x"},
            {"current_config": {"photon": "ENABLED"}}, {"current_config": {"photon": {"status": "enabled"}}},
            {"current_config": {"photon": {"status": "DISABLED"}}}, {"current_config": {"photon": {"status": None}}},
            {"current_config": {"photon": {"status": 5}}}, {"current_config": {"photon": []}},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_same("package_config", variant)
                self.assert_same("photon_bool_from_package", variant)
        self.assertEqual(self.assert_same("package_config", {})[0], "KeyError")
        self.assertEqual(self.assert_same("package_config", {"current_config": []})[0], "AttributeError")
        self.assertEqual(self.assert_same("photon_bool_from_package", {"current_config": {"photon": "ENABLED"}})[0], "AttributeError")
        # ``photon_bool_from_package`` is lenient where ``package_config`` is strict: an absent current_config is fine.
        self.assertEqual(self.assert_same("photon_bool_from_package", {}), ("ok", None))

    def test_observed_config(self):
        for observation in [
            {}, {"config": None}, {"config": {}}, {"config": {"warehouse_size": "small", "min_clusters": 3}},
            {"config": {"enable_photon": False, "warehouse_type": "serverless", "auto_stop_minutes": None}},
            {"config": [("warehouse_size", "x")]}, {"config": []}, {"config": ["a"]}, {"config": "ab"}, {"config": 5}, {"config": True},
        ]:
            with self.subTest(observation=observation):
                self.assert_same("observed_config", observation, self.pkg)
        for package in [{}, {"current_config": None}, {"current_config": "x"}]:
            with self.subTest(package=package):
                self.assert_same("observed_config", {"config": {"warehouse_size": "x"}}, package)
        self.assertEqual(self.assert_same("observed_config", {"config": 5}, self.pkg)[0], "TypeError")
        # Omitted knobs inherit the source package's, not a recommendation.
        self.assertEqual(analysis.observed_config({}, self.pkg), analysis.package_config(self.pkg))

    def test_plan_lookups_on_the_fixture_and_on_malformed_packages(self):
        plan = self.pkg["recommended_plan"]["scenario_id"]
        candidate = self.pkg["candidates"][0]["candidate_id"]
        for sid in [None, plan, "SCN-MISSING", "", 5]:
            self.assert_same("scenario_by_id", self.pkg, sid)
            self.assert_same("candidate_steps", self.pkg, sid)
        for cid in [None, candidate, "CAND-MISSING"]:
            self.assert_same("candidate_by_id", self.pkg, cid)
        self.assert_same("recommended_scenario", self.pkg)
        self.assertEqual(self.assert_same("candidate_steps", self.pkg)[0], "ok")
        self.assertTrue(analysis.candidate_steps(self.pkg), "the fixture plan has candidate steps")
        for variant in [
            {}, {"scenarios": None}, {"scenarios": 5}, {"scenarios": ["x"]}, {"scenarios": [None]}, {"scenarios": [{"scenario_id": "S"}]},
            {"scenarios": [{"scenario_id": "S", "execution_plan": 5}]}, {"scenarios": [{"scenario_id": "S", "execution_plan": [1]}]},
            {"scenarios": [{"scenario_id": "S", "execution_plan": [{"step_type": "CANDIDATE"}, {"step_type": "X"}]}], "recommended_plan": {"scenario_id": "S"}},
            {"scenarios": [{"scenario_id": "S"}], "recommended_plan": None}, {"scenarios": [{"scenario_id": "S"}], "recommended_plan": "x"},
            {"candidates": None}, {"candidates": [3]}, {"candidates": [{"candidate_id": "C"}]},
        ]:
            with self.subTest(variant=variant):
                self.assert_same("recommended_scenario", variant)
                self.assert_same("scenario_by_id", variant, "S")
                self.assert_same("candidate_steps", variant)
                self.assert_same("candidate_steps", variant, "S")
                self.assert_same("candidate_by_id", variant, "C")
        self.assertEqual(self.assert_same("scenario_by_id", {"scenarios": 5}, "S")[0], "TypeError")
        self.assertEqual(self.assert_same("scenario_by_id", {"scenarios": ["x"]}, "S")[0], "AttributeError")
        # Lazy: an unknown scenario id never reads the scenarios list, and a missing key defaults to empty.
        self.assertEqual(analysis.scenario_by_id({"scenarios": 5}, None), None)
        self.assertEqual(analysis.candidate_by_id({"candidates": 5}, None), None)
        self.assertEqual(analysis.candidate_steps({}), [])


class AnalyzeObservationTests(unittest.TestCase):
    """``analyze_observation`` gathers the engine's first steps (times, configurations, hashes, evidence record)."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.obs = oengine.demo_observation(cls.pkg)

    @staticmethod
    def oracle_inline(package, observation):
        """The oracle's own first statements of ``process_observation``, in order, as the reference."""
        utc_iso, rc = oengine.utc_iso, oengine.rc
        obs_time = utc_iso(observation.get("observed_at_utc") or observation.get("recorded_at_utc"))
        recorded = utc_iso(observation.get("recorded_at_utc") or obs_time)
        effective = utc_iso(observation.get("applied_at_utc") or observation.get("effective_at_utc") or obs_time)
        obs_cfg = oengine.observed_config(observation, package)
        source_cfg = oengine.package_config(package)
        source_hash, observed_hash = oengine.config_hash(source_cfg), oengine.config_hash(obs_cfg)
        evidence_payload = {"config": obs_cfg, "observed_at_utc": obs_time, "source": observation.get("source", "NORMALIZED_OBSERVATION")}
        revid = oengine.make_evidence_id("OBSERVED_CONFIGURATION", package, observation, evidence_payload)
        return {
            "times": (obs_time, recorded, effective), "obs_cfg": obs_cfg, "source_cfg": source_cfg,
            "hashes": (source_hash, observed_hash), "revid": revid,
            "record": {
                "evidence_id": revid, "evidence_type": "OBSERVED_CONFIGURATION",
                "workspace_id": package["warehouse"]["workspace_id"], "warehouse_id": package["warehouse"]["warehouse_id"],
                "source": observation.get("source", "NORMALIZED_OBSERVATION"),
                "observation_window": observation.get("window") or {"end_utc": obs_time},
                "captured_at_utc": recorded, "quality": str(observation.get("evidence_quality", "SUFFICIENT")).upper(),
                "payload_hash": "SHA256:" + rc.sha256_hex(evidence_payload), "payload": evidence_payload,
            },
        }

    @staticmethod
    def flatten(a):
        return {"times": (a.observed_at_utc, a.recorded_at_utc, a.effective_at_utc), "obs_cfg": a.observed_config,
                "source_cfg": a.source_config, "hashes": (a.source_config_hash, a.observed_config_hash),
                "revid": a.evidence_id, "record": a.evidence_record}

    def test_matches_the_oracle_statements_on_valid_observations(self):
        variants = [
            {}, {"observed_at_utc": None}, {"recorded_at_utc": "2026-10-10T01:00:00+05:30"}, {"applied_at_utc": None, "effective_at_utc": "2026-10-03T00:00:00Z"},
            {"source": "LIVE"}, {"window": {"start_utc": "a", "end_utc": "b"}}, {"evidence_quality": "partial"}, {"evidence_quality": None},
            {"config": {"warehouse_size": "small"}}, {"config": {}}, {"observed_at_utc": "2026-10-09T12:00:00"},
        ]
        for patch in variants:
            observation = {**copy.deepcopy(self.obs), **patch}
            with self.subTest(patch=patch):
                expected = outcome(self.oracle_inline, self.pkg, copy.deepcopy(observation))
                got = outcome(lambda: self.flatten(analysis.analyze_observation(self.pkg, observation)))
                self.assertEqual(got, expected)
                self.assertEqual(got[0], "ok")

    def test_malformed_observations_raise_the_oracle_exception(self):
        malformed = [
            {"observed_at_utc": None, "recorded_at_utc": None}, {"observed_at_utc": "not a time"}, {"recorded_at_utc": 5},
            {"applied_at_utc": "bad"}, {"config": 5}, {"config": ["x"]}, {"source": object()}, {"source": {1, 2}},
            {"window": object()}, {"evidence_quality": object()},
        ]
        for patch in malformed:
            observation = {**copy.deepcopy(self.obs), **patch}
            with self.subTest(patch=patch):
                expected = outcome(self.oracle_inline, self.pkg, observation)
                self.assertEqual(outcome(lambda: self.flatten(analysis.analyze_observation(self.pkg, observation))), expected)
        for package in [{}, {**self.pkg, "current_config": "x"}, {k: v for k, v in self.pkg.items() if k != "warehouse"},
                        {**self.pkg, "warehouse": {"workspace_id": "w"}}]:
            with self.subTest(package=sorted(package)):
                expected = outcome(self.oracle_inline, package, self.obs)
                self.assertEqual(outcome(lambda: self.flatten(analysis.analyze_observation(package, self.obs))), expected)
                self.assertNotEqual(expected[0], "ok")

    def test_result_is_frozen_and_keeps_the_raw_observation(self):
        result = analysis.analyze_observation(self.pkg, self.obs)
        self.assertIs(result.raw, self.obs)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.evidence_id = "x"

    def test_evidence_of_every_scenario_observation_equals_what_the_oracle_wrote(self):
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
