"""The loader's input gates (src/realizer/config/loader.py): ``validate_observation`` and the added ``validate_package`` checks.

``realize`` runs ``validate_package`` and ``validate_observation`` in ``build_context``, before anything is written:
an observation that is not a complete, consistent format 2.0.0 observation of the package's warehouse, and a package
whose recommended scenario does not exist or whose warehouse type is not supported, raise ``ValueError`` and leave the
output directory untouched. A lineage-only run (no observation) skips the observation gate.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.helpers import canonical, fixture_package, observation_gate_open, tree
from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES  # noqa: E402
from publisher.contracts import reporter_contract as publisher_contract  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.config import loader  # noqa: E402
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION, observation_schema  # noqa: E402
from realizer.realizer import realize  # noqa: E402


def rehashed(package):
    """*package* with its id and full hash recomputed, so that only the semantic checks can refuse it."""
    payload = publisher_contract.package_hash_payload_from_document(package)
    package["recommendation_package_id"] = publisher_contract.content_id("PKG", payload)
    package["provenance"]["package_hash_full_sha256"] = publisher_contract.sha256_hex(payload)
    return package


class ObservationGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def refused(self, observation, message=None, package=None):
        """``validate_observation`` raises ``ValueError``, and ``realize`` raises it before writing anything."""
        package = self.pkg if package is None else package
        with self.assertRaises(ValueError) as caught:
            loader.validate_observation(copy.deepcopy(observation), package)
        if message is not None:
            self.assertIn(message, str(caught.exception))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(ValueError):
                realize(copy.deepcopy(package), copy.deepcopy(observation), out)
            self.assertEqual(tree(out), {}, "nothing is written for a refused observation")
        return str(caught.exception)

    def test_the_demo_observations_and_every_stored_scenario_observation_are_accepted(self):
        for modified in (False, True):
            loader.validate_observation(demo_observation(self.pkg, modified=modified), self.pkg)
        checked = 0
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            for run in scenario["runs"]:
                if run.get("observation"):
                    with self.subTest(scenario=name):
                        loader.validate_observation(run["observation"], scenario["package_doc"])
                        checked += 1
        self.assertGreater(checked, 40)

    def test_a_lineage_only_run_skips_the_observation_gate(self):
        for observation in (None, {}):
            with self.subTest(observation=observation), tempfile.TemporaryDirectory() as tmp:
                realize(copy.deepcopy(self.pkg), observation, Path(tmp))

    def test_an_observation_must_be_a_mapping(self):
        for observation in ([], "x", 5):
            with self.subTest(observation=observation), self.assertRaises(ValueError):
                loader.validate_observation(observation, self.pkg)

    def test_format_version(self):
        self.assertEqual(OBSERVATION_FORMAT_VERSION, "2.0.0")
        for version in ("1.0.0", "2.0.1", None, 2):
            with self.subTest(version=version):
                self.refused({**self.base, "observation_format_version": version}, "unsupported observation format")
        legacy = {k: v for k, v in self.base.items() if k != "observation_format_version"}
        self.refused(legacy, "unsupported observation format")

    def test_every_top_level_key_is_required(self):
        keys = sorted(observation_schema()["required"])
        self.assertEqual(sorted(self.base), keys, "the demo observation is complete")
        for key in keys:
            with self.subTest(missing=key):
                observation = {k: v for k, v in self.base.items() if k != key}
                if key == "observation_format_version":
                    self.refused(observation, "unsupported observation format")
                else:
                    self.refused(observation, "required")

    def test_nested_keys_are_required_too(self):
        for section in ("config", "performance", "financial_quality", "economics"):
            for key in sorted(self.base[section]):
                with self.subTest(section=section, missing=key):
                    observation = copy.deepcopy(self.base)
                    del observation[section][key]
                    self.refused(observation, f"{section}")

    def test_an_extra_key_is_refused(self):
        self.refused({**self.base, "tag_only": "x"}, "additionalProperties")
        self.refused({**self.base, "attributable_spend_pct": 90}, "additionalProperties")   # the 1.x top-level quality input
        for section in ("config", "performance", "financial_quality", "economics"):
            with self.subTest(section=section):
                observation = copy.deepcopy(self.base)
                observation[section]["tag_only"] = "x"
                self.refused(observation, section)

    def test_a_value_outside_its_vocabulary_is_refused(self):
        cases = {
            "intent_state": "MAYBE", "origin_attribution": "GUESSED", "evidence_quality": "GREAT", "effective_time_quality": "ROUGH",
            "source": "demo normalized", "applied_scenario_id": "SCN-1", "replacement_package_id": "PKG-1",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.refused({**self.base, key: value}, key)
        for key, value in {"counterfactual_method": "GUESS", "economic_basis": "VIBES", "aws_evidence_type": "CUR", "currency": "usd"}.items():
            with self.subTest(economics=key):
                observation = copy.deepcopy(self.base)
                observation["economics"][key] = value
                self.refused(observation, "economics")
        observation = copy.deepcopy(self.base)
        observation["correction"] = {"key": "c1", "type": "restatement", "version": 1, "reason": None, "corrects_entry_id": None}
        self.refused(observation, "correction")

    def test_wrong_types_are_refused(self):
        for patch in ({"elapsed_days": "8"}, {"query_count": "250"}, {"representative_regime": "yes"}, {"rollback_requested": None},
                      {"performance": None}, {"financial_quality": None}, {"config": None}, {"workload": []}):
            with self.subTest(patch=patch):
                self.refused({**self.base, **patch})

    def test_identity_must_be_the_packages(self):
        self.refused({**self.base, "workspace_id": "999"}, "observation is for")
        self.refused({**self.base, "warehouse_id": "wh-other"}, "observation is for")

    def test_times_must_be_ordered(self):
        self.refused({**self.base, "applied_at_utc": "2026-10-09T12:00:01Z"}, "applied_at_utc is after observed_at_utc")
        self.refused({**self.base, "window_start_utc": "2026-10-09T12:00:01Z", "window_end_utc": "2026-10-09T12:00:00Z"}, "window_start_utc is after window_end_utc")
        self.refused({**self.base, "window_end_utc": "2026-10-09T12:00:01Z"}, "window_end_utc is after observed_at_utc")
        # the boundaries themselves are fine: applied at observed, a one-instant window ending at observed
        loader.validate_observation({**self.base, "applied_at_utc": self.base["observed_at_utc"], "window_start_utc": self.base["observed_at_utc"]}, self.pkg)

    def test_times_are_iso_8601_with_a_utc_offset(self):
        for field in loader.TIME_FIELDS:
            for value in ("nope", "2026-10-09T12:00:00", "2026-13-45T00:00:00Z"):
                with self.subTest(field=field, value=value):
                    message = self.refused({**self.base, field: value}, f"{field} is not an ISO-8601 time with a UTC offset")
                    self.assertNotIn(value, message, "the message names the field, never the value")
        loader.validate_observation({**self.base, "observed_at_utc": "2026-10-09T14:00:00+02:00", "recorded_at_utc": "2026-10-09T14:00:00+02:00"}, self.pkg)

    def test_economics_need_a_window(self):
        self.refused({**self.base, "window_start_utc": None}, "need window_start_utc and window_end_utc")
        self.refused({**self.base, "window_end_utc": None}, "need window_start_utc and window_end_utc")
        loader.validate_observation({**self.base, "economics": None, "window_start_utc": None, "window_end_utc": None}, self.pkg)

    def test_the_applied_scenario_must_be_a_scenario_of_the_package(self):
        self.refused({**self.base, "applied_scenario_id": "SCN-" + "A" * 24}, "names no scenario of the package")
        known = self.pkg["scenarios"][0]["scenario_id"]
        loader.validate_observation({**self.base, "applied_scenario_id": known}, self.pkg)

    def test_the_observed_warehouse_type_must_be_supported(self):
        for unsupported in ("SERVERLESS_V2", "FUTURE"):
            with self.subTest(type=unsupported):
                observation = copy.deepcopy(self.base)
                observation["config"]["warehouse_type"] = unsupported
                self.refused(observation, f"unsupported warehouse type observed: {unsupported}")
        for supported in sorted(SUPPORTED_WAREHOUSE_TYPES) + ["pro", " Classic "]:
            with self.subTest(type=supported):
                observation = copy.deepcopy(self.base)
                observation["config"]["warehouse_type"] = supported
                loader.validate_observation(observation, self.pkg)

    def test_the_gate_is_the_only_thing_standing_between_a_malformed_observation_and_the_stages(self):
        malformed = {**self.base, "config": 5}
        self.refused(malformed)
        with tempfile.TemporaryDirectory() as tmp, observation_gate_open():
            with self.assertRaises(Exception):
                realize(copy.deepcopy(self.pkg), malformed, Path(tmp))
            self.assertNotEqual(tree(Path(tmp)), {}, "with the gate open the stages run and leave the lineage entry")


class PackageGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()

    def test_a_recommended_scenario_that_is_not_in_the_package_is_refused(self):
        package = copy.deepcopy(self.pkg)
        package["recommended_plan"]["scenario_id"] = "SCN-" + "A" * 24
        rehashed(package)
        with self.assertRaisesRegex(ValueError, "names no scenario of the package"):
            loader.validate_package(package)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            realize(package, None, Path(tmp))
        self.assertIsNone(loader.validate_package(copy.deepcopy(self.pkg)))

    def test_a_package_of_an_unsupported_warehouse_type_is_refused(self):
        for unsupported in ("SERVERLESS_V2", "FUTURE"):
            with self.subTest(type=unsupported):
                package = copy.deepcopy(self.pkg)
                package["current_config"]["warehouse_type"] = unsupported
                rehashed(package)
                with self.assertRaisesRegex(ValueError, f"unsupported warehouse type in the package: {unsupported}"):
                    loader.validate_package(package)
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp)
                    with self.assertRaises(ValueError):
                        realize(package, None, out)
                    self.assertEqual(tree(out), {})


if __name__ == "__main__":
    unittest.main()
