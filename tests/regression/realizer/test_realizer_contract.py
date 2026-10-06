"""Realizer contract helpers of the frozen single-file oracle."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import oracle  # noqa: E402

rc = oracle.rc()
ledger_entry_schema = oracle.realizer_schema().ledger_entry_schema
realization_package_schema = oracle.realizer_schema().realization_package_schema


class RealizerContractTests(unittest.TestCase):
    def test_canonical_decimal(self):
        self.assertEqual(rc.canonical_decimal(Decimal('1.00')), '1')
        self.assertEqual(rc.canonical_decimal(Decimal('0.000')), '0')
        self.assertEqual(rc.canonical_decimal(Decimal('-10.5000')), '-10.5')

    def test_canonical_json_is_order_independent(self):
        self.assertEqual(rc.canonical_json({'b': 2, 'a': 1}), rc.canonical_json({'a': 1, 'b': 2}))

    def test_content_id_is_deterministic_96_bit_display(self):
        a = rc.content_id('RPKG', {'x': 1})
        b = rc.content_id('RPKG', {'x': 1})
        self.assertEqual(a, b)
        self.assertEqual(len(a.split('-', 1)[1]), 24)

    def test_schema_files_are_valid_json_schema(self):
        jsonschema.Draft202012Validator.check_schema(realization_package_schema())
        jsonschema.Draft202012Validator.check_schema(ledger_entry_schema())

    def test_enums_exclude_partial_application(self):
        self.assertNotIn('PARTIALLY_APPLIED', [x.value for x in rc.ImplementationConformance])
        self.assertEqual(
            [x.value for x in rc.ImplementationConformance],
            ['EXACT', 'MODIFIED', 'NOT_APPLIED', 'CONTRARY', 'UNRELATED'],
        )


if __name__ == '__main__':
    unittest.main()
