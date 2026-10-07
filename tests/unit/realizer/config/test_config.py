"""Realizer configuration (src/realizer/config): version identifiers, knobs, size order, input loading and policy.

Every value is the single-file release's (stored in ``tests/fixtures/realizer/expectations/``), ``validate_package`` accepts
only unmodified Reporter JSON Contract 3.1.0 packages, and the CLI loads its inputs and prints its results the same way.
``RealizerPolicy`` itself lives in ``realizer.governor.policy``; its defaults and ``from_mapping`` are also pinned here
because the policy file is loaded by the config layer.
"""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests import realizer_expect as expect  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402
from tests.unit.realizer.helpers import fixture_package  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from publisher.config import defaults as publisher_defaults  # noqa: E402
from realizer.cli.main import main  # noqa: E402
from realizer.config import loader, settings  # noqa: E402
from realizer.governor.policy import RealizerPolicy, policy_to_dict  # noqa: E402
from realizer.realizer import realize  # noqa: E402
VERSIONS = (
    "PRODUCT_VERSION", "REALIZER_VERSION", "PROJECTOR_VERSION", "POLICY_VERSION", "ANALYZER_GATEWAY_VERSION",
    "MODELER_GATEWAY_VERSION", "ESTIMATOR_GATEWAY_VERSION", "ATTRIBUTION_VERSION", "EXCLUSIVITY_VERSION",
)


class SettingsTests(unittest.TestCase):
    def test_version_identifiers(self):
        expect.check(self, "versions", {name: getattr(settings, name) for name in VERSIONS})

    def test_config_knobs(self):
        expect.check(self, "knobs", settings.CONFIG_KNOBS)

    def test_size_order_is_the_publisher_registry(self):
        expect.check(self, "size order", settings.SIZE_ORDER)
        self.assertEqual(settings.SIZE_ORDER, {n: i for i, n in enumerate(publisher_defaults.SIZE_ORDER)})
        self.assertEqual(list(settings.SIZE_ORDER)[0], "2X_SMALL")


class LoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()

    def rejected(self, mutate, message):
        """Assert that the fixture package, mutated by *mutate*, is refused with a ``ValueError`` matching *message*."""
        package = copy.deepcopy(self.pkg)
        mutate(package)
        with self.assertRaisesRegex(ValueError, message):
            loader.validate_package(package)
        return package

    def test_every_fixture_package_is_a_valid_3_1_0_package(self):
        for path in sorted(canonical.PACKAGES.glob("*.json")):
            with self.subTest(package=path.name):
                package = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(package["contract_version"], loader.SUPPORTED_PUBLISHER_CONTRACT)
                self.assertIsNone(loader.validate_package(package))

    def test_supported_contract_is_the_publishers(self):
        self.assertEqual(loader.SUPPORTED_PUBLISHER_CONTRACT, "3.1.0")
        self.assertEqual(self.pkg["contract_version"], loader.SUPPORTED_PUBLISHER_CONTRACT)

    def test_package_must_be_a_mapping(self):
        for value in (None, [], [self.pkg], "PKG-1", 7):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                    loader.validate_package(value)

    def test_wrong_or_missing_contract_version(self):
        self.rejected(lambda p: p.update(contract_version="1.0.0"), r"unsupported Publisher contract: 1\.0\.0 \(this Realizer reads 3\.1\.0\)")
        self.rejected(lambda p: p.update(contract_version="3.0.0"), r"unsupported Publisher contract: 3\.0\.0")
        self.rejected(lambda p: p.update(contract_version=None), r"unsupported Publisher contract: None")
        self.rejected(lambda p: p.pop("contract_version"), r"unsupported Publisher contract: None")

    def test_schema_violations_name_the_location_and_not_the_values(self):
        for key in ("recommendation_package_id", "warehouse", "provenance", "scenarios", "recommended_plan", "realization"):
            with self.subTest(removed=key):
                self.rejected(lambda p: p.pop(key), r"fails the Reporter JSON Contract 3\.1\.0 schema")
        package = self.rejected(lambda p: p.update(recommendation_package_id=7), r"schema at recommendation_package_id")
        self.assertEqual(package["recommendation_package_id"], 7)
        self.rejected(lambda p: p["provenance"].update(package_hash_full_sha256="not a hash"), r"schema at provenance/package_hash_full_sha256")
        self.rejected(lambda p: p.update(unexpected_field=1), r"fails the Reporter JSON Contract 3\.1\.0 schema")

    def test_an_edited_hashed_field_breaks_the_package_id(self):
        self.rejected(lambda p: p["current_config"].update(warehouse_size="SMALL"), f"package id {self.pkg['recommendation_package_id']} does not match the package content")
        self.rejected(lambda p: p["current_config"].update(max_clusters=p["current_config"]["max_clusters"] + 1), "does not match the package content")

    def test_an_edited_full_hash_is_refused(self):
        package = self.rejected(lambda p: p["provenance"].update(package_hash_full_sha256="0" * 64), "package_hash_full_sha256 does not match the package content")
        self.assertEqual(package["recommendation_package_id"], self.pkg["recommendation_package_id"])

    def test_the_realization_block_must_be_the_empty_placeholder(self):
        self.rejected(lambda p: p["realization"].update(status="REALIZED"), "empty NOT_APPLIED placeholder")
        self.rejected(lambda p: p["realization"].update(realization_records=[{"x": 1}]), "fails the Reporter JSON Contract 3\\.1\\.0 schema|empty NOT_APPLIED placeholder")

    def test_a_carried_forward_plan_is_refused(self):
        self.rejected(lambda p: p.update(carried_forward_plan={"plan": 1}), "carried_forward_plan|carried-forward")

    def test_an_execution_state_edit_outside_the_hash_still_validates(self):
        """The package hash covers only selected fields; a step's execution_state is projected, so an edit that stays
        within the schema does not change the id and is accepted."""
        package = copy.deepcopy(self.pkg)
        step = next(s for s in package["scenarios"][0]["execution_plan"] if s["execution_state"] == "READY")
        step["execution_state"] = "PENDING_REEVALUATION"
        self.assertIsNone(loader.validate_package(package))

    def test_realize_uses_the_loader_gate(self):
        cases = {"contract": ({**self.pkg, "contract_version": "1.0.0"}, "unsupported Publisher contract: 1.0.0"),
                 "id": ({**self.pkg, "current_config": {**self.pkg["current_config"], "warehouse_size": "SMALL"}}, "does not match the package content"),
                 "schema": ({k: v for k, v in self.pkg.items() if k != "scenarios"}, "fails the Reporter JSON Contract 3.1.0 schema")}
        for label, (package, message) in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, message):
                    realize(package, None, Path(tmp))
                self.assertEqual(list(Path(tmp).iterdir()), [], "the gate runs before anything is written")

    def test_load_package_and_policy_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_path = Path(tmp) / "pkg.json"
            pkg_path.write_text(json.dumps(self.pkg), encoding="utf-8")
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(json.dumps({"minimum_days": 3, "realized_ratio_min": 0.9}), encoding="utf-8")
            self.assertEqual(loader.load_package(pkg_path), self.pkg)
            self.assertEqual(loader.load_package(str(pkg_path)), self.pkg)
            self.assertEqual(loader.load_policy_mapping(str(policy_path)), {"minimum_days": 3, "realized_ratio_min": 0.9})
        self.assertIsNone(loader.load_policy_mapping(None))
        self.assertIsNone(loader.load_policy_mapping(""))


