"""Exit statuses of ``scripts/compare_realizer_with_ref.py``: 0 identical, 1 a difference (or a crashed run), 2 an unusable
ref or invalid arguments. The comparison itself is exercised with real git refs on one or two scenarios to stay fast,
and with doctored per-side outputs for the difference and crash cases.

There is no comparison against the single-file oracle tag (``realizer-reorg-oracle-1.0.0``): that Realizer reads only
Reporter JSON Contract 1.0.0 packages, which the Realizer no longer accepts (the fixtures are 3.1.0 packages), so the two
engines cannot run the same inputs. Their equivalence on the stored scenarios was proved once, before the input contract
changed, by the decision-equivalence check recorded in docs/proposals/realizer/repo-reorg-parity.md."""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402

SCRIPT = ROOT / "scripts" / "compare_realizer_with_ref.py"
SCENARIOS = ["--scenario", "GR-001", "--scenario", "GR-022"]


def _git_ok(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).returncode == 0
    except OSError:
        return False


HAVE_GIT = _git_ok("rev-parse", "--verify", "--quiet", "HEAD^{commit}")


def load_script():
    spec = importlib.util.spec_from_file_location("compare_realizer_with_ref", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(*argv, script=None):
    """``main(argv)`` of the script: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = (script or load_script()).main(list(argv))
        except SystemExit as exc:  # argparse errors
            status = exc.code
    return status, out.getvalue(), err.getvalue()


class ArgumentTests(unittest.TestCase):
    def test_ref_is_required(self):
        status, _, err = run_script()
        self.assertEqual(status, 2)
        self.assertIn("--ref is required", err)

    def test_unknown_scenario_is_an_argument_error(self):
        status, _, err = run_script("--ref", "HEAD", "--scenario", "GR-999")
        self.assertEqual(status, 2)
        self.assertIn("unknown scenario", err)

    @unittest.skipUnless(HAVE_GIT, "needs a git checkout")
    def test_unknown_ref_is_an_error(self):
        status, out, err = run_script("--ref", "no-such-ref-for-the-realizer", *SCENARIOS)
        self.assertEqual(status, 2)
        self.assertIn("not a git commit", err)
        self.assertNotIn("identical", out, "no comparison may be reported as passing")


class DifferenceTests(unittest.TestCase):
    """The reporting and exit status for outputs that differ, with the two workers replaced by canned results."""

    @staticmethod
    def doctored(ref_maps, work_maps):
        script = load_script()
        sides = iter([ref_maps, work_maps])
        script.checkout = lambda ref, target: None
        script.run_worker = lambda tree, names, out: next(sides)
        script.subprocess = types.SimpleNamespace(run=lambda *args, **kwargs: None)   # no git worktree to remove
        return script

    def run_with(self, ref_maps, work_maps):
        return run_script("--ref", "HEAD", *SCENARIOS, script=self.doctored(ref_maps, work_maps))

    def maps(self, names=("GR-001", "GR-022")):
        return {name: canonical.stored_expected(name) for name in names}

    def test_identical_outputs_exit_0(self):
        status, out, err = self.run_with(self.maps(), self.maps())
        self.assertEqual((status, err), (0, ""))
        self.assertIn("2 scenario(s) identical to HEAD", out)

    def test_a_differing_file_exits_1_and_names_the_aspect(self):
        drifted = self.maps()
        rel = next(r for r in drifted["GR-022"] if r.startswith("realization/ledger/"))
        drifted["GR-022"] = {**drifted["GR-022"], rel: drifted["GR-022"][rel] + "\n"}
        status, out, _ = self.run_with(self.maps(), drifted)
        self.assertEqual(status, 1)
        self.assertIn("GR-022 [ref vs working tree]", out)
        self.assertIn("differs", out)
        self.assertIn("difference(s) across 2 scenario(s) against HEAD", out)

    def test_a_file_only_on_one_side_exits_1(self):
        extra = self.maps()
        extra["GR-001"] = {**extra["GR-001"], "realization/extra.json": "{}"}
        status, out, _ = self.run_with(self.maps(), extra)
        self.assertEqual(status, 1)
        self.assertIn("realization/extra.json only in actual", out)

    def test_a_crash_on_one_side_exits_1(self):
        crashed = self.maps()
        crashed["GR-001"] = {"__error__": "RuntimeError: deliberate failure"}
        status, out, _ = self.run_with(self.maps(), crashed)
        self.assertEqual(status, 1)
        self.assertIn("GR-001 [working tree]: run failed: RuntimeError: deliberate failure", out)

    def test_the_same_crash_on_both_sides_is_not_a_difference(self):
        crashed = {**self.maps(), "GR-001": {"__error__": "RuntimeError: same on both"}}
        status, _, _ = self.run_with(crashed, dict(crashed))
        self.assertEqual(status, 0)


@unittest.skipUnless(HAVE_GIT, "needs a git checkout")
class RealRefTests(unittest.TestCase):
    """Real refs, real worktrees and fresh interpreters (what a shell or CI sees)."""

    @staticmethod
    def process(*argv):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
        return subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=ROOT, env=env, text=True, capture_output=True)

    def test_head_is_identical_to_the_working_tree_outputs(self):
        done = self.process("--ref", "HEAD", *SCENARIOS)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("2 scenario(s) identical to HEAD", done.stdout)

    def test_unknown_ref_exits_2(self):
        self.assertEqual(self.process("--ref", "no-such-ref-for-the-realizer", "--scenario", "GR-001").returncode, 2)


if __name__ == "__main__":
    unittest.main()
