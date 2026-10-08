"""Reporter (src/publisher/reporter) and the run manifest (src/publisher/packager/manifest.py)."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests.component.publisher.pipeline import demo_run  # noqa: E402
from publisher.contracts.review import PackageReviewResult  # noqa: E402
from publisher.packager.manifest import build_manifest  # noqa: E402
from publisher.reporter import reporter  # noqa: E402


class ReporterTests(unittest.TestCase):
    def run_reporter(self, tmp, *, workbook, count=3):
        context, analysis, governance, _, packaging = demo_run()
        context = replace(context, request=replace(context.request, output_dir=Path(tmp), generate_workbook=workbook))
        return reporter.run(context, analysis, governance, PackageReviewResult(packaging.packages[:count]))

    def test_writes_every_package_evidence_manifest_and_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_reporter(tmp, workbook=True)
            self.assertEqual(sum(1 for k in report.files if k.startswith("package:")), 3)
            for key in ("workbook", "manifest", "evidence"):
                self.assertTrue(Path(report.files[key]).is_file(), key)
            manifest = json.loads(Path(report.files["manifest"]).read_text())
            self.assertEqual(manifest["package_count"], 3)
            self.assertEqual(manifest["workbook_status"], "GENERATED")
            for p in report.packages:
                written = json.loads(Path(report.files[f"package:{p.warehouse.workspace_id}:{p.warehouse.warehouse_id}"]).read_text())
                self.assertEqual(written, p.to_dict())

    def test_skipped_workbook_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_reporter(tmp, workbook=False, count=1)
            self.assertNotIn("workbook", report.files)
            self.assertEqual(json.loads(Path(report.files["manifest"]).read_text())["workbook_status"], "SKIPPED")


class ManifestTests(unittest.TestCase):
    def test_unseen_check_states(self):
        packages = demo_run()[4].packages[:1]
        not_checked = build_manifest(packages, [], [], None, None, Path("evidence.json"))
        self.assertEqual((not_checked["unseen_billed_warehouse_check"], not_checked["unseen_billed_warehouse_count"]), ("NOT_CHECKED", None))
        checked = build_manifest(packages, [], [], [{"warehouse_id": "x"}], None, Path("evidence.json"))
        self.assertEqual((checked["unseen_billed_warehouse_check"], checked["unseen_billed_warehouse_count"]), ("CHECKED", 1))
        self.assertEqual(checked["generated_at"], packages[0].provenance.generated_at)


if __name__ == "__main__":
    unittest.main()
