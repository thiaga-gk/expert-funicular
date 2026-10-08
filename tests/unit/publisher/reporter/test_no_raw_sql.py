"""No raw SQL text leaves the run (FR-SRC-002, NFR-009): evidence.json, the packages, the manifest and publisher.log carry none."""
import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector import queries  # noqa: E402
from publisher.cli.main import main  # noqa: E402

FRAGMENTS = ("FROM system.", "SELECT ", "GROUP BY", "JOIN system.", "WHERE ", "ORDER BY", "PARTITION BY", "QUALIFY")


def statement_lines():
    """Distinctive lines of every SQL template the collector holds (module-level statements in the queries package)."""
    import importlib, pkgutil
    lines = set()
    for info in pkgutil.iter_modules(queries.__path__):
        module = importlib.import_module(f"{queries.__name__}.{info.name}")
        for value in vars(module).values():
            if isinstance(value, str) and re.search(r"\bSELECT\b", value):
                lines.update(line.strip() for line in value.splitlines() if len(line.strip()) >= 30)
    return lines


class NoRawSqlTests(unittest.TestCase):
    def test_a_demo_run_writes_no_sql_text_anywhere(self):
        templates = statement_lines()
        self.assertGreater(len(templates), 50)  # the scan itself is not vacuous
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--demo", "--demo-count", "6", "--flat-output", "--no-workbook", "--log-level", "DEBUG", "--output-dir", tmp])
            self.assertEqual(code, 0)
            files = [p for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertTrue({"evidence.json", "manifest.json", "publisher.log"} <= {p.name for p in files})
            self.assertTrue(list((Path(tmp) / "packages").glob("*.json")))
            for path in files:
                text = path.read_text(encoding="utf-8")
                for fragment in FRAGMENTS:
                    self.assertNotIn(fragment, text, f"{path.name} contains SQL fragment {fragment!r}")
                for line in templates:
                    self.assertNotIn(line, text, f"{path.name} contains a collector statement line")


if __name__ == "__main__":
    unittest.main()
