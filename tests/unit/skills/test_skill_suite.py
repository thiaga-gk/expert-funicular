"""The reusable skill suite (.claude/skills) is covered by `unittest discover`: version guard and the suite validator."""
import contextlib
import importlib.util
import io
import sys
import unittest
import unittest.mock
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from publisher.config.versions import PUBLISHER_VERSION  # noqa: E402

SUITE = ROOT / ".claude" / "skills" / "suite.yaml"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_skill_suite", ROOT / "scripts" / "validate_skill_suite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkillSuiteTests(unittest.TestCase):
    def test_reference_exemplar_version_is_the_publisher_version(self):
        manifest = yaml.safe_load(SUITE.read_text(encoding="utf-8"))
        self.assertEqual(str(manifest["reference_exemplar"]["publisher_version"]), PUBLISHER_VERSION,
                         "suite.yaml reference_exemplar.publisher_version must follow src/publisher/config/versions.py")

    def test_suite_validator_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = _load_validator().main()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("skill suite: OK", out.getvalue())

    def test_suite_validator_fails_on_a_wrong_manifest_entrypoint(self):
        # The validator is not a rubber stamp: point it at a copy of the manifest with a different entrypoint.
        validator = _load_validator()
        original = yaml.safe_load

        def tampered(text, *args, **kwargs):
            data = original(text, *args, **kwargs)
            if isinstance(data, dict) and data.get("name") == "databricks-publisher-skill-suite":
                data = dict(data, entrypoint="not-the-orchestrator")
            return data

        out = io.StringIO()
        with unittest.mock.patch.object(yaml, "safe_load", tampered), contextlib.redirect_stdout(out):
            code = validator.main()
        self.assertEqual(code, 1)
        self.assertIn("entrypoint", out.getvalue())


if __name__ == "__main__":
    unittest.main()
