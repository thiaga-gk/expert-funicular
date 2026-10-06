"""Realizer Contract JSON Schema builders (src/realizer/contracts/realization_schema.py).

The builders reproduce the committed schema files byte for byte (they live with the frozen oracle in
legacy/realizer_single_file/schema/), equal the oracle's builders, and accept every stored RPKG/RSNAP and RLE.
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
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.contracts import realization_schema as schema  # noqa: E402

COMMITTED = ROOT / "legacy" / "realizer_single_file" / "schema"
BUILDERS = {
    "realization-package.schema.json": "realization_package_schema",
    "realization-ledger-entry.schema.json": "ledger_entry_schema",
}


def serialize(value) -> bytes:
    """How the committed schema files are written."""
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


class SchemaBuilderTests(unittest.TestCase):
    def test_builders_reproduce_committed_schema_files_byte_for_byte(self):
        self.assertEqual(sorted(p.name for p in COMMITTED.glob("*.json")), sorted(BUILDERS))
        for file_name, builder in BUILDERS.items():
            with self.subTest(schema=file_name):
                self.assertEqual(serialize(getattr(schema, builder)()), (COMMITTED / file_name).read_bytes())

    def test_builders_equal_the_oracle_builders(self):
        legacy = oracle.realizer_schema()
        for builder in BUILDERS.values():
            self.assertEqual(getattr(schema, builder)(), getattr(legacy, builder)(), builder)

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


if __name__ == "__main__":
    unittest.main()
