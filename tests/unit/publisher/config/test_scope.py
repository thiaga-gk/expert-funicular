"""Run scope resolution (src/publisher/config/loader.py resolve_scope) and the committed scope schema and presets."""
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS, SCOPE_SCENARIOS  # noqa: E402
from publisher.config.loader import resolve_scope  # noqa: E402
from publisher.contracts.scope import FULL_SCOPE, WarehouseSelector, build_scope_schema  # noqa: E402


def scope_file(**overrides):
    """A valid scope-file payload (current: auto-stop only, all warehouses) with top-level overrides."""
    payload = {
        "scope_format_version": "1.0.0",
        "warehouses": "all",
        "scenarios": {
            "current": {"enabled": True, "techniques": {"size": False, "max_clusters": False, "min_clusters": False, "auto_stop": True}},
            "migration": {"enabled": False, "techniques": {"warehouse_type": True}},
        },
    }
    payload.update(overrides)
    return payload


class ScopeResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def resolve_file(self, payload, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
            return resolve_scope(scope_file=str(path), **kwargs)

    def test_no_input_is_the_full_scope(self):
        scope = resolve_scope()
        self.assertEqual(scope, FULL_SCOPE)
        self.assertTrue(scope.is_full)
        self.assertEqual(scope.describe(), "full")
        self.assertEqual(set(scope.knobs), set(ACTIVE_KNOBS))

    def test_every_active_knob_belongs_to_exactly_one_scenario(self):
        knobs = [k for _, techniques in SCOPE_SCENARIOS.values() for k in techniques.values()]
        self.assertEqual(sorted(knobs), sorted(ACTIVE_KNOBS))

    def test_scenario_alone_enables_all_its_techniques(self):
        self.assertEqual(resolve_scope(["current"]).knobs, ("auto_stop_minutes", "max_clusters", "min_clusters", "warehouse_size"))
        self.assertEqual(resolve_scope(["migration"]).knobs, ("warehouse_type",))
        self.assertTrue(resolve_scope(["current", "migration"]).is_full)

    def test_techniques_accept_hyphens_or_underscores_and_any_case(self):
        a = resolve_scope(["current:auto-stop,min-clusters"])
        b = resolve_scope(["CURRENT:Auto_Stop, min_clusters"])
        self.assertEqual(a, b)
        self.assertEqual(a.knobs, ("auto_stop_minutes", "min_clusters"))
        self.assertTrue(a.scenario_enabled("current"))
        self.assertFalse(a.scenario_enabled("migration"))
        self.assertEqual(a.describe(), "current: min_clusters, auto_stop")
        self.assertEqual(a.scenario_knobs(), [("IN_PLACE_OPTIMIZATION", ["auto_stop_minutes", "min_clusters"])])

    def test_invalid_scope_values_are_refused(self):
        for specs, message in (
            (["planA"], "Unknown scope scenario"),
            (["current:photon"], "Unknown technique"),
            (["migration:auto-stop"], "Unknown technique"),
            (["current:"], "needs technique names"),
            (["current:auto-stop,"], "needs technique names"),
            (["current:size", "current:auto-stop"], "more than once"),
        ):
            with self.subTest(specs=specs), self.assertRaisesRegex(PublisherError, message):
                resolve_scope(specs)

    def test_warehouse_selectors(self):
        self.assertEqual(resolve_scope(warehouses=["all"]).warehouses, ())
        scope = resolve_scope(warehouses=["wh-2", "123/wh-1", "wh-2"])
        self.assertEqual(scope.warehouses, (WarehouseSelector("wh-1", "123"), WarehouseSelector("wh-2")))
        self.assertEqual([s.label() for s in scope.warehouses], ["123/wh-1", "wh-2"])
        self.assertTrue(scope.is_full)  # selecting warehouses does not restrict what a package contains

    def test_invalid_warehouse_selectors_are_refused(self):
        for values, message in ((["all", "wh-1"], "cannot be combined"), (["/wh-1"], "Invalid warehouse selector"),
                                (["1/2/wh"], "Invalid warehouse selector"), (["wh 1"], "Invalid warehouse selector"),
                                (["123/"], "Invalid warehouse selector")):
            with self.subTest(values=values), self.assertRaisesRegex(PublisherError, message):
                resolve_scope(warehouses=values)

    def test_scope_file_matches_the_equivalent_flags(self):
        self.assertEqual(self.resolve_file(scope_file()), resolve_scope(["current:auto-stop"]))
        listed = self.resolve_file(scope_file(warehouses=["wh-1", "123/wh-2"]))
        self.assertEqual(listed, resolve_scope(["current:auto-stop"], warehouses=["wh-1", "123/wh-2"]))

    def test_disabled_scenario_toggles_are_ignored(self):
        payload = scope_file()
        payload["scenarios"]["migration"]["techniques"]["warehouse_type"] = True
        self.assertNotIn("warehouse_type", self.resolve_file(payload).knobs)

    def test_invalid_scope_files_are_refused(self):
        nothing = scope_file()
        nothing["scenarios"]["current"]["enabled"] = False
        no_technique = scope_file()
        no_technique["scenarios"]["current"]["techniques"]["auto_stop"] = False
        missing_toggle = scope_file()
        del missing_toggle["scenarios"]["current"]["techniques"]["size"]
        unknown = scope_file()
        unknown["scenarios"]["current"]["techniques"]["photon"] = True
        for payload, message in (
            (nothing, "no scenario is enabled"),
            (no_technique, "enabled but no technique"),
            (missing_toggle, "invalid scope file"),
            (unknown, "invalid scope file"),
            (scope_file(scope_format_version="2.0.0"), "invalid scope file"),
            (scope_file(warehouses=["all"]), "not a list containing"),
            (scope_file(warehouses=[]), "invalid scope file"),
            ("{not json", "cannot read the scope file"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(PublisherError, message):
                self.resolve_file(payload)

    def test_scope_file_is_not_combined_with_flags(self):
        for kwargs in ({"scope_specs": ["current"]}, {"warehouses": ["wh-1"]}):
            with self.subTest(**kwargs), self.assertRaisesRegex(PublisherError, "cannot be combined"):
                self.resolve_file(scope_file(), **kwargs)

    def test_missing_scope_file_is_refused(self):
        with self.assertRaisesRegex(PublisherError, "cannot read the scope file"):
            resolve_scope(scope_file=str(ROOT / "no-such-scope.json"))


class ScopeArtifactTests(unittest.TestCase):
    def test_committed_scope_schema_is_generated_from_the_hierarchy(self):
        committed = json.loads((ROOT / "schema" / "run-scope.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, build_scope_schema())

    def test_presets_resolve(self):
        logging.disable(logging.CRITICAL)
        try:
            expected = {
                "full.json": FULL_SCOPE.knobs,
                "current-only.json": ("auto_stop_minutes", "max_clusters", "min_clusters", "warehouse_size"),
                "current-auto-stop.json": ("auto_stop_minutes",),
                "migration-only.json": ("warehouse_type",),
            }
            presets = sorted(p.name for p in (ROOT / "examples" / "scopes").glob("*.json"))
            self.assertEqual(presets, sorted(expected))
            for name, knobs in expected.items():
                with self.subTest(preset=name):
                    self.assertEqual(resolve_scope(scope_file=str(ROOT / "examples" / "scopes" / name)).knobs, knobs)
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
