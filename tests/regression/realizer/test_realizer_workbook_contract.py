"""Realizer Workbook Extension projection of the frozen single-file oracle and of src/realizer/contracts/report.py (the .xlsx renderer needs artifact_tool)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical, oracle  # noqa: E402
canonical.modular()  # src/realizer, never a shadowing copy
from realizer import realizer as modular_realizer  # noqa: E402
from realizer.contracts import report as modular_report  # noqa: E402


class RealizerWorkbookContractTests(unittest.TestCase):
    realizer = oracle.engine()
    rwc = oracle.realizer_workbook_contract()

    @classmethod
    def setUpClass(cls):
        cls.pkg = canonical.load_scenario('example-exact')['package_doc']

    def test_projection_has_one_summary_row_and_ledger_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snap = self.realizer.realize(self.pkg, self.realizer.demo_observation(self.pkg), out)
            entries = self.realizer.load_ledger(self.realizer.ledger_path(out, snap.realization_package_id))
            proj = self.rwc.project_realizer_workbook([snap], {snap.realization_package_id: entries})
            self.assertEqual(len(proj.realization_summary), 1)
            self.assertEqual(proj.realization_summary[0]['realization_package_id'], snap.realization_package_id)
            self.assertGreater(len(proj.realization_timeline), 1)
            self.assertEqual(proj.realization_summary[0]['implementation_conformance'], 'EXACT')
            self.assertEqual(proj.realization_summary[0]['realization_state'], 'REALIZED')

    def test_projection_does_not_recompute_economics(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snap = self.realizer.realize(self.pkg, self.realizer.demo_observation(self.pkg), out)
            proj = self.rwc.project_realizer_workbook([snap], {})
            self.assertEqual(
                proj.realization_summary[0]['annualized_realized_savings'],
                round(float(snap.economics['annualized_realized_savings']), 2),
            )


class ModularRealizerWorkbookContractTests(RealizerWorkbookContractTests):
    """The same projection from realizer.contracts.report on the modular engine."""
    realizer = modular_realizer
    rwc = modular_report


@unittest.skipUnless(oracle.reporters_available(), "the workbook renderers need the artifact_tool runtime")
class RealizerWorkbookRendererTests(unittest.TestCase):
    def test_renderer_loads(self):
        reporter = oracle.reporters()
        self.assertTrue(callable(reporter.write_realizer_extension_workbook))


if __name__ == '__main__':
    unittest.main()
