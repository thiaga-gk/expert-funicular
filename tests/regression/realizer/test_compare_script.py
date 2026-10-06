"""Exit statuses of ``scripts/compare_realizer_single_file.py``: 0 identical, 1 a difference (or a candidate crash),
2 a candidate that cannot be resolved. Each case runs the script's ``main`` on one or two scenarios to stay fast."""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402

SCRIPT = ROOT / "scripts" / "compare_realizer_single_file.py"
SCENARIOS = ["--scenario", "GR-001", "--scenario", "GR-022"]
ALTERED = "tests.regression.realizer.altered_engines"


def _load_script():
    spec = importlib.util.spec_from_file_location("compare_realizer_single_file", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(*argv):
    """``main(argv)`` of the script: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = _load_script().main(list(argv))
        except SystemExit as exc:  # argparse errors
            status = exc.code
    return status, out.getvalue(), err.getvalue()


class CompareScriptExitStatusTests(unittest.TestCase):
    def test_default_modular_candidate_is_identical(self):
        status, out, err = run_script(*SCENARIOS)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("candidate: modular realizer (realizer.realizer)", out)
        self.assertIn("2 scenario(s): oracle = stored baseline = candidate", out)

    def test_default_candidate_goes_through_the_shadowing_guard(self):
        calls = []
        script = _load_script()
        original = canonical.modular_engine
        canonical.modular_engine = lambda: calls.append("guard") or original()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(["--scenario", "GR-001"]), 0)
        finally:
            canonical.modular_engine = original
        self.assertEqual(calls, ["guard"])

    def test_failed_shadowing_guard_is_an_error(self):
        script = _load_script()
        original = canonical.modular_engine

        def shadowed():
            raise ImportError("realizer.realizer resolved to /elsewhere/realizer.py")

        canonical.modular_engine = shadowed
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                status = script.main(["--scenario", "GR-001"])
        finally:
            canonical.modular_engine = original
        self.assertEqual(status, 2)
        self.assertIn("resolved to /elsewhere/realizer.py", err.getvalue())

    def test_missing_candidate_is_an_error(self):
        status, out, err = run_script("--candidate", "no_such_realizer_package.realizer", *SCENARIOS)
        self.assertEqual(status, 2)
        self.assertIn("no_such_realizer_package", err)
        self.assertIn("cannot be resolved", err)
        self.assertNotIn("stored baseline only", out)
        self.assertNotIn("scenario(s):", out, "no comparison may be reported as passing")

    def test_missing_candidate_attr_and_non_engine_are_errors(self):
        for spec in (f"{ALTERED}:no_such_factory", f"{ALTERED}:not_an_engine"):
            with self.subTest(candidate=spec):
                status, _, err = run_script("--candidate", spec, *SCENARIOS)
                self.assertEqual(status, 2)
                self.assertIn("cannot be resolved", err)

    def test_oracle_only_is_explicit_and_succeeds(self):
        status, out, err = run_script("--oracle-only", *SCENARIOS)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("candidate: skipped (--oracle-only)", out)
        self.assertIn("2 scenario(s): oracle = stored baseline\n", out)

    def test_oracle_only_does_not_resolve_the_candidate(self):
        status, _, err = run_script("--oracle-only", "--candidate", "no_such_realizer_package", *SCENARIOS)
        self.assertEqual((status, err), (0, ""))

    def test_unknown_scenario_is_an_argument_error(self):
        status, _, err = run_script("--scenario", "GR-999")
        self.assertEqual(status, 2)
        self.assertIn("unknown scenario", err)

    def test_candidate_that_differs_from_the_oracle_exits_1(self):
        status, out, err = run_script("--candidate", f"{ALTERED}:drifting", *SCENARIOS)
        self.assertEqual(status, 1)
        self.assertIn("[oracle vs candidate]", out)
        self.assertIn("differs", out)
        self.assertIn("difference(s) across 2 scenario(s)", out)

    def test_candidate_that_crashes_exits_1(self):
        status, out, _ = run_script("--candidate", f"{ALTERED}:crashing", "--scenario", "GR-002")
        self.assertEqual(status, 1)
        self.assertIn("GR-002 [candidate]: run failed: RuntimeError: deliberate candidate failure", out)

    def test_faithful_factory_candidate_is_identical(self):
        status, _, err = run_script("--candidate", "tests.regression.realizer.canonical:modular_engine", *SCENARIOS)
        self.assertEqual((status, err), (0, ""))


class CompareScriptProcessExitCodeTests(unittest.TestCase):
    """The real process exit status (what a shell or CI sees), not just ``main``'s return value."""

    @staticmethod
    def _run(*argv):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
        return subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=ROOT, env=env, text=True, capture_output=True)

    def test_exit_codes(self):
        cases = [
            (0, ["--scenario", "GR-001"]),
            (0, ["--oracle-only", "--scenario", "GR-001"]),
            (1, ["--candidate", f"{ALTERED}:drifting", "--scenario", "GR-022"]),
            (2, ["--candidate", "no_such_realizer_package", "--scenario", "GR-001"]),
        ]
        for code, argv in cases:
            with self.subTest(argv=argv):
                done = self._run(*argv)
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
