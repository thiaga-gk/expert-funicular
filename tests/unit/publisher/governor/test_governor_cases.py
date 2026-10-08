import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.workspace_api import AuthContext, collect_workspace_compatibility_evidence  # noqa: E402
from publisher.analyzer.findings import Analyzer  # noqa: E402
from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES  # noqa: E402
from publisher.contracts.source import WarehouseConfig  # noqa: E402
from publisher.reporter.json_reporter import package_locked_contract  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class GovernorCases(PublisherCase):
    def test_read_only_api_scope_and_supported_type_gate_are_explicit(self):
        self.assertEqual(SUPPORTED_WAREHOUSE_TYPES, {"CLASSIC", "PRO", "SERVERLESS"})
        source = "\n".join(path.read_text() for path in sorted(Path(p.__file__).parent.rglob("*.py")))
        self.assertIn("/api/2.0/sql/config/warehouses", source)
        self.assertIn("/api/2.1/clusters/get", source)
        self.assertNotIn("/api/2.0/sql/warehouses/{id}", source)

    def test_unsupported_type_is_excluded_per_warehouse_not_whole_run(self):
        # G1: one unsupported warehouse must not abort the run for every other warehouse.
        fleet = self._fleet_with_one_unsupported()
        packages = p.build_recommendation_packages(fleet, self.t)
        keys = {(pkg.warehouse.workspace_id, pkg.warehouse.warehouse_id) for pkg in packages}
        self.assertEqual(len(packages), 2)
        self.assertNotIn((fleet[1].workspace_id, fleet[1].warehouse_id), keys)
        supported_only = [e for i, e in enumerate(synthetic_demo_evidence(3)) if i != 1]
        expected = p.build_recommendation_packages(supported_only, self.t)
        self.assertEqual(
            sorted(pkg.recommendation_package_id for pkg in packages),
            sorted(pkg.recommendation_package_id for pkg in expected),
        )

    def test_unsupported_type_never_reaches_the_analyzer(self):
        seen = []
        original = Analyzer.analyze

        def spy(analyzer, e):
            seen.append(e.current_epoch.config.warehouse_type)
            return original(analyzer, e)

        with patch.object(Analyzer, "analyze", spy):
            p.evaluate_warehouses(self._fleet_with_one_unsupported(), self.t)
        self.assertTrue(seen)
        self.assertTrue(set(seen) <= SUPPORTED_WAREHOUSE_TYPES)

    def test_unsupported_warehouse_does_not_feed_workspace_calibration(self):
        fleet = synthetic_demo_evidence(2)
        unsupported = fleet[1]
        prior = copy.deepcopy(unsupported.current_epoch)
        prior.is_current = False
        prior.epoch_id = prior.epoch_id + "-prior"
        prior.config = WarehouseConfig("SERVERLESS", "CURRENT", "MEDIUM", 1, 1, 10)
        unsupported.epochs.insert(0, prior)
        unsupported.current_epoch.config = WarehouseConfig("FUTURE_TYPE", "CURRENT", "MEDIUM", 1, 1, 10)
        self.assertFalse(any(ep.config.warehouse_type == "SERVERLESS" for ep in fleet[0].epochs))
        p.evaluate_warehouses(fleet, self.t)
        self.assertFalse(fleet[0].billing_features["workspace_serverless_observed_from_system_compute_warehouses"])

    def test_unsupported_exclusion_recorded_in_manifest(self):
        fleet = self._fleet_with_one_unsupported()
        packages = p.build_recommendation_packages(fleet, self.t)
        with tempfile.TemporaryDirectory() as td:
            files = package_locked_contract(Path(td), packages, fleet, generate_workbook=False)
            manifest = json.loads(Path(files["manifest"]).read_text())
        self.assertEqual(manifest["package_count"], 2)
        self.assertEqual(manifest["excluded_warehouse_count"], 1)
        self.assertEqual(manifest["excluded_warehouses"], [{
            "workspace_id": fleet[1].workspace_id,
            "warehouse_id": fleet[1].warehouse_id,
            "warehouse_name": fleet[1].warehouse_name,
            "warehouse_type": "LAKEHOUSE_REAL_TIME",
            "reason_code": "UNSUPPORTED_WAREHOUSE_TYPE",
        }])

    def test_all_unsupported_fleet_emits_no_packages_and_records_every_exclusion(self):
        fleet = synthetic_demo_evidence(2)
        for e in fleet:
            e.current_epoch.config = WarehouseConfig("FUTURE_TYPE", "CURRENT", "MEDIUM", 1, 1, 10)
        packages = p.build_recommendation_packages(fleet, self.t)
        self.assertEqual(packages, [])
        with tempfile.TemporaryDirectory() as td:
            files = package_locked_contract(Path(td), packages, fleet, generate_workbook=False)
            manifest = json.loads(Path(files["manifest"]).read_text())
        self.assertEqual(manifest["package_count"], 0)
        self.assertEqual([x["warehouse_type"] for x in manifest["excluded_warehouses"]], ["FUTURE_TYPE", "FUTURE_TYPE"])

    def test_supported_manifest_has_empty_exclusions(self):
        with tempfile.TemporaryDirectory() as td:
            files = package_locked_contract(Path(td), self.packages, self.evidence, generate_workbook=False)
            manifest = json.loads(Path(files["manifest"]).read_text())
        self.assertEqual(manifest["excluded_warehouse_count"], 0)
        self.assertEqual(manifest["excluded_warehouses"], [])

    def test_compatibility_api_b_and_fallback_a(self):
        auth=AuthContext("WHOPT","dbc.example.cloud.databricks.com","/sql/1.0/warehouses/x","tok",None,"warehouse","x","123")
        payload={"enable_serverless_compute":True,"enabled_warehouse_types":[{"warehouse_type":"PRO","enabled":True}],"data_access_config":[],"security_policy":"NONE"}
        with patch("publisher.analyzer.collector.workspace_api._api_get", return_value=payload):
            ev=collect_workspace_compatibility_evidence(auth,"auto")
            self.assertEqual(ev["mode"],"B_READ_ONLY_API")
            self.assertTrue(ev["enable_serverless_compute"])
            self.assertFalse(ev["external_legacy_hive_metastore_detected"])
        with patch("publisher.analyzer.collector.workspace_api._api_get", side_effect=RuntimeError("denied")):
            ev=collect_workspace_compatibility_evidence(auth,"auto")
            self.assertEqual(ev["mode"],"A_HITL_FALLBACK")
