"""Fail closed end to end (FR-REV-003, NFR-012): a package that fails the semantic check or the schema is never published."""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
import jsonschema  # noqa: E402
from publisher import publisher as facade  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402
from publisher.contracts.request import MODE_DEMO, PublisherRequest  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.packager import packager  # noqa: E402
from publisher.reporter.json_reporter import package_locked_contract  # noqa: E402

REAL_PACKAGER_RUN = packager.run


def demo_request(out: Path) -> PublisherRequest:
    return PublisherRequest(mode=MODE_DEMO, output_dir=out, demo_count=1, generate_workbook=False)


def nothing_published(testcase: unittest.TestCase, out: Path) -> None:
    testcase.assertEqual(list((out / "packages").glob("*.json")) if (out / "packages").exists() else [], [])
    for name in ("manifest.json", "workbook.xlsx", "evidence.json"):
        testcase.assertFalse((out / name).exists(), name)


class FailClosedTests(unittest.TestCase):
    def test_a_package_that_fails_the_semantic_check_aborts_the_run_before_any_file(self):
        def tampered_run(*args, **kwargs):
            result = REAL_PACKAGER_RUN(*args, **kwargs)
            bad = dataclasses.replace(result.packages[0], recommendation_package_id="PKG-" + "0" * 24)  # ID no longer matches the content
            return dataclasses.replace(result, packages=[bad, *result.packages[1:]])

        with tempfile.TemporaryDirectory() as tmp, patch.object(facade.packager, "run", side_effect=tampered_run):
            with self.assertRaises(ValueError):
                facade.publish(demo_request(Path(tmp)))
            nothing_published(self, Path(tmp))

    def test_a_package_that_fails_the_schema_aborts_the_run_before_any_file(self):
        real_to_dict = rc.RecommendationPackage.to_dict

        def without_economics(self_):
            payload = real_to_dict(self_)
            payload.pop("package_economics", None)  # still a semantically valid dataclass; only the serialized form breaks the schema
            return payload

        with tempfile.TemporaryDirectory() as tmp, patch.object(rc.RecommendationPackage, "to_dict", without_economics):
            with self.assertRaises(jsonschema.ValidationError):
                facade.publish(demo_request(Path(tmp)))
            nothing_published(self, Path(tmp))

    def test_the_writer_checks_the_schema_again_before_each_package_file(self):
        packages = facade.build_recommendation_packages(demo_evidence(), Thresholds())
        real_to_dict = rc.RecommendationPackage.to_dict

        def without_economics(self_):
            payload = real_to_dict(self_)
            payload.pop("package_economics", None)
            return payload

        with tempfile.TemporaryDirectory() as tmp, patch.object(rc.RecommendationPackage, "to_dict", without_economics):
            with self.assertRaises(jsonschema.ValidationError):
                package_locked_contract(Path(tmp), packages, [], generate_workbook=False)
            self.assertEqual(list((Path(tmp) / "packages").glob("*.json")), [])
            self.assertFalse((Path(tmp) / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