class PolicyTests(unittest.TestCase):
    MAPPINGS = [
        None, {}, {"minimum_days": 3}, {"realized_ratio_min": 0.9, "under_realized_ratio_min": "0.4"},
        {"max_normalized_p95_runtime_regression_pct": 4, "require_representative_regime": False},
        {"minimum_attributable_spend_pct": "90.00", "minimum_price_coverage_pct": 99.5,
         "required_currency_consistency_pct": "100", "minimum_material_savings_delta_pct": 2,
         "remeasure_days_after_invalidating_change": 14, "require_confirmed_origin_for_hard_kpi": False,
         "minimum_queries": 10, "policy_version": "custom"},
    ]

    def test_defaults(self):
        policy = RealizerPolicy()
        expect.check(self, "policy", {"policy": policy_to_dict(policy), "snapshot_id": policy.snapshot_id})
        self.assertEqual(policy.policy_version, settings.POLICY_VERSION)

    def test_from_mapping(self):
        for index, mapping in enumerate(self.MAPPINGS):
            with self.subTest(mapping=mapping):
                policy = RealizerPolicy.from_mapping(mapping)
                expect.check(self, f"mapping {index}", {"policy": policy_to_dict(policy), "snapshot_id": policy.snapshot_id})
                for value in policy_to_dict(policy).values():
                    self.assertNotIsInstance(value, Decimal)

    def test_policy_file_round_trip_through_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(self.MAPPINGS[-1]), encoding="utf-8")
            policy = RealizerPolicy.from_mapping(loader.load_policy_mapping(str(path)))
            self.assertEqual(policy.snapshot_id, RealizerPolicy.from_mapping(self.MAPPINGS[-1]).snapshot_id)
            expect.check(self, "snapshot id", policy.snapshot_id)


class CliLoadingTests(unittest.TestCase):
    """``main`` loads package, observation and policy as the single-file CLI did and prints the same result."""

    def _run(self, args, base):
        out = io.StringIO()
        with mock.patch("realizer.cli.realize.setup_logging") as setup, redirect_stdout(out):
            self.assertEqual(main(args), 0)
        return out.getvalue().replace(str(base), "<tmp>"), setup.call_args

    def test_cli_output_and_logging_setup(self):
        scenario = canonical.load_scenario("example-exact")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            obs = base / "obs.json"
            obs.write_text(json.dumps(scenario["runs"][0]["observation"]), encoding="utf-8")
            policy = base / "policy.json"
            policy.write_text(json.dumps({"minimum_days": 5}), encoding="utf-8")
            pkg = canonical.PACKAGES / scenario["package"]
            for label, extra in (("observation and policy", ["--observation", str(obs), "--policy", str(policy)]),
                                 ("demo modified", ["--demo-modified"]), ("lineage only", [])):
                with self.subTest(args=label):
                    common = ["--package", str(pkg), "--print-json", "--log-level", "DEBUG", *extra]
                    stdout, logging_call = self._run(common + ["--output-dir", str(base / f"out{len(extra)}"), "--flat-output"], base)
                    self.assertTrue(stdout.startswith("{"))
                    expect.check(self, label, (stdout, logging_call), spot={"stdout_characters": len(stdout), "logging": logging_call})
            expect.check(self, "demo prints the package path", self._run(["--package", str(pkg), "--demo", "--output-dir", str(base / "p"), "--flat-output"], base)[0])


if __name__ == "__main__":
    unittest.main()
