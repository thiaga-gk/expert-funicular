import tempfile
import unittest
from pathlib import Path

import publisher
import realizer
from contracts import reporter_contract as prc
from contracts import realizer_workbook_contract as rwc


class RealizerWorkbookContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ev = publisher.synthetic_demo_evidence(1)
        rep = publisher.build_report(ev, publisher.Thresholds())
        cls.pkg_obj = publisher.build_locked_recommendation_packages(rep, ev, publisher.Thresholds())[0]
        cls.pkg = prc.to_primitive(cls.pkg_obj)

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


if __name__ == '__main__':
    unittest.main()
