"""Billed node types guard the modeled AWS topology; billed SQL tier is collected as evidence.

system.billing.usage.usage_metadata.node_type is populated for all non-serverless SQL warehouses (it does not say
which node is the driver). AWS cost assumes a driver and worker instance per size: billed node types outside that pair
mean the topology is wrong for this warehouse, so AWS cost is unavailable rather than mispriced. product_features.sql_tier
(CLASSIC, PRO or null) is reported beside the configured type; no decision uses it yet.
"""
import copy
import json
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import DEMO_ANALYSIS_TIME, _demo_fleet_rows, demo_evidence, replay_demo_rows  # noqa: E402
from publisher.analyzer.collector.pricing import model_aws_baseline_cost  # noqa: E402
from publisher.analyzer.collector.queries.billing import EPOCH_BILLING_ATTRIBUTION_SQL  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402

REGISTRY = json.loads((ROOT / "pricing" / "aws_ec2_on_demand.json").read_text())


class BillingTopologyTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Thresholds()
        self.e = copy.deepcopy(demo_evidence()[0])  # PRO LARGE: driver i3.8xlarge, workers i3.2xlarge

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def aws(self, node_types):
        return model_aws_baseline_cost(self.e.current_epoch, "us-east-1", self.e.analysis_time, self.e.forecast_metadata, REGISTRY, self.t, observed_node_types=node_types)

    def test_attribution_sql_collects_node_types_and_sql_tier(self):
        sql = EPOCH_BILLING_ATTRIBUTION_SQL
        self.assertIn("usage_metadata.node_type AS node_type", sql)
        self.assertIn("product_features.sql_tier AS sql_tier", sql)
        self.assertIn("ARRAY_SORT(COLLECT_SET(u.node_type)) AS observed_node_types", sql)
        self.assertIn("ARRAY_SORT(COLLECT_SET(u.sql_tier)) AS observed_sql_tiers", sql)

    def test_matching_node_types_verify_the_topology(self):
        cost, meta = self.aws(["i3.2xlarge", "i3.8xlarge"])
        self.assertIsNotNone(cost)
        self.assertEqual(meta["node_type_check"], "VERIFIED")

    def test_unreported_node_types_leave_the_topology_unverified(self):
        cost, meta = self.aws(None)
        self.assertIsNotNone(cost)
        self.assertEqual(meta["node_type_check"], "UNVERIFIED")

    def test_mismatched_node_types_make_aws_cost_unavailable(self):
        cost, meta = self.aws(["r5d.4xlarge"])
        self.assertIsNone(cost)
        self.assertEqual(meta["blocker"], "AWS_NODE_TYPE_MISMATCH")
        self.assertEqual(meta["observed_node_types"], ["r5d.4xlarge"])

    def test_collector_wires_node_types_and_sql_tier(self):
        rows = _demo_fleet_rows(1, DEMO_ANALYSIS_TIME)  # Pro MEDIUM
        rows = copy.deepcopy(rows)
        for r in rows["billing_attribution_photon_owner"]:
            r["observed_node_types"] = ["m5.large"]
            r["observed_sql_tiers"] = ["CLASSIC"]

        class Client:
            def query(self, statement, *, label="collector_sql"):
                return replay_demo_rows(rows, statement, label)

        e = extract_evidence(Client(), self.t, "us-east-1", REGISTRY, analysis_time=DEMO_ANALYSIS_TIME)[0]
        self.assertIsNone(e.aws_baseline_cost_365)
        self.assertEqual(e.aws_cost_model["blocker"], "AWS_NODE_TYPE_MISMATCH")
        self.assertEqual(e.billing_features["current_epoch_sql_tiers"], ["CLASSIC"])
        wt = {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["warehouse_type"]
        self.assertEqual({f.metric: f.value for f in wt.findings}["billed_sql_tiers"], ["CLASSIC"])


if __name__ == "__main__":
    unittest.main()
