"""Run folders under --output-dir (src/publisher/config/output_layout.py) and the CLI that resolves them."""
import contextlib
import datetime as dt
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.cli.main import main as cli_main  # noqa: E402
from publisher.config.output_layout import latest_run_dir, partitioned_run_dir  # noqa: E402
from tests.golden import load_case  # noqa: E402

NOW = dt.datetime(2026, 10, 5, 14, 30, 12, tzinfo=dt.timezone.utc)


class OutputLayoutTests(unittest.TestCase):
    def test_run_folder_is_hive_style_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = NOW.astimezone(dt.timezone(dt.timedelta(hours=-7)))  # same instant, another offset
            run = partitioned_run_dir(Path(tmp), local)
            self.assertEqual(run.relative_to(tmp).as_posix(), "run_date=2026-10-05/run_ts=20261005T143012Z")
            self.assertTrue(run.is_dir())

    def test_same_second_never_reuses_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = [partitioned_run_dir(Path(tmp), NOW).name for _ in range(3)]
            self.assertEqual(names, ["run_ts=20261005T143012Z", "run_ts=20261005T143012Z-01", "run_ts=20261005T143012Z-02"])
            self.assertEqual(latest_run_dir(Path(tmp)).name, "run_ts=20261005T143012Z-02")

    def test_latest_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                latest_run_dir(root)
            older = partitioned_run_dir(root, NOW - dt.timedelta(days=1))
            newer = partitioned_run_dir(root, NOW)
            self.assertEqual(latest_run_dir(root), newer)
            (older / "manifest.json").write_text("{}")
            self.assertEqual(latest_run_dir(older), older)  # a run folder resolves to itself


class CliRunFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(argv)
        return code, out.getvalue()

    def evidence_file(self, tmp):
        path = Path(tmp) / "evidence.json"
        path.write_text(json.dumps({"evidence_format_version": "1.0.0", "publisher_version": "test",
                                    "warehouses": load_case("idle-expensive-warehouse")["evidence"]}))
        return path

    def test_identical_replays_write_identical_files_in_separate_run_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev, out = self.evidence_file(tmp), Path(tmp) / "out"
            args = ["--from-evidence", str(ev), "--no-workbook", "--output-dir", str(out)]
            self.assertEqual(self.run_cli(args)[0], 0)
            code, stdout = self.run_cli(args)
            self.assertEqual(code, 0)
            runs = sorted(p for p in out.glob("run_date=*/run_ts=*"))
            self.assertEqual(len(runs), 2)
            self.assertIn(f"run folder: {runs[-1]}", stdout)
            for run in runs:
                self.assertTrue((run / "publisher.log").is_file())
            for rel in ("manifest.json", "evidence.json", "packages/1234567890123456__wh-demo-001.json"):
                self.assertEqual((runs[0] / rel).read_text(), (runs[1] / rel).read_text(), rel)

    def test_flat_output_writes_into_the_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev, out = self.evidence_file(tmp), Path(tmp) / "flat"
            self.assertEqual(self.run_cli(["--from-evidence", str(ev), "--no-workbook", "--flat-output", "--output-dir", str(out)])[0], 0)
            self.assertTrue((out / "manifest.json").is_file())
            self.assertEqual(list(out.glob("run_date=*")), [])

    def test_scoped_replay_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev, out = self.evidence_file(tmp), Path(tmp) / "scoped"
            code, _ = self.run_cli(["--from-evidence", str(ev), "--no-workbook", "--flat-output", "--output-dir", str(out),
                                    "--scope", "current:auto-stop", "--warehouse", "wh-demo-001"])
            self.assertEqual(code, 0)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["run_scope"], {"label": "current: auto_stop",
                                                     "scenarios": [{"scenario_type": "IN_PLACE_OPTIMIZATION", "knobs": ["auto_stop_minutes"]}]})
            self.assertEqual(manifest["warehouse_selection"], {"requested": ["wh-demo-001"], "selected_count": 1, "not_selected_count": 0})
            package = json.loads(next((out / "packages").glob("*.json")).read_text())
            self.assertEqual([c["knob"] for c in package["candidates"]], ["auto_stop_minutes"])
            self.assertEqual(package["run_scope"]["scenarios"], [{"scenario_type": "IN_PLACE_OPTIMIZATION", "knobs": ["auto_stop_minutes"]}])

    def test_invalid_scope_or_unknown_warehouse_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = self.evidence_file(tmp)
            for extra in (["--scope", "current:photon"], ["--warehouse", "wh-missing"],
                          ["--scope-file", str(ROOT / "examples" / "scopes" / "full.json"), "--scope", "current"]):
                with self.subTest(extra=extra):
                    code, _ = self.run_cli(["--from-evidence", str(ev), "--no-workbook", "--output-dir", f"{tmp}/x", *extra])
                    self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
