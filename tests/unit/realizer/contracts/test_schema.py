"""Realizer Contract JSON Schema builders (src/realizer/contracts/realization_schema.py).

The builders reproduce the committed schema files (``schema/realization-package.schema.json`` and
``schema/realization-ledger-entry.schema.json``) byte for byte, and accept every stored RPKG/RSNAP and RLE. The observation
schema (``schema/realization-observation.schema.json``, built by ``realizer.contracts.observation.observation_schema``) is
reproduced byte for byte too, and every stored scenario observation and shipped example validates against it. So is the
measurement evidence schema (``schema/realization-measurement-evidence.schema.json``, built by
``realizer.contracts.measurement.measurement_evidence_schema``), which a simulated live measurement satisfies.
"""
import json
import sys
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[4]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests.regression.realizer import canonical  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.contracts import measurement as measurement_module  # noqa: E402
from realizer.contracts import observation as observation_module  # noqa: E402
from realizer.contracts import realization_schema as schema  # noqa: E402

COMMITTED = ROOT / "schema"
BUILDERS = {
    "realization-package.schema.json": "realization_package_schema",
    "realization-ledger-entry.schema.json": "ledger_entry_schema",
}


def serialize(value) -> bytes:
    """How the committed schema files are written."""
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


class SchemaBuilderTests(unittest.TestCase):
    def test_builders_reproduce_committed_schema_files_byte_for_byte(self):
        self.assertTrue(all((COMMITTED / name).is_file() for name in BUILDERS))
        for file_name, builder in BUILDERS.items():
            with self.subTest(schema=file_name):
                self.assertEqual(serialize(getattr(schema, builder)()), (COMMITTED / file_name).read_bytes())

    def test_schemas_are_valid_draft_2020_12(self):
        for builder in BUILDERS.values():
            jsonschema.Draft202012Validator.check_schema(getattr(schema, builder)())

    def test_every_stored_output_validates(self):
        package_validator = jsonschema.Draft202012Validator(schema.realization_package_schema())
        entry_validator = jsonschema.Draft202012Validator(schema.ledger_entry_schema())
        packages = entries = 0
        for name in canonical.scenario_names():
            base = canonical.EXPECTED / name / "realization"
            for path in sorted(base.glob("packages/*.json")) + sorted(base.glob("snapshots/*.json")):
                package_validator.validate(json.loads(path.read_text(encoding="utf-8")))
                packages += 1
            for path in sorted(base.glob("ledger/*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    entry_validator.validate(json.loads(line))
                    entries += 1
        self.assertGreater(packages, 41)
        self.assertGreater(entries, 300)


class ObservationSchemaTests(unittest.TestCase):
    FILE = COMMITTED / "realization-observation.schema.json"

    def test_builder_reproduces_the_committed_schema_file_byte_for_byte(self):
        self.assertTrue(self.FILE.is_file())
        self.assertEqual(serialize(observation_module.observation_schema()), self.FILE.read_bytes())

    def test_schema_is_valid_draft_2020_12_and_strict(self):
        built = observation_module.observation_schema()
        jsonschema.Draft202012Validator.check_schema(built)
        self.assertEqual(built["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIs(built["additionalProperties"], False)
        self.assertEqual(built["required"], sorted(built["properties"]), "every key is required")
        for section in ("config", "performance", "financial_quality"):
            self.assertIs(built["properties"][section]["additionalProperties"], False, section)
            self.assertEqual(built["properties"][section]["required"], sorted(built["properties"][section]["properties"]), section)
        self.assertEqual(sorted(built["properties"]["config"]["properties"]),
                         ["auto_stop_minutes", "enable_photon", "max_clusters", "min_clusters", "warehouse_channel", "warehouse_size", "warehouse_type"])
        self.assertEqual(built["properties"]["observation_format_version"], {"const": "2.2.0"})
        for section in ("measurement",):
            self.assertIs(built["properties"][section]["additionalProperties"], False, section)
            self.assertEqual(built["properties"][section]["required"], sorted(built["properties"][section]["properties"]), section)
        billed_change = built["properties"]["measurement"]["properties"]["billed_change"]
        self.assertEqual(billed_change["type"], ["object", "null"])
        self.assertEqual(billed_change["required"], ["annualized_dbu", "annualized_total", "aws_modeled", "basis", "currency", "days", "dbu", "total"])

    def test_the_billed_change_block_is_strict(self):
        validator = jsonschema.Draft202012Validator(observation_module.observation_schema()["properties"]["measurement"]["properties"]["billed_change"])
        good = {"basis": "BILLED_PRE_POST_AT_PRE_MIX", "days": 28, "currency": "USD", "dbu": "631.95", "aws_modeled": "-12.40",
                "total": "619.55", "annualized_dbu": "8237.92", "annualized_total": "8076.28"}
        self.assertTrue(validator.is_valid(good))
        self.assertTrue(validator.is_valid(None))
        self.assertTrue(validator.is_valid({**good, "aws_modeled": None, "total": None, "annualized_total": None}))
        self.assertFalse(validator.is_valid({**good, "dbu": None}))  # the DBU change is known whenever the block is
        self.assertFalse(validator.is_valid({**good, "basis": "REPLAY"}))
        self.assertFalse(validator.is_valid({**good, "days": 0}))
        self.assertFalse(validator.is_valid({k: v for k, v in good.items() if k != "annualized_total"}))
        self.assertFalse(validator.is_valid({**good, "claimed": True}))

    def test_every_stored_scenario_observation_and_the_examples_validate(self):
        validator = jsonschema.Draft202012Validator(observation_module.observation_schema())
        checked = 0
        for name in canonical.scenario_names():
            for run in canonical.load_scenario(name)["runs"]:
                if run.get("observation"):
                    with self.subTest(scenario=name):
                        validator.validate(run["observation"])
                        checked += 1
        for variant in ("exact", "modified"):
            with self.subTest(example=variant):
                validator.validate(json.loads((ROOT / "examples" / "realizer" / variant / "observation.json").read_text(encoding="utf-8")))
                checked += 1
        self.assertGreater(checked, 41)

    def test_the_schema_refuses_an_incomplete_or_extended_observation(self):
        validator = jsonschema.Draft202012Validator(observation_module.observation_schema())
        observation = json.loads((ROOT / "examples" / "realizer" / "exact" / "observation.json").read_text(encoding="utf-8"))
        self.assertTrue(validator.is_valid(observation))
        self.assertFalse(validator.is_valid({k: v for k, v in observation.items() if k != "financial_quality"}))
        self.assertFalse(validator.is_valid({**observation, "tag_only": "x"}))
        self.assertFalse(validator.is_valid({**observation, "config": {**observation["config"], "tag_only": "x"}}))
        self.assertFalse(validator.is_valid({**observation, "observation_format_version": "1.0.0"}))



class MeasurementEvidenceSchemaTests(unittest.TestCase):
    FILE = COMMITTED / "realization-measurement-evidence.schema.json"

    def test_builder_reproduces_the_committed_schema_file_byte_for_byte(self):
        self.assertTrue(self.FILE.is_file())
        self.assertEqual(serialize(measurement_module.measurement_evidence_schema()), self.FILE.read_bytes())

    def test_schema_is_strict_and_a_simulated_measurement_satisfies_it(self):
        from tests import realizer_world as world
        built = measurement_module.measurement_evidence_schema()
        jsonschema.Draft202012Validator.check_schema(built)
        self.assertIs(built["additionalProperties"], False)
        self.assertEqual(built["required"], sorted(built["properties"]))
        evidence = world.measure(world.package("autostop-scoped-pro"), world.world_spec("C13", {"auto_stop": 10}), 13)
        validator = jsonschema.Draft202012Validator(built)
        self.assertTrue(validator.is_valid(evidence))
        self.assertFalse(validator.is_valid({**evidence, "owner": "someone@example.com"}))
        self.assertFalse(validator.is_valid({**evidence, "measurement_evidence_format_version": "2.0.0"}))


if __name__ == "__main__":
    unittest.main()
