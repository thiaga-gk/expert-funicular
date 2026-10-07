"""Realizer run request (src/realizer/contracts/request.py): exactly the inputs ``realize()`` takes, frozen."""
import dataclasses
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests import realizer_expect as expect  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402
from tests.unit.realizer.helpers import fixture_package  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.common.errors import InputError  # noqa: E402
from realizer.contracts.realization import make_rpkg_id  # noqa: E402
from realizer.contracts.request import RealizationRequest, RunContext  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402


class RealizationRequestTests(unittest.TestCase):
    def test_fields_are_the_realize_parameters(self):
        fields = [f.name for f in dataclasses.fields(RealizationRequest)]
        self.assertEqual(fields, list(inspect.signature(realize).parameters))

    def test_defaults_match_realize(self):
        request = RealizationRequest(package={"recommendation_package_id": "PKG-1"}, observation=None, output_dir=Path("out"))
        signature = inspect.signature(realize).parameters
        self.assertIsNone(request.policy)
        self.assertIsNone(request.replay_through)
        self.assertIsNone(signature["policy"].default)
        self.assertIsNone(signature["replay_through"].default)

    def test_request_is_frozen(self):
        request = RealizationRequest(package={}, observation={}, output_dir=Path("out"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.output_dir = Path("other")


class RunContextTests(unittest.TestCase):
    """``build_context`` is the first three statements of ``realize``: package gate, policy default, RPKG id."""

    def setUp(self):
        self.pkg = fixture_package()

    def test_context_carries_the_request_the_default_policy_and_the_rpkg_id(self):
        request = RealizationRequest(self.pkg, None, Path("out"))
        context = build_context(request)
        self.assertIsInstance(context, RunContext)
        self.assertIs(context.request, request)
        self.assertEqual(context.policy, RealizerPolicy())
        self.assertEqual(context.realization_package_id, make_rpkg_id(self.pkg["recommendation_package_id"]))
        explicit = RealizerPolicy(minimum_days=3)
        self.assertIs(build_context(RealizationRequest(self.pkg, None, Path("out"), policy=explicit)).policy, explicit)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.policy = None

    def test_malformed_packages_raise_what_realize_raises_first(self):
        import tempfile
        cases = {
            "empty": {},
            "contract 9.9.9": {**self.pkg, "contract_version": "9.9.9"},
            "bad id": {**self.pkg, "recommendation_package_id": "X-1"},
            "no candidates": {k: v for k, v in self.pkg.items() if k != "candidates"},
        }
        for label, package in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                try:
                    build_context(RealizationRequest(package, None, Path(tmp)))
                except Exception as exc:  # noqa: BLE001 - stored
                    new = (type(exc).__name__, str(exc))
                else:
                    new = None
                self.assertIsNotNone(new)
                self.assertEqual(new[0], "InputError")  # a ValueError that the CLI maps to exit status 2
                self.assertTrue(issubclass(InputError, ValueError))
                expect.check(self, label, new)

if __name__ == "__main__":
    unittest.main()
