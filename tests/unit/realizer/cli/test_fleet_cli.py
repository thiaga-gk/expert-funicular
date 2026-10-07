"""``realizer --publisher-run``: a fleet run from the command line (src/realizer/cli/realize.py ``run_fleet``).

The CLI checks the options, reads the Publisher run (the named run folder, or the newest under a root) and its selected
warehouses before anything is written, then writes one run folder (realization packages and snapshots, the run
manifest, the realization workbook, ``realizer.log``) and the state. It prints one line per warehouse and the manifest
and workbook paths, and exits 1 when any warehouse crashed, else 2 when any was refused, else 0. A refusal of the whole
run before any warehouse starts exits 2 and writes nothing.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.helpers import ROOT  # noqa: F401  (src/realizer on the path)
from tests.unit.realizer import fleet_support
from realizer import fleet
from realizer.cli.main import main
from realizer.common import logging as rlogging
from realizer.config.loader import load_publisher_run


class FleetCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fleet_support.publisher_demo_root()
        cls.run_dir = load_publisher_run(cls.root).run_dir

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
            try:
                code = main(list(argv))
            except SystemExit as exc:  # argparse
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_a_demo_fleet_run_writes_a_run_folder_and_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, stdout, err = self.run_cli("--publisher-run", str(self.root), "--demo", "--output-dir", str(out))
            self.assertEqual(code, 0, err)
            (run_dir,) = list(out.glob("run_date=*/run_ts=*"))
            lines = stdout.splitlines()
            self.assertEqual(lines[:3], ["1234567890123456/wh-demo-006: OK", "2234567890123456/wh-demo-013: OK", "2234567890123456/wh-demo-039: OK"])
            self.assertEqual(lines[3:], [f"manifest: {run_dir / 'manifest.json'}", f"workbook: {run_dir / 'realization_workbook.xlsx'}"])
            self.assertEqual(sorted(p.name for p in run_dir.iterdir()), ["manifest.json", "realization", "realization_workbook.xlsx", "realizer.log"])
            log = (run_dir / "realizer.log").read_text(encoding="utf-8")
            self.assertIn("realizer.start", log.splitlines()[0])
            self.assertIn("fleet.done ok=3 refused=0 crashed=0", log)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((manifest["state_dir"], manifest["publisher_run"]["run_dir"]), (str(out / "state"), str(self.run_dir)))
            self.assertEqual(len(list((out / "state" / "realization" / "ledger").glob("*.jsonl"))), 3)

    def test_the_named_run_folder_the_warehouse_filter_and_the_flat_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, stdout, err = self.run_cli("--publisher-run", str(self.run_dir), "--demo", "--warehouse", "wh-demo-013",
                                             "--warehouse", "1234567890123456/wh-demo-006", "--output-dir", str(out), "--flat-output")
            self.assertEqual(code, 0, err)
            self.assertEqual(stdout.splitlines()[:2], ["1234567890123456/wh-demo-006: OK", "2234567890123456/wh-demo-013: OK"])
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["manifest.json", "realization", "realization_workbook.xlsx"])
            self.assertTrue((out / "realization" / "ledger").is_dir())
            self.assertEqual(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["publisher_run"]["selected_package_count"], 2)

    def test_exit_status_1_on_a_crash_and_2_on_a_refusal(self):
        for error, status in ((RuntimeError("value 99"), 1), (fleet.InputError("refused"), 2)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(fleet, "observe", side_effect=error):
                code, stdout, err = self.run_cli("--publisher-run", str(self.root), "--demo", "--output-dir", tmp)
                self.assertEqual(code, status)
                self.assertNotIn("value 99", stdout + err)
                self.assertTrue(stdout.splitlines()[-1].startswith("workbook: "))

    def test_refusals_before_any_warehouse_starts_exit_2_and_write_nothing(self):
        cases = (
            (["--demo", "--warehouse", "wh-nope"], r"--warehouse wh-nope is not a warehouse of the Publisher run"),
            ([], r"a fleet run needs one source: --profile with --sql-warehouse-id, --demo or --from-evidence <folder>"),
            (["--demo", "--from-evidence", "x"], r"a fleet run needs one source: .*, not --demo and --from-evidence"),
            (["--demo", "--observation", "o.json"], r"--observation applies to one package \(--package\), not to a fleet run \(--publisher-run\)"),
            (["--demo", "--replay-through", "2026-10-01T00:00:00Z", "--print-json"], r"--replay-through and --print-json apply to one package"),
            (["--profile", "p"], r"--profile needs --sql-warehouse-id"),
            (["--from-evidence", "missing-folder"], r"a fleet run reads measurement evidence from a folder: missing-folder is not one"),
        )
        for extra, pattern in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                code, stdout, err = self.run_cli("--publisher-run", str(self.root), *extra, "--output-dir", str(Path(tmp) / "out"))
                self.assertEqual((code, stdout), (2, ""))
                self.assertRegex(err.splitlines()[-1], rf"^realizer: {pattern}")
                self.assertFalse((Path(tmp) / "out").exists())
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli("--publisher-run", tmp, "--demo", "--output-dir", str(Path(tmp) / "out"))
            self.assertEqual(code, 2)
            self.assertRegex(err.splitlines()[-1], r"^realizer: no Publisher run \(manifest.json or run_date=\*/run_ts=\* folders\) under ")
            code, _, err = self.run_cli("--package", "p.json", "--publisher-run", tmp, "--demo")
            self.assertEqual(code, 2)
            self.assertIn("not allowed with argument", err)
            code, _, err = self.run_cli("--package", "p.json", "--warehouse", "wh-1", "--output-dir", str(Path(tmp) / "out"))
            self.assertEqual(code, 2)
            self.assertIn("--warehouse selects warehouses of a fleet run (--publisher-run)", err)


if __name__ == "__main__":
    unittest.main()
