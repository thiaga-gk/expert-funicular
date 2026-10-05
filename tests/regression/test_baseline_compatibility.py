"""Behavioral baseline: the demo fleet reproduces its stored outputs exactly.

The stored demo expectations under tests/fixtures/expected/demo/ were first produced by the single-file publisher
2.10.0 and reproduced unchanged through the src/publisher reorganization. The demo fleet (one simulated warehouse per golden scenario) runs through the CLI entry and
must reproduce every package and the manifest byte for byte, and evidence.json and the workbook cell dump by digest.
Golden cases carry their own expected/ outputs (tests/golden/test_golden_publisher.py). To diagnose a diff, run
``python scripts/compare_with_ref.py --ref <ref>``.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from tests.regression import canonical  # noqa: E402


class DemoFleetParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.produced = canonical.demo_expected_files(p)
        cls.stored = canonical.stored_demo_expected()

    def test_same_output_files(self):
        self.assertEqual(sorted(self.produced), sorted(self.stored))

    def test_packages_and_manifest_are_byte_identical(self):
        differing = sorted(rel for rel, text in self.produced.items() if rel != "digests.json" and self.stored.get(rel) != text)
        self.assertEqual(differing, [], "outputs differ from the stored baseline")

    def test_evidence_and_workbook_digests(self):
        self.assertEqual(self.produced["digests.json"], self.stored["digests.json"])


if __name__ == "__main__":
    unittest.main()
