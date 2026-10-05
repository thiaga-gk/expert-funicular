"""The public output contract stays byte-identical through a structural change (repo-reorg-design.md section 21.1).

Pins the committed JSON Schemas and the canonical contract example by digest, checks the schema the contract code
builds still equals the committed one, and validates every stored baseline package against the committed schema.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

from publisher.contracts.reporter_schema import build_schema  # noqa: E402
from tests.golden import case_dir, case_names  # noqa: E402
from tests.regression import canonical  # noqa: E402


class OutputContractCompatibilityTests(unittest.TestCase):
    def test_committed_contract_artifacts_are_unchanged(self):
        stored = (canonical.CONTRACT_EXPECTED / "digests.json").read_text(encoding="utf-8")
        self.assertEqual(canonical.contract_expected_files()["digests.json"], stored)

    def test_contract_code_builds_the_committed_schema(self):
        committed = json.loads((ROOT / "schema" / "recommendation-package.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(build_schema(), committed)

    def test_every_baseline_package_validates_against_the_committed_schema(self):
        schema = json.loads((ROOT / "schema" / "recommendation-package.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        packages = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((canonical.DEMO_EXPECTED / "packages").glob("*.json"))]
        for name in case_names():
            expected = case_dir(name) / "expected" / "recommendation.json"
            if expected.is_file():
                packages += json.loads(expected.read_text(encoding="utf-8"))
        self.assertGreater(len(packages), 41)
        for package in packages:
            errors = [e.message for e in validator.iter_errors(package)]
            self.assertEqual(errors, [], package.get("recommendation_package_id"))


if __name__ == "__main__":
    unittest.main()
