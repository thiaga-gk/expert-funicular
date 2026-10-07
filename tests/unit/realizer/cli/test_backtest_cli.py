"""``realizer --backtest``: its own options, one source (live, evidence replay or demo), refusals before anything runs.

A backtest needs no package. The live path refuses the collector warehouse when ``--warehouse`` names it before any
credential is read, and does not measure it otherwise; it writes the evidence first. ``--from-evidence`` replays that
file to the same report, and ``--demo`` runs the live path on a simulated fleet of past changes. stdout is one line per
change and the report path; refusals exit 2 with a one-line message naming options, never values.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer import realizer as facade
from realizer.analyzer.backtest import backtest_report
from realizer.analyzer.collector import backtest_collector, bridge
from realizer.cli import realize as cli_realize
from realizer.cli.main import EXIT_REFUSED, main
from realizer.common import logging as rlogging
from realizer.config.settings import SUPPORTED_WAREHOUSE_TYPES

REPORT = "realization/backtest/backtest-20261001T120000Z.json"
EVIDENCE = "realization/backtest/evidence-20261001T120000Z.json"


class BacktestCliTests(unittest.TestCase):
    def setUp(self):
        saved = (list(rlogging.LOGGER.handlers), rlogging.LOGGER.level, rlogging.LOGGER.propagate)  # main() configures the logger

        def restore():
            for handler in rlogging.LOGGER.handlers:
                handler.close()
            rlogging.LOGGER.handlers, rlogging.LOGGER.level, rlogging.LOGGER.propagate = saved
        self.addCleanup(restore)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--backtest", *argv, "--log-level", "ERROR"])
        return code, out.getvalue(), err.getvalue()

    def test_demo_writes_evidence_then_the_report_and_prints_a_line_per_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_cli("--demo", "--output-dir", tmp)
            self.assertEqual(code, 0, err)
            lines = out.splitlines()
            self.assertEqual(lines[-1], str(Path(tmp) / REPORT))
            self.assertEqual(len(lines), 1 + len(backtest_collector.DEMO_CHANGES))
            self.assertIn("2234567890123456/wh-demo-013 2026-08-01T14:32:00Z auto-stop 30->10: post 56 days", out)
            self.assertIn("3234567890123456/wh-demo-027 2026-08-12T10:00:00Z auto-stop 30->15: skipped OTHER_KNOB_CHANGED", out)
            self.assertIn("auto-stop 10->20", out)  # the raise
            self.assertIn("auto-stop 0->35", out)  # a never-stop warehouse given a value
            evidence = json.loads((Path(tmp) / EVIDENCE).read_text(encoding="utf-8"))
            report = json.loads((Path(tmp) / REPORT).read_text(encoding="utf-8"))
            self.assertEqual(report, backtest_report(evidence, supported_types=SUPPORTED_WAREHOUSE_TYPES))
            self.assertEqual((evidence["source"], report["source"]), ("DEMO_SIMULATION", "DEMO_SIMULATION"))
            text = (Path(tmp) / EVIDENCE).read_text(encoding="utf-8") + (Path(tmp) / REPORT).read_text(encoding="utf-8")
            for word in ("SELECT", "golden_scenario", "owned_by", "@", "tags"):
                self.assertNotIn(word, text)

    def test_from_evidence_replays_the_same_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_cli("--demo", "--output-dir", str(Path(tmp) / "a"))[0], 0)
            code, out, err = self.run_cli("--from-evidence", str(Path(tmp) / "a" / EVIDENCE), "--output-dir", str(Path(tmp) / "b"))
            self.assertEqual(code, 0, err)
            self.assertEqual((Path(tmp) / "a" / REPORT).read_bytes(), (Path(tmp) / "b" / REPORT).read_bytes())
            self.assertFalse((Path(tmp) / "b" / EVIDENCE).exists())  # a replay writes the report only

    def test_a_live_backtest_on_a_named_warehouse_is_refused_before_any_credential(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bridge.access, "resolve_cli_token") as token:
            code, out, err = self.run_cli("--profile", "p", "--sql-warehouse-id", "wh-demo-013", "--warehouse", "2234567890123456/wh-demo-013",
                                          "--output-dir", tmp)
            self.assertEqual((code, out), (EXIT_REFUSED, ""))
            self.assertIn("cannot run on the warehouse it measures", err)
            token.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_a_live_backtest_writes_its_evidence_and_does_not_measure_the_collector_warehouse(self):
        client = bridge.demo_fleet_client(backtest_collector.DEMO_CHANGES, backtest_collector.DEMO_ANALYSIS_TIME)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(backtest_collector.bridge, "resolve_live_access", return_value=object()) as access, \
                mock.patch.object(backtest_collector.bridge, "LiveSqlClient") as live:
            live.return_value.__enter__.return_value = client
            code, out, err = self.run_cli("--profile", "p", "--sql-warehouse-id", "wh-demo-013", "--as-of", backtest_collector.DEMO_ANALYSIS_TIME,
                                          "--source-region", "us-east-1", "--output-dir", tmp)
            self.assertEqual(code, 0, err)
            self.assertEqual(access.call_args.args[:3], ("p", "wh-demo-013", []))
            self.assertIn("wh-demo-013 2026-08-01T14:32:00Z auto-stop 30->10: skipped EXECUTION_WAREHOUSE", out)
            evidence = json.loads((Path(tmp) / EVIDENCE).read_text(encoding="utf-8"))
            self.assertEqual((evidence["source"], evidence["execution_warehouse_id"], evidence["source_region"]),
                             ("LIVE_SYSTEM_TABLES", "wh-demo-013", "us-east-1"))
            self.assertTrue((Path(tmp) / REPORT).is_file())

    def test_refusals_name_options_and_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"backtest_evidence_format_version": "0.1.0"}), encoding="utf-8")
            out_dir = str(Path(tmp) / "out")
            for argv, message in (
                ((), "--backtest needs one source"),
                (("--demo", "--from-evidence", str(bad)), "not --from-evidence and --demo"),
                (("--demo", "--package", "p.json"), "--backtest reads no package"),
                (("--demo", "--warehouse", "wh-1"), "--warehouse selects warehouses of a live backtest"),
                (("--profile", "p", "--sql-warehouse-id", "x", "--warehouse", "a/b/c"), "--warehouse must be"),
                (("--profile", "p"), "needs --sql-warehouse-id"),
                (("--from-evidence", str(bad)), "unsupported backtest evidence format"),
            ):
                with self.subTest(argv=argv):
                    code, out, err = self.run_cli(*argv, "--output-dir", out_dir)
                    self.assertEqual((code, out), (EXIT_REFUSED, ""))
                    self.assertTrue(err.splitlines()[-1].startswith("realizer: "), err)
                    self.assertIn(message, err)
                    self.assertNotIn("a/b/c", err)
            self.assertFalse(Path(out_dir).exists())

    def test_the_realize_options_are_unchanged_and_the_backtest_has_its_own(self):
        self.assertFalse(hasattr(cli_realize.parse_args(["--package", "p"]), "backtest"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_realize.parse_args([])  # a realize run still needs --package
        args = cli_realize.parse_backtest_args(["--backtest", "--demo", "--warehouse", "a", "--warehouse", "w/b"])
        self.assertEqual((args.demo, args.warehouse, args.package), (True, ["a", "w/b"], None))

    def test_the_facade_passes_the_backtest_entry_points_through(self):
        from realizer.analyzer import backtest
        self.assertIs(facade.backtest_live, backtest_collector.backtest_live)
        self.assertIs(facade.backtest_from_evidence, backtest_collector.backtest_from_evidence)
        self.assertIs(facade.backtest_demo, backtest_collector.backtest_demo)
        self.assertIs(facade.backtest_summary, backtest.summary_lines)


if __name__ == "__main__":
    unittest.main()
