"""Smoke tests of ``scripts/compare_with_ref.py``: it finds a run's outputs in both output layouts (flat, and the
``run_date=*/run_ts=*`` run folders), and a real comparison of one golden case against ``HEAD`` runs to its summary
line. The canonical comparison itself is covered by the golden and regression tests; this guards the script."""
import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "compare_with_ref.py"


def load_script():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("compare_with_ref", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_ok(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).returncode == 0
    except OSError:
        return False


class RunDirTests(unittest.TestCase):
    def setUp(self):
        self.script = load_script()

    def test_flat_output_is_the_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "publisher.log").write_text("x", encoding="utf-8")
            self.assertEqual(self.script.run_dir(out), out)

    def test_latest_run_folder_is_the_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for ts in ("20261005T100000Z", "20261005T110000Z"):
                folder = out / "run_date=2026-10-05" / f"run_ts={ts}"
                folder.mkdir(parents=True)
                (folder / "publisher.log").write_text("x", encoding="utf-8")
            self.assertEqual(self.script.run_dir(out).name, "run_ts=20261005T110000Z")

    def test_no_run_exits_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                self.script.run_dir(Path(tmp))
            self.assertIn("no publisher.log", str(ctx.exception))


@unittest.skipUnless(_git_ok("rev-parse", "--verify", "--quiet", "HEAD^{commit}"), "needs a git checkout")
class CompareAgainstHeadTests(unittest.TestCase):
    def test_one_case_against_head_reaches_the_summary(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = load_script().main(["--ref", "HEAD", "--no-demo", "--case", "c06-oversized-warehouse"])
        self.assertIn(status, (0, 1))
        self.assertIn("runs compared against HEAD", out.getvalue())


if __name__ == "__main__":
    unittest.main()
