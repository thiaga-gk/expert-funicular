import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.common.logging import LOGGER, setup_logging  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class CommonCases(PublisherCase):
    def test_logging_file_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            log=setup_logging(Path(td),"INFO")
            LOGGER.info("test.event value=%s","ok")
            self.assertTrue(log.exists())
            self.assertIn("test.event",log.read_text())
