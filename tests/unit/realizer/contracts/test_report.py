"""Realizer report contract (src/realizer/contracts/report.py): the Workbook Extension projection, moved verbatim.

For every scenario of the compatibility baseline, projecting the stored final RPKG and its ledger reproduces the
stored ``report.json`` and equals the oracle's projection.
"""
import dataclasses
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.contracts import realization as rc, report  # noqa: E402

orc = oracle.rc()
owc = oracle.realizer_workbook_contract()


def _stored(name, contract):
    """The stored final RPKG and its ledger entries as *contract*'s dataclasses."""
    base = canonical.EXPECTED / name / "realization"
    (package_path,) = base.glob("packages/*.json")
    package = contract.RealizationPackage(**json.loads(package_path.read_text(encoding="utf-8")))
    entries = []
    for path in base.glob("ledger/*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            entries.append(contract.RealizationLedgerEntry(**{**raw, "event_type": contract.EventType(raw["event_type"])}))
    return package, entries


class ReportContractTests(unittest.TestCase):
    def test_constants_match_oracle(self):
        for name in ("REALIZER_WORKBOOK_EXTENSION_VERSION", "SHEET_REALIZATION_SUMMARY", "SHEET_REALIZATION_TIMELINE", "SHEET_NAMES"):
            self.assertEqual(getattr(report, name), getattr(owc, name), name)

    def test_projection_reproduces_stored_report_for_every_scenario(self):
        for name in canonical.scenario_names():
            with self.subTest(scenario=name):
                package, entries = _stored(name, rc)
                projection = report.project_realizer_workbook([package], {package.realization_package_id: entries})
                stored = json.loads((canonical.EXPECTED / name / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(json.loads(json.dumps(dataclasses.asdict(projection))), stored)
                legacy_package, legacy_entries = _stored(name, orc)
                legacy = owc.project_realizer_workbook([legacy_package], {legacy_package.realization_package_id: legacy_entries})
                self.assertEqual(dataclasses.asdict(projection), dataclasses.asdict(legacy))

    def test_projection_validation_matches_oracle(self):
        package, _ = _stored("example-exact", rc)
        legacy_package, _ = _stored("example-exact", orc)
        projection = report.RealizerWorkbookProjection(realization_summary=[], realization_timeline=[])
        legacy = owc.RealizerWorkbookProjection(realization_summary=[], realization_timeline=[])
        with self.assertRaises(ValueError) as new:
            projection.validate([package])
        with self.assertRaises(ValueError) as old:
            legacy.validate([legacy_package])
        self.assertEqual(str(new.exception), str(old.exception))
        self.assertEqual(str(new.exception), "one Realization Summary row required per RPKG")


if __name__ == "__main__":
    unittest.main()
