"""Realizer Workbook Extension projection (src/realizer/contracts/report.py). The projection is data only: summary and timeline rows."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402
canonical.modular()  # src/realizer, never a shadowing copy
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.report import project_realizer_workbook  # noqa: E402
from realizer.packager.ledger import ledger_path, load_ledger  # noqa: E402
from realizer.realizer import realize  # noqa: E402


class RealizerWorkbookProjectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pkg = canonical.load_scenario('example-exact')['package_doc']

    def test_projection_has_one_summary_row_and_ledger_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snap = realize(self.pkg, demo_observation(self.pkg), out)
            entries = load_ledger(ledger_path(out, snap.realization_package_id))
            proj = project_realizer_workbook([snap], {snap.realization_package_id: entries})
            self.assertEqual(len(proj.realization_summary), 1)
            self.assertEqual(proj.realization_summary[0]['realization_package_id'], snap.realization_package_id)
            self.assertGreater(len(proj.realization_timeline), 1)
            self.assertEqual(proj.realization_summary[0]['implementation_conformance'], 'EXACT')
            self.assertEqual(proj.realization_summary[0]['realization_state'], 'REALIZED')

    def test_projection_does_not_recompute_economics(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snap = realize(self.pkg, demo_observation(self.pkg), out)
            proj = project_realizer_workbook([snap], {})
            self.assertEqual(
                proj.realization_summary[0]['annualized_realized_savings'],
                round(float(snap.economics['annualized_realized_savings']), 2),
            )


if __name__ == '__main__':
    unittest.main()
