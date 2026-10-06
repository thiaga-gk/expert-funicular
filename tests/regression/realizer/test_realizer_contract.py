"""Realizer contract helpers of the frozen single-file oracle and of src/realizer/contracts."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical, oracle  # noqa: E402
canonical.modular()  # src/realizer, never a shadowing copy
from realizer.contracts import realization as modular_rc, realization_schema as modular_schema  # noqa: E402


class RealizerContractTests(unittest.TestCase):
    rc = oracle.rc()
    schema = oracle.realizer_schema()

    def test_canonical_decimal(self):
        self.assertEqual(self.rc.canonical_decimal(Decimal('1.00')), '1')
        self.assertEqual(self.rc.canonical_decimal(Decimal('0.000')), '0')
        self.assertEqual(self.rc.canonical_decimal(Decimal('-10.5000')), '-10.5')

    def test_canonical_json_is_order_independent(self):
        self.assertEqual(self.rc.canonical_json({'b': 2, 'a': 1}), self.rc.canonical_json({'a': 1, 'b': 2}))

    def test_content_id_is_deterministic_96_bit_display(self):
        a = self.rc.content_id('RPKG', {'x': 1})
        b = self.rc.content_id('RPKG', {'x': 1})
        self.assertEqual(a, b)
        self.assertEqual(len(a.split('-', 1)[1]), 24)

    def test_schema_files_are_valid_json_schema(self):
        jsonschema.Draft202012Validator.check_schema(self.schema.realization_package_schema())
        jsonschema.Draft202012Validator.check_schema(self.schema.ledger_entry_schema())

    def test_enums_exclude_partial_application(self):
        self.assertNotIn('PARTIALLY_APPLIED', [x.value for x in self.rc.ImplementationConformance])
        self.assertEqual(
            [x.value for x in self.rc.ImplementationConformance],
            ['EXACT', 'MODIFIED', 'NOT_APPLIED', 'CONTRARY', 'UNRELATED'],
        )


class ModularRealizerContractTests(RealizerContractTests):
    """The same contract helpers from realizer.contracts."""
    rc = modular_rc
    schema = modular_schema


if __name__ == '__main__':
    unittest.main()
