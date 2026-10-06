"""Realizer configuration (src/realizer/config): version identifiers, knobs, size order, input loading and policy.

Every value is the single-file release's (stored in ``tests/fixtures/realizer/expectations/``), the package-shape gate
raises the single-file release's errors, and the CLI loads its inputs and prints its results the same way.
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


def _error(fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)
    return None


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
        cls.pkg = canonical.load_scenario("example-exact")["package_doc"]

    def test_package_shape_gate(self):
        cases = {"valid": copy.deepcopy(self.pkg)}
        for key in ("recommendation_package_id", "warehouse", "provenance"):
            cases[f"missing {key}"] = {k: v for k, v in self.pkg.items() if k != key}
        cases["missing all"] = {}
        cases["bad id"] = {**self.pkg, "recommendation_package_id": "RPKG-1"}
        cases["numeric id"] = {**self.pkg, "recommendation_package_id": 7}
        cases["contract 3.1.0"] = {**self.pkg, "contract_version": "3.1.0"}
        cases["contract None"] = {**self.pkg, "contract_version": None}
        for label, package in cases.items():
            with self.subTest(case=label):
                error = _error(loader.validate_package_shape, package)
                expect.check(self, label, error)
                self.assertEqual(error is None, label == "valid")

    def test_realize_uses_the_loader_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unsupported Publisher contract: 3.1.0"):
                realize({**self.pkg, "contract_version": "3.1.0"}, None, Path(tmp))
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
        {"max_normalized_p95_runtime_regression_pct": 4, "require_representative_regime": False, "unknown_key": 1},
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
        with mock.patch("logging.basicConfig") as basic, redirect_stdout(out):
            self.assertEqual(main(args), 0)
        return out.getvalue().replace(str(base), "<tmp>"), basic.call_args

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
                    stdout, logging_call = self._run(common + ["--output-dir", str(base / f"out{len(extra)}")], base)
                    self.assertTrue(stdout.startswith("{"))
                    expect.check(self, label, (stdout, logging_call), spot={"stdout_characters": len(stdout), "logging": logging_call})
            expect.check(self, "demo prints the package path", self._run(["--package", str(pkg), "--demo", "--output-dir", str(base / "p")], base)[0])


if __name__ == "__main__":
    unittest.main()
