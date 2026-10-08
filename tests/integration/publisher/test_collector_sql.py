"""Live collector check against a real SQL warehouse (repo-reorg-design.md section 13.5).

Skipped unless PUBLISHER_LIVE_PROFILE (a Databricks CLI OAuth profile) and PUBLISHER_LIVE_SQL_WAREHOUSE_ID are set;
PUBLISHER_LIVE_SOURCE_REGION defaults to us-east-1. The run is read-only: it executes the collector SQL, then checks
that the evidence it returns passes the committed evidence schema and analyzes without error.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.replay import save_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts.request import MODE_LIVE, PublisherRequest, RunContext  # noqa: E402

PROFILE = os.getenv("PUBLISHER_LIVE_PROFILE")
WAREHOUSE = os.getenv("PUBLISHER_LIVE_SQL_WAREHOUSE_ID")


@unittest.skipUnless(PROFILE and WAREHOUSE, "live collector check needs PUBLISHER_LIVE_PROFILE and PUBLISHER_LIVE_SQL_WAREHOUSE_ID")
class LiveCollectorTests(unittest.TestCase):
    def test_collected_evidence_matches_the_schema_and_analyzes(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = PublisherRequest(mode=MODE_LIVE, output_dir=Path(tmp), profile=PROFILE, execution_kind="warehouse",
                                       execution_id=WAREHOUSE, source_region=os.getenv("PUBLISHER_LIVE_SOURCE_REGION", "us-east-1"),
                                       aws_price_registry=str(ROOT / "pricing" / "aws_ec2_on_demand.json"))
            analysis = analyzer.run(RunContext(request, Thresholds()))
            save_evidence(Path(tmp) / "evidence.json", analysis.evidence)  # validates against schema/evidence.schema.json
        self.assertIsNotNone(analysis.unseen_billed_warehouses)
        self.assertLessEqual(len(analysis.warehouses), len(analysis.evidence))


if __name__ == "__main__":
    unittest.main()
