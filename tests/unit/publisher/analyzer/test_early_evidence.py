"""A live run saves evidence.json as soon as the collection is extracted, so a later refusal or crash keeps it replayable."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import load_evidence_files, save_evidence  # noqa: E402
from publisher.cli.main import main as cli_main  # noqa: E402

LIVE = ["--sql-warehouse-id", "abc", "--no-workbook", "--flat-output", "--log-level", "ERROR"]


@contextlib.contextmanager
def live_collection(evidence, unseen=None, unseen_error=None):
    """Patch the live collector edges: OAuth, workspace API, SQL client, evidence extraction and the unseen-billing query."""
    unseen_patch = patch("publisher.analyzer.analyzer.collect_unseen_billed_warehouses",
                         side_effect=unseen_error, return_value=unseen or [])
    with patch("publisher.analyzer.analyzer.resolve_cli_oauth", return_value=MagicMock()), \
            patch("publisher.analyzer.analyzer.collect_workspace_compatibility_evidence", return_value={"mode": "A_HITL_FALLBACK", "status": "OFF"}), \
            patch("publisher.analyzer.analyzer.SqlClient", MagicMock()), \
            patch("publisher.analyzer.analyzer.extract_evidence", return_value=evidence), unseen_patch:
        yield


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(argv)
    return code, err.getvalue()


class EarlyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.evidence = demo_evidence()  # one Pro warehouse

    def assertReplayable(self, tmp, err):
        saved = Path(tmp) / "evidence.json"
        self.assertTrue(saved.exists())
        self.assertEqual([e.warehouse_id for e in load_evidence_files([str(saved)])], [e.warehouse_id for e in self.evidence])
        self.assertIn(str(saved), err)
        self.assertIn(f"--from-evidence {saved}", err)

    def test_an_aws_region_refusal_keeps_the_collection(self):
        with tempfile.TemporaryDirectory() as tmp, live_collection(self.evidence):
            code, err = run_cli(["--output-dir", tmp, "--source-region", "unknown", *LIVE])
            self.assertEqual(code, 2)
            self.assertIn("--allow-missing-aws-price", err)
            self.assertFalse((Path(tmp) / "packages").exists())
            self.assertReplayable(tmp, err)

    def test_a_warehouse_typo_checked_after_collection_keeps_the_collection(self):
        with tempfile.TemporaryDirectory() as tmp, live_collection(self.evidence):
            code, err = run_cli(["--output-dir", tmp, "--source-region", "us-east-1", "--warehouse", "no-such-wh", *LIVE])
            self.assertEqual(code, 2)
            self.assertReplayable(tmp, err)

    def test_a_failure_after_extraction_inside_the_collector_keeps_the_collection(self):
        with tempfile.TemporaryDirectory() as tmp, live_collection(self.evidence, unseen_error=ValueError("boom")):
            with self.assertRaises(ValueError):
                run_cli(["--output-dir", tmp, "--source-region", "us-east-1", *LIVE])
            self.assertTrue((Path(tmp) / "evidence.json").exists())
            log = (Path(tmp) / "publisher.log").read_text()
            self.assertIn("--from-evidence", log)

    def test_a_failure_to_collect_leaves_no_evidence_and_no_misleading_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "evidence.json").write_text("stale")
            with patch("publisher.analyzer.analyzer.resolve_cli_oauth", side_effect=RuntimeError("no auth")):
                with self.assertRaises(RuntimeError):
                    run_cli(["--output-dir", tmp, "--source-region", "us-east-1", *LIVE])
            self.assertNotIn("--from-evidence", (Path(tmp) / "publisher.log").read_text())

    def test_the_final_evidence_file_is_byte_identical_to_the_saved_envelope(self):
        with tempfile.TemporaryDirectory() as tmp, live_collection(self.evidence):
            code, _ = run_cli(["--output-dir", tmp, "--source-region", "us-east-1", *LIVE])
            self.assertEqual(code, 0)
            expected = Path(tmp) / "expected.json"
            save_evidence(expected, self.evidence)
            self.assertEqual((Path(tmp) / "evidence.json").read_bytes(), expected.read_bytes())
            self.assertEqual(json.loads(expected.read_text())["evidence_format_version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
