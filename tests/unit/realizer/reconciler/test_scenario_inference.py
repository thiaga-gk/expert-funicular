"""Which scenario an observation applied (src/realizer/reconciler/matching.py ``match``).

Without an ``applied_scenario_id`` the scenario is inferred: the one whose authority-eligible steps the observed
configuration applies most (exactly or modified), the recommended plan first on a tie and when nothing is applied. A
caller-named scenario is an override. The application records the scenario's compatibility status, and a migration whose
compatibility was not evidenced is annotated ``COMPATIBILITY_NOT_EVIDENCED``.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401
from realizer.analyzer.analysis import observed_config
from realizer.analyzer.collector.collector import demo_observation
from realizer.reconciler.matching import match
from realizer.realizer import realize

FULL = world.package("inplace-size-canary")
MIGRATION_ONLY = world.package("scope-migration-only")


def migration_id(package):
    return next(s["scenario_id"] for s in package["scenarios"] if s["scenario_type"] == "WAREHOUSE_TYPE_MIGRATION")


def observe(package, **config):
    obs = demo_observation(package)
    obs["config"] = {**observed_config({"config": {}}, package), **config}
    return obs


def matched(package, obs):
    return match(package, obs, observed_config(obs, package))


class InferenceTests(unittest.TestCase):
    def test_a_type_change_is_the_migration_scenario(self):
        result = matched(FULL, observe(FULL, warehouse_type="SERVERLESS"))
        self.assertEqual(result.selected_scenario_id, migration_id(FULL))
        self.assertTrue(result.scenario_scope)
        self.assertEqual([(c["knob"], c["implementation_conformance"]) for c in result.authority_changes], [("warehouse_type", "EXACT")])

    def test_a_migration_only_scope_sees_the_migration(self):
        result = matched(MIGRATION_ONLY, observe(MIGRATION_ONLY, warehouse_type="SERVERLESS"))
        self.assertEqual(result.selected_scenario_id, migration_id(MIGRATION_ONLY))
        self.assertEqual(len(result.authority_changes), 1)

    def test_an_in_place_step_stays_with_the_recommended_plan(self):
        result = matched(FULL, observe(FULL, warehouse_size="MEDIUM"))
        self.assertEqual(result.selected_scenario_id, FULL["recommended_plan"]["scenario_id"])
        self.assertFalse(result.scenario_scope)

    def test_nothing_applied_is_the_recommended_plan(self):
        self.assertEqual(matched(FULL, observe(FULL)).selected_scenario_id, FULL["recommended_plan"]["scenario_id"])

    def test_a_named_scenario_overrides_the_inference(self):
        obs = observe(FULL, warehouse_type="SERVERLESS")
        obs["applied_scenario_id"] = FULL["recommended_plan"]["scenario_id"]
        self.assertEqual(matched(FULL, obs).selected_scenario_id, FULL["recommended_plan"]["scenario_id"])


class CompatibilityTests(unittest.TestCase):
    def test_the_migration_application_records_unresolved_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(FULL, observe(FULL, warehouse_type="SERVERLESS"), Path(tmp) / "out").to_dict()
        app = snapshot["application"]
        self.assertEqual((app["application_scope"], app["scenario_id"]), ("SCENARIO", migration_id(FULL)))
        self.assertEqual(app["compatibility_status"], "REQUIRES_VALIDATION")
        self.assertEqual(app["annotations"], ["COMPATIBILITY_NOT_EVIDENCED"])

    def test_an_in_place_application_is_compatible_without_annotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(FULL, observe(FULL, warehouse_size="MEDIUM"), Path(tmp) / "out").to_dict()
        self.assertEqual((snapshot["application"]["compatibility_status"], snapshot["application"]["annotations"]), ("COMPATIBLE", []))


class LiveMigrationTests(unittest.TestCase):
    """The migration-only scope's package (wh-demo-001, recommended NO_CHANGE_BASELINE, Plan B PRO -> SERVERLESS) and a
    simulated world where the owner migrates: the live measurement finds the migration step, the run infers Plan B."""

    def test_the_live_path_sees_the_migration(self):
        from realizer.analyzer.projector import project_observation
        observation = project_observation(MIGRATION_ONLY, world.measure(MIGRATION_ONLY, world.world_spec("C01", {"type": "SERVERLESS"}), 1))
        self.assertEqual((observation["measurement"]["knob"], observation["measurement"]["applied_value"]), ("warehouse_type", "SERVERLESS"))
        self.assertEqual(observation["origin_attribution"], "INFERRED")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(MIGRATION_ONLY, observation, Path(tmp) / "out").to_dict()
        app = snapshot["application"]
        self.assertEqual((app["application_scope"], app["scenario_id"], app["implementation_conformance"]),
                         ("SCENARIO", migration_id(MIGRATION_ONLY), "EXACT"))
        self.assertEqual(app["annotations"], ["COMPATIBILITY_NOT_EVIDENCED"])


if __name__ == "__main__":
    unittest.main()
