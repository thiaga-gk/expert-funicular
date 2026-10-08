import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.analyzer.collector.pricing import load_aws_price_registry  # noqa: E402
from publisher.governor.constraints import aws_region_price_guard  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.packager.scenarios import ScenarioPlanner  # noqa: E402
from publisher.cli.main import main as cli_main  # noqa: E402


class AwsRegionPriceGuardTests(unittest.TestCase):
    """Live runs refuse to publish Pro/Classic economics for a region the AWS price registry does not cover."""

    def setUp(self):
        self.evidence = demo_evidence()  # one Pro warehouse
        self.registry = load_aws_price_registry(str(ROOT / "pricing" / "aws_ec2_on_demand.json"))

    def _serverless_only(self):
        ev = copy.deepcopy(self.evidence)
        for e in ev:
            for ep in e.epochs:
                ep.config = dataclasses.replace(ep.config, warehouse_type="SERVERLESS")
        return ev

    def test_unknown_or_unregistered_region_with_pro_warehouse_is_refused(self):
        for region in ("unknown", "eu-west-1", ""):
            msg = aws_region_price_guard(self.evidence, region, self.registry, allow_missing=False)
            self.assertIsNotNone(msg, region)
            self.assertIn("--allow-missing-aws-price", msg)
            self.assertIn("1 Pro/Classic", msg)

    def test_registered_region_escape_flag_and_serverless_only_fleet_pass(self):
        self.assertIsNone(aws_region_price_guard(self.evidence, "us-east-1", self.registry, allow_missing=False))
        self.assertIsNone(aws_region_price_guard(self.evidence, "unknown", self.registry, allow_missing=True))
        self.assertIsNone(aws_region_price_guard(self._serverless_only(), "unknown", self.registry, allow_missing=False))

    def test_unsupported_types_do_not_trigger_the_guard(self):
        ev = copy.deepcopy(self.evidence)
        for ep in ev[0].epochs:
            ep.config = dataclasses.replace(ep.config, warehouse_type="LAKEHOUSE_REALTIME")
        self.assertIsNone(aws_region_price_guard(ev, "unknown", self.registry, allow_missing=False))

    def test_live_main_refuses_before_writing_packages(self):
        import contextlib, io
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
                patch("publisher.analyzer.analyzer.resolve_cli_oauth", return_value=MagicMock()), \
                patch("publisher.analyzer.analyzer.collect_workspace_compatibility_evidence", return_value={"mode": "A_HITL_FALLBACK", "status": "OFF"}), \
                patch("publisher.analyzer.analyzer.SqlClient", MagicMock()), \
                patch("publisher.analyzer.analyzer.extract_evidence", return_value=self.evidence):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(["--output-dir", tmp, "--flat-output", "--sql-warehouse-id", "abc", "--source-region", "unknown", "--no-workbook"])
            self.assertEqual(code, 2)
            self.assertIn("--allow-missing-aws-price", err.getvalue())
            self.assertFalse((Path(tmp) / "packages").exists())
            # The escape flag publishes with AWS cost unavailable.
            self.assertEqual(cli_main(["--output-dir", tmp, "--flat-output", "--sql-warehouse-id", "abc", "--source-region", "unknown", "--no-workbook", "--allow-missing-aws-price"]), 0)
            self.assertTrue(any((Path(tmp) / "packages").glob("*.json")))


class ServerlessRegionCheckTests(unittest.TestCase):
    """No verified per-region Serverless list exists: a region label alone never passes the check."""

    def _check(self, observed, region="us-east-1"):
        e = demo_evidence()[0]
        e.billing_features["workspace_serverless_observed_from_system_compute_warehouses"] = observed
        e = dataclasses.replace(e, source_region=region)
        planner = ScenarioPlanner(Thresholds())
        compat = planner._compatibility(e, "SERVERLESS")
        return next(c for c in compat["checks"] if c["check"] == "aws_region_serverless_availability")

    def test_region_label_alone_is_unknown(self):
        for region in ("us-east-1", "unknown", "ap-south-2"):
            self.assertEqual(self._check(False, region)["status"], "UNKNOWN", region)

    def test_existing_serverless_warehouse_in_the_workspace_proves_the_region(self):
        self.assertEqual(self._check(True)["status"], "PASS")


class EarlyRegionWarningTests(unittest.TestCase):
    """The AWS price-registry gap is announced before any SQL runs; the refusal itself still waits for the fleet."""

    def _log(self, region, extra=()):
        import contextlib, io
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("publisher.analyzer.analyzer.resolve_cli_oauth", side_effect=RuntimeError("stop before any SQL")):
            with self.assertRaises(RuntimeError):
                cli_main(["--output-dir", tmp, "--flat-output", "--sql-warehouse-id", "abc", "--source-region", region, *extra])
            return (Path(tmp) / "publisher.log").read_text()

    def test_an_unregistered_region_warns_before_collection(self):
        text = self._log("unknown")
        self.assertIn("WARNING aws.region_not_in_registry_early", text)
        self.assertIn("--allow-missing-aws-price", text)

    def test_the_opt_in_names_the_unavailable_cost(self):
        text = self._log("unknown", ("--allow-missing-aws-price",))
        self.assertIn("WARNING aws.region_not_in_registry_early", text)
        self.assertIn("unavailable", text)

    def test_a_registered_region_does_not_warn(self):
        self.assertNotIn("region_not_in_registry", self._log("us-east-1"))
