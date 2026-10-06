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
from realizer.contracts.request import RealizationRequest  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
