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
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer import realizer as engine  # noqa: E402
from realizer.contracts.request import RealizationRequest, RunContext  # noqa: E402


class RealizationRequestTests(unittest.TestCase):
    def test_fields_are_the_realize_parameters(self):
        fields = [f.name for f in dataclasses.fields(RealizationRequest)]
        self.assertEqual(fields, list(inspect.signature(engine.realize).parameters))
        self.assertEqual(fields, list(inspect.signature(oracle.engine().realize).parameters))

    def test_defaults_match_realize(self):
        request = RealizationRequest(package={"recommendation_package_id": "PKG-1"}, observation=None, output_dir=Path("out"))
        signature = inspect.signature(engine.realize).parameters
        self.assertIsNone(request.policy)
        self.assertIsNone(request.replay_through)
        self.assertIsNone(signature["policy"].default)
        self.assertIsNone(signature["replay_through"].default)

    def test_request_is_frozen(self):
        request = RealizationRequest(package={}, observation={}, output_dir=Path("out"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.output_dir = Path("other")


class RunContextTests(unittest.TestCase):
    """``build_context`` is the first three statements of the oracle's ``realize``: shape gate, policy default, RPKG id."""

    def setUp(self):
        import json
        self.pkg = json.loads(next(canonical.PACKAGES.glob("PKG-*.json")).read_text(encoding="utf-8"))
        self.oengine = oracle.engine()

    def test_context_carries_the_request_the_default_policy_and_the_rpkg_id(self):
        request = RealizationRequest(self.pkg, None, Path("out"))
        context = engine.build_context(request)
        self.assertIsInstance(context, RunContext)
        self.assertIs(context.request, request)
        self.assertEqual(context.policy, engine.RealizerPolicy())
        self.assertEqual(context.realization_package_id, self.oengine.make_rpkg_id(self.pkg["recommendation_package_id"]))
        explicit = engine.RealizerPolicy(minimum_days=3)
        self.assertIs(engine.build_context(RealizationRequest(self.pkg, None, Path("out"), policy=explicit)).policy, explicit)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.policy = None

    def test_malformed_packages_raise_what_the_oracle_realize_raises_first(self):
        import tempfile
        for package in [{}, {**self.pkg, "contract_version": "9.9.9"}, {**self.pkg, "recommendation_package_id": "X-1"},
                        {k: v for k, v in self.pkg.items() if k != "candidates"}]:
            with self.subTest(package=sorted(package)[:3]), tempfile.TemporaryDirectory() as tmp:
                try:
                    engine.build_context(RealizationRequest(package, None, Path(tmp)))
                except Exception as exc:  # noqa: BLE001 - compared
                    new = (type(exc).__name__, str(exc))
                else:
                    new = None
                try:
                    self.oengine.realize(package, None, Path(tmp))
                except Exception as exc:  # noqa: BLE001
                    old = (type(exc).__name__, str(exc))
                else:
                    old = None
                self.assertEqual(new, old)
                self.assertEqual(new[0], "ValueError")


if __name__ == "__main__":
    unittest.main()
