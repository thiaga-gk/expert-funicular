"""Configuration (src/publisher/config): versions, defaults and repository-relative settings."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.config import defaults, settings, versions  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_package_version_matches_the_publisher_version(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1), versions.PUBLISHER_VERSION)
        self.assertEqual(versions.CONTRACT_VERSION, rc.CONTRACT_VERSION)

    def test_settings_point_at_the_committed_files(self):
        self.assertEqual(settings.REPO_ROOT, ROOT)
        self.assertTrue(settings._CONTRACT_SCHEMA_PATH.is_file())
        self.assertTrue(settings._EVIDENCE_SCHEMA_PATH.is_file())
        self.assertTrue((settings.REPO_ROOT / "pricing" / "aws_ec2_on_demand.json").is_file())

    def test_defaults_cover_every_size(self):
        self.assertEqual(len(defaults.SIZE_ORDER), 10)
        self.assertEqual(set(defaults.AWS_SQL_WAREHOUSE_TOPOLOGY), set(defaults.SIZE_ORDER))
        self.assertLessEqual(set(defaults.SQL_DBU_PER_CLUSTER_HOUR), set(defaults.SIZE_ORDER))
        self.assertEqual(len(defaults.ACTIVE_KNOBS), 5)


if __name__ == "__main__":
    unittest.main()
