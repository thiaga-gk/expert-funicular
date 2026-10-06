"""Realizer configuration (src/realizer/config): version identifiers, knobs, size order, input loading and policy.

Every value equals the frozen single-file oracle's, the package-shape gate raises the oracle's errors, and the CLI
loads its inputs the same way. ``RealizerPolicy`` itself stays in the engine until the governor phase; its defaults
and ``from_mapping`` are pinned here against the oracle because the policy file is loaded by the config layer.
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
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from publisher.config import defaults as publisher_defaults  # noqa: E402
from realizer import realizer as engine  # noqa: E402
from realizer.config import loader, settings  # noqa: E402

oengine = oracle.engine()
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
    def test_version_identifiers_equal_the_oracle(self):
        for name in VERSIONS:
            with self.subTest(name=name):
                self.assertEqual(getattr(settings, name), getattr(oengine, name))
                self.assertIs(getattr(engine, name), getattr(settings, name))

    def test_config_knobs_equal_the_oracle(self):
        self.assertEqual(settings.CONFIG_KNOBS, oengine.CONFIG_KNOBS)
        self.assertIs(engine.CONFIG_KNOBS, settings.CONFIG_KNOBS)

    def test_size_order_is_the_publisher_registry(self):
        self.assertEqual(settings.SIZE_ORDER, oengine.SIZE_ORDER)
        self.assertEqual(settings.SIZE_ORDER, {n: i for i, n in enumerate(publisher_defaults.SIZE_ORDER)})
        self.assertEqual(list(settings.SIZE_ORDER)[0], "2X_SMALL")
        self.assertIs(engine.SIZE_ORDER, settings.SIZE_ORDER)


class LoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = canonical.load_scenario("example-exact")["package_doc"]

    def test_package_shape_gate_matches_oracle(self):
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
                self.assertEqual(_error(loader.validate_package_shape, package),
                                 _error(oengine.validate_package_shape, package))
                self.assertEqual(_error(loader.validate_package_shape, package) is None, label == "valid")

    def test_engine_uses_the_loader_gate(self):
        self.assertIs(engine.validate_package_shape, loader.validate_package_shape)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unsupported Publisher contract: 3.1.0"):
                engine.realize({**self.pkg, "contract_version": "3.1.0"}, None, Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [], "the gate runs before anything is written")

    def test_load_package_and_policy_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_path = Path(tmp) / "pkg.json"
            pkg_path.write_text(json.dumps(self.pkg), encoding="utf-8")
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(json.dumps({"minimum_days": 3, "realized_ratio_min": 0.9}), encoding="utf-8")
            self.assertEqual(loader.load_package(pkg_path), self.pkg)
            self.assertEqual(loader.load_package(str(pkg_path)), oengine.load_json(str(pkg_path)))
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

    def test_defaults_equal_the_oracle(self):
        new, old = engine.RealizerPolicy(), oengine.RealizerPolicy()
        self.assertEqual(engine.policy_to_dict(new), oengine.policy_to_dict(old))
        self.assertEqual(new.snapshot_id, old.snapshot_id)
        self.assertEqual(new.policy_version, settings.POLICY_VERSION)

    def test_from_mapping_equals_the_oracle(self):
        for mapping in self.MAPPINGS:
            with self.subTest(mapping=mapping):
                new = engine.RealizerPolicy.from_mapping(mapping)
                old = oengine.RealizerPolicy.from_mapping(mapping)
                self.assertEqual(engine.policy_to_dict(new), oengine.policy_to_dict(old))
                self.assertEqual(new.snapshot_id, old.snapshot_id)
                for value in engine.policy_to_dict(new).values():
                    self.assertNotIsInstance(value, Decimal)

    def test_policy_file_round_trip_through_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(self.MAPPINGS[-1]), encoding="utf-8")
            new = engine.RealizerPolicy.from_mapping(loader.load_policy_mapping(str(path)))
            old = oengine.RealizerPolicy.from_mapping(oengine.load_json(str(path)))
            self.assertEqual(new.snapshot_id, old.snapshot_id)


class CliLoadingTests(unittest.TestCase):
    """``main`` loads package, observation and policy as the oracle does and prints the same result."""

    def _run(self, module, args):
        out = io.StringIO()
        with mock.patch("logging.basicConfig") as basic, redirect_stdout(out):
            self.assertEqual(module.main(args), 0)
        return out.getvalue(), basic.call_args

    def test_cli_output_and_logging_setup_match_oracle(self):
        scenario = canonical.load_scenario("example-exact")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            obs = base / "obs.json"
            obs.write_text(json.dumps(scenario["runs"][0]["observation"]), encoding="utf-8")
            policy = base / "policy.json"
            policy.write_text(json.dumps({"minimum_days": 5}), encoding="utf-8")
            pkg = canonical.PACKAGES / scenario["package"]
            for extra in (["--observation", str(obs), "--policy", str(policy)], ["--demo-modified"], []):
                with self.subTest(args=extra):
                    common = ["--package", str(pkg), "--print-json", "--log-level", "DEBUG", *extra]
                    new = self._run(engine, common + ["--output-dir", str(base / f"new{len(extra)}")])
                    old = self._run(oengine, common + ["--output-dir", str(base / f"old{len(extra)}")])
                    self.assertEqual(new, old)
            path_new = self._run(engine, ["--package", str(pkg), "--demo", "--output-dir", str(base / "p")])[0]
            path_old = self._run(oengine, ["--package", str(pkg), "--demo", "--output-dir", str(base / "p")])[0]
            self.assertEqual(path_new, path_old)


if __name__ == "__main__":
    unittest.main()
