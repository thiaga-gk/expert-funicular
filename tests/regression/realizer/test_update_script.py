"""``scripts/update_realizer_goldens.py``: it needs an approved change, and its module list matches the stored expectations."""
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402

SCRIPT = ROOT / "scripts" / "update_realizer_goldens.py"


def load_script():
    spec = importlib.util.spec_from_file_location("update_realizer_goldens", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*argv):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            status = load_script().main(list(argv))
        except SystemExit as exc:
            status = exc.code
    return status, err.getvalue()


class UpdateScriptTests(unittest.TestCase):
    def test_an_approved_change_is_required(self):
        status, err = run()
        self.assertEqual(status, 2)
        self.assertIn("--approved-change", err)
        self.assertEqual(run("--approved-change", "  ")[0], 2)

    def test_unknown_scenarios_and_mixed_modes_are_argument_errors(self):
        self.assertEqual(run("--approved-change", "x", "--scenario", "GR-999")[0], 2)
        self.assertEqual(run("--approved-change", "x", "--expectations", "--scenario", "GR-001")[0], 2)

    def test_the_expectation_modules_are_exactly_the_stored_files(self):
        modules = load_script().expectation_modules()
        self.assertIn("tests.unit.realizer.estimator.test_economics", modules)
        stored = sorted(p.stem for p in (canonical.FIXTURES / "expectations").glob("*.json"))
        self.assertEqual(sorted(m[len("tests."):] for m in modules if m[len("tests."):] in stored), stored)
        self.assertEqual([s for s in stored if "tests." + s not in modules], [], "a stored file whose test module is gone")

    def test_stored_expectations_stay_compact(self):
        size = sum(p.stat().st_size for p in (canonical.FIXTURES / "expectations").glob("*.json"))
        self.assertLess(size, 800_000, "expectations are digests and short values, not result dumps")


if __name__ == "__main__":
    unittest.main()
