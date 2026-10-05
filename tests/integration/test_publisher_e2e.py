"""Live end-to-end run, then replay of its own evidence (repo-reorg-design.md section 13.5).

Skipped unless PUBLISHER_LIVE_PROFILE and PUBLISHER_LIVE_SQL_WAREHOUSE_ID are set (PUBLISHER_LIVE_SOURCE_REGION defaults
to us-east-1). Runs ``publish`` against the live system tables, then replays the evidence.json it wrote through the
same pipeline: the replayed packages must equal the live ones (system-run output equals fixture-driven local output).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.contracts.request import MODE_LIVE, MODE_REPLAY, PublisherRequest  # noqa: E402
from publisher.publisher import publish  # noqa: E402

PROFILE = os.getenv("PUBLISHER_LIVE_PROFILE")
WAREHOUSE = os.getenv("PUBLISHER_LIVE_SQL_WAREHOUSE_ID")


@unittest.skipUnless(PROFILE and WAREHOUSE, "needs PUBLISHER_LIVE_PROFILE and PUBLISHER_LIVE_SQL_WAREHOUSE_ID")
class LivePublisherTests(unittest.TestCase):
    def test_live_run_and_its_replay_publish_the_same_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_dir, replay_dir = Path(tmp) / "live", Path(tmp) / "replay"
            live = publish(PublisherRequest(mode=MODE_LIVE, output_dir=live_dir, profile=PROFILE, execution_kind="warehouse",
                                            execution_id=WAREHOUSE, source_region=os.getenv("PUBLISHER_LIVE_SOURCE_REGION", "us-east-1"),
                                            aws_price_registry=str(ROOT / "pricing" / "aws_ec2_on_demand.json"), generate_workbook=False))
            replay = publish(PublisherRequest(mode=MODE_REPLAY, output_dir=replay_dir, evidence_files=(str(live_dir / "evidence.json"),),
                                              generate_workbook=False))
            self.assertEqual([p.to_dict() for p in replay.packages], [p.to_dict() for p in live.packages])
            self.assertTrue(json.loads((live_dir / "manifest.json").read_text())["unseen_billed_warehouse_check"] == "CHECKED")


if __name__ == "__main__":
    unittest.main()
