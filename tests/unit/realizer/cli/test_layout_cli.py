"""The CLI's output layout (src/realizer/config/output_layout.py): one long-lived state, one run folder per run.

By default a run writes its realization packages, snapshots and log into a new
``<output-dir>/run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/`` folder and keeps the history (ledger, evidence,
regeneration requests, measurements, source package copies, policies, state index) in ``--state-dir``, default
``<output-dir>/state``; daily runs into one ``--output-dir`` therefore continue one history. ``--flat-output`` is the
single-folder layout of the facade. A refused input writes nothing in either layout.
"""
import contextlib
import datetime as dt
import io
import re
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.helpers import ROOT  # noqa: F401  (src/realizer on the path)
from tests import realizer_world as world
from realizer.cli.main import main
from realizer.common import logging as rlogging
from realizer.config import output_layout

PACKAGE = world.PACKAGES / "autostop-scoped-pro.json"
RUN = re.compile(r"^run_date=\d{4}-\d\d-\d\d/run_ts=\d{8}T\d{6}Z(-\d\d)?$")


def files(root: Path):
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


class LayoutCliTests(unittest.TestCase):
    def setUp(self):
        saved = (list(rlogging.LOGGER.handlers), rlogging.LOGGER.level, rlogging.LOGGER.propagate)

        def restore():
            for handler in rlogging.LOGGER.handlers:
                handler.close()
            rlogging.LOGGER.handlers, rlogging.LOGGER.level, rlogging.LOGGER.propagate = saved
        self.addCleanup(restore)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_run_writes_its_projections_and_log_to_a_run_folder_and_its_history_to_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, stdout, err = self.run_cli("--package", str(PACKAGE), "--demo", "--output-dir", str(out))
            self.assertEqual(code, 0, err)
            (run_dir,) = [p for p in out.glob("run_date=*/run_ts=*")]
            self.assertRegex(run_dir.relative_to(out).as_posix(), RUN)
            self.assertEqual(Path(stdout.strip()).parent, run_dir / "realization" / "packages")
            in_run = files(run_dir)
            self.assertEqual({k.split("/")[1] if k.startswith("realization/") else k for k in in_run}, {"packages", "snapshots", "realizer.log"})
            log = in_run["realizer.log"]
            self.assertRegex(log.splitlines()[0], r"Z INFO realizer\.start version=")
            self.assertIn("realizer.done rpkg=RPKG-", log)
            state = files(out / "state")
            self.assertTrue({k.split("/")[1] for k in state} <= {"ledger", "evidence", "measurements", "sources", "state", "policies", "regeneration"})
            self.assertTrue({"ledger", "measurements", "sources", "state"} <= {k.split("/")[1] for k in state})
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["run_date=" + run_dir.parent.name.split("=", 1)[1], "state"])

    def test_the_single_folder_layout_writes_realizer_log_beside_the_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli("--package", str(PACKAGE), "--demo", "--output-dir", tmp, "--flat-output")
            self.assertEqual(code, 0, err)
            log = (Path(tmp) / "realizer.log").read_text(encoding="utf-8").splitlines()
            self.assertRegex(log[0], r"Z INFO realizer\.start version=")
            self.assertTrue(any("realizer.layout run_dir=" in line and "flat=True" in line for line in log))
            self.assertRegex(log[-1], r"Z INFO realizer\.done rpkg=RPKG-\w+ rsnap=RSNAP-\w+ state=\w+ elapsed_ms=\d+$")
            self.assertFalse(any("ledger.append" in line for line in log), "a ledger entry is DEBUG detail")

    def test_daily_runs_into_one_output_dir_continue_one_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, flat = Path(tmp, "out"), Path(tmp, "flat")
            printed = []
            for as_of in ("2026-10-20T12:00:00Z", "2026-11-07T12:00:00Z"):
                code, stdout, err = self.run_cli("--package", str(PACKAGE), "--demo", "--as-of", as_of, "--output-dir", str(out))
                self.assertEqual(code, 0, err)
                printed.append(Path(stdout.strip()))
                code, _, err = self.run_cli("--package", str(PACKAGE), "--demo", "--as-of", as_of, "--output-dir", str(flat), "--flat-output")
                self.assertEqual(code, 0, err)
            self.assertEqual(len(list(out.glob("run_date=*/run_ts=*"))), 2)
            self.assertNotEqual(printed[0].parents[2], printed[1].parents[2])
            # The state is the flat run's history, file for file; the last run folder holds the flat run's projections.
            self.assertEqual(files(out / "state"), {k: v for k, v in files(flat).items() if k != "realizer.log" and not k.startswith(("realization/packages/", "realization/snapshots/"))})
            self.assertEqual(printed[1].read_text(encoding="utf-8"), (flat / "realization" / "packages" / printed[1].name).read_text(encoding="utf-8"))
            self.assertEqual(len(list((out / "state" / "realization" / "measurements").glob("*.json"))), 2)

    def test_an_explicit_state_dir_holds_the_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, state = Path(tmp, "out"), Path(tmp, "elsewhere")
            code, _, err = self.run_cli("--package", str(PACKAGE), "--demo", "--output-dir", str(out), "--state-dir", str(state))
            self.assertEqual(code, 0, err)
            self.assertFalse((out / "state").exists())
            self.assertTrue((state / "realization" / "ledger").is_dir())

    def test_a_refused_input_writes_nothing_in_the_run_folder_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["--package", str(Path(tmp) / "missing.json")],
                         ["--package", str(PACKAGE), "--demo", "--replay-through", "2000-01-01T00:00:00Z"]):
                with self.subTest(argv=argv[2:]):
                    code, stdout, _ = self.run_cli(*argv, "--output-dir", str(Path(tmp) / "out"))
                    self.assertEqual((code, stdout), (2, ""))
                    self.assertFalse((Path(tmp) / "out").exists())


class LayoutResolutionTests(unittest.TestCase):
    def test_state_dir_defaults(self):
        self.assertEqual(output_layout.resolve_state_dir(Path("o")), Path("o/state"))
        self.assertEqual(output_layout.resolve_state_dir(Path("o"), flat=True), Path("o"))
        self.assertEqual(output_layout.resolve_state_dir(Path("o"), Path("s"), flat=True), Path("s"))

    def test_run_folders_are_named_after_the_utc_start_and_never_reused(self):
        now = dt.datetime(2026, 10, 7, 13, 5, 9, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
        with tempfile.TemporaryDirectory() as tmp:
            first = output_layout.resolve_layout(Path(tmp), now=now)
            second = output_layout.resolve_layout(Path(tmp), now=now)
            self.assertEqual(first.run_dir.relative_to(tmp).as_posix(), "run_date=2026-10-07/run_ts=20261007T170509Z")
            self.assertEqual(second.run_dir.name, "run_ts=20261007T170509Z-01")
            self.assertEqual((first.state_dir, first.flat, first.log_path), (Path(tmp) / "state", False, first.run_dir / "realizer.log"))
            flat = output_layout.resolve_layout(Path(tmp) / "f", flat=True)
            self.assertEqual((flat.run_dir, flat.state_dir, flat.log_path), (Path(tmp) / "f", Path(tmp) / "f", Path(tmp) / "f" / "realizer.log"))
            self.assertFalse((Path(tmp) / "f").exists())


if __name__ == "__main__":
    unittest.main()
