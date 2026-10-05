"""Governor (src/publisher/governor): eligibility, the live AWS price-registry constraint and the migration compatibility gate."""
import copy
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts.analysis import FleetAnalysis  # noqa: E402
from publisher.contracts.request import MODE_DEMO, MODE_LIVE, PublisherRequest, RunContext  # noqa: E402
from publisher.governor import governor  # noqa: E402
from publisher.governor.compatibility import migration_compatibility  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402

REGISTRY = {"regions": {"us-east-1": {}}}


def fleet_with_unsupported(n=4, index=1):
    fleet = synthetic_demo_evidence(n)
    for ep in fleet[index].epochs:
        ep.config = dataclasses.replace(ep.config, warehouse_type="LAKEHOUSE_REAL_TIME")
    return fleet


def analysis_of(fleet, registry=None):
    return FleetAnalysis(fleet, analyzer.analyze(fleet, Thresholds()), aws_price_registry=registry)


class EligibilityTests(unittest.TestCase):
    def test_unsupported_warehouses_are_excluded_one_entry_each(self):
        fleet = fleet_with_unsupported()
        result = governor.govern(analysis_of(fleet))
        self.assertEqual([x["warehouse_id"] for x in result.exclusions], [fleet[1].warehouse_id])
        self.assertEqual(result.exclusions[0]["reason_code"], "UNSUPPORTED_WAREHOUSE_TYPE")
        self.assertEqual(len(result.eligible), len(fleet) - 1)
        self.assertNotIn(fleet[1].warehouse_id, {w.evidence.warehouse_id for w in result.eligible})


class LiveConstraintTests(unittest.TestCase):
    def request(self, region, allow=False, mode=MODE_LIVE):
        return RunContext(PublisherRequest(mode=mode, output_dir=Path("."), source_region=region, allow_missing_aws_price=allow), Thresholds())

    def test_live_run_stops_when_its_region_has_no_aws_prices(self):
        analysis = analysis_of(synthetic_demo_evidence(4), REGISTRY)
        with self.assertRaises(PublisherError) as cm:
            governor.run(self.request("eu-west-9"), analysis)
        self.assertIn("--allow-missing-aws-price", str(cm.exception))
        self.assertEqual(len(governor.run(self.request("us-east-1"), analysis).eligible), 4)

    def test_escape_flag_publishes_and_logs(self):
        analysis = analysis_of(synthetic_demo_evidence(4), REGISTRY)
        with self.assertLogs("publisher", "WARNING") as logs:
            result = governor.run(self.request("eu-west-9", allow=True), analysis)
        self.assertEqual(len(result.eligible), 4)
        self.assertIn("aws.region_not_in_registry", "\n".join(logs.output))

    def test_demo_and_replay_skip_the_live_constraint(self):
        analysis = analysis_of(synthetic_demo_evidence(4))
        self.assertEqual(len(governor.run(self.request("eu-west-9", mode=MODE_DEMO), analysis).eligible), 4)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.e = copy.deepcopy(next(x for x in synthetic_demo_evidence() if x.current_epoch.config.warehouse_type == "PRO"))

    def test_api_evidence_that_serverless_is_disabled_fails_the_gate(self):
        self.e.compatibility_evidence = {"mode": "B_READ_ONLY_API", "status": "AVAILABLE", "enable_serverless_compute": False}
        result = migration_compatibility(self.e, "serverless")
        self.assertEqual(result["target_warehouse_type"], "SERVERLESS")
        self.assertEqual(result["assessment"], "FAIL")

    def test_without_api_evidence_the_gate_needs_review(self):
        self.e.compatibility_evidence = {}
        self.e.billing_features["workspace_serverless_observed_from_system_compute_warehouses"] = False
        result = migration_compatibility(self.e, "SERVERLESS")
        self.assertEqual(result["evidence_mode"], "A_HITL_FALLBACK")
        self.assertEqual(result["assessment"], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
