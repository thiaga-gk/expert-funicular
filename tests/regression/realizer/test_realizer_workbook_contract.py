"""Realizer Workbook Extension projection of the frozen single-file oracle (the .xlsx renderer needs artifact_tool)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical, oracle  # noqa: E402

realizer = oracle.engine()
rwc = oracle.realizer_workbook_contract()


class RealizerWorkbookContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = canonical.load_scenario('example-exact')['package_doc']

    def test_projection_has_one_summary_row_and_ledger_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snap = realizer.realize(self.pkg, realizer.demo_observation(self.pkg), out)
            entries = realizer.load_ledger(realizer.ledger_path(out, snap.realization_package_id))
            proj = rwc.project_realizer_workbook([snap], {snap.realization_package_id: entries})
            self.assertEqual(len(proj.realization_summary), 1)
            self.assertEqual(proj.realization_summary[0]['realization_package_id'], snap.realization_package_id)
            self.assertGreater(len(proj.realization_timeline), 1)
            self.assertEqual(proj.realization_summary[0]['implementation_conformance'], 'EXACT')
            self.assertEqual(proj.realization_summary[0]['realization_state'], 'REALIZED')

    def test_projection_does_not_recompute_economics(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snap = realizer.realize(self.pkg, realizer.demo_observation(self.pkg), out)
            proj = rwc.project_realizer_workbook([snap], {})
            self.assertEqual(
                proj.realization_summary[0]['annualized_realized_savings'],
                round(float(snap.economics['annualized_realized_savings']), 2),
            )



@unittest.skipUnless(oracle.reporters_available(), "the workbook renderers need the artifact_tool runtime")
class RealizerWorkbookRendererTests(unittest.TestCase):
    def test_renderer_loads(self):
        reporter = oracle.reporters()
        self.assertTrue(callable(reporter.write_realizer_extension_workbook))


if __name__ == '__main__':
    unittest.main()
