import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import publisher as p
from contracts import workbook_contract as wc
from reporters import workbook_reporter as wr


class WorkbookContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t=p.Thresholds()
        cls.evidence=p.synthetic_demo_evidence(15)
        cls.packages=p.build_recommendation_packages(cls.evidence,cls.t)
        cls.projection=wc.project_workbook(cls.packages)

    def test_four_locked_sheet_grains(self):
        self.assertEqual(wc.SHEET_NAMES,("Executive Summary","Warehouse Portfolio","Scenario Plans","Recommendation Registry"))
        self.assertEqual(len(self.packages),15)
        self.assertEqual(len(self.projection.warehouse_portfolio),15)
        self.assertEqual(len(self.projection.scenario_plans),30)
        self.assertEqual(len(self.projection.recommendation_registry),105)

    def test_workbook_rows_trace_back_to_locked_ids(self):
        self.assertEqual({x["recommendation_package_id"] for x in self.projection.warehouse_portfolio},{p.recommendation_package_id for p in self.packages})
        self.assertTrue(all(x["recommendation_package_id"] for x in self.projection.scenario_plans))
        self.assertTrue(all(x["recommendation_package_id"] and x["candidate_id"] and x["technique_id"] for x in self.projection.recommendation_registry))

    def test_candidate_registry_has_hitl_evidence_and_nonadditive_economics(self):
        self.assertTrue(all(x["evidence_json"] and x["evidence_refs_json"] for x in self.projection.recommendation_registry))
        self.assertTrue(all(x["is_additive"] is False for x in self.projection.recommendation_registry))

    def test_portfolio_matches_exact_locked_columns_and_package_economics(self):
        row=self.projection.warehouse_portfolio[0]
        self.assertEqual(set(row),{c.key for c in wc.PORTFOLIO_COLUMNS})
        for key in ("candidate_view","recommended_execution_plan","package_low_impact","package_base_impact","package_high_impact","package_cost_low_365d","package_cost_base_365d","package_cost_high_365d","active_days_30d","query_source_profile_json","scenario_ids_json","candidate_ids_json"):
            self.assertIn(key,row)
        self.assertIn("Photon",row["candidate_view"])

    def test_scenario_and_registry_match_exact_locked_columns(self):
        self.assertEqual(set(self.projection.scenario_plans[0]),{c.key for c in wc.SCENARIO_COLUMNS})
        self.assertEqual(set(self.projection.recommendation_registry[0]),{c.key for c in wc.REGISTRY_COLUMNS})
        for key in ("dbu_low_impact","dbu_base_impact","dbu_high_impact","aws_low_impact","aws_base_impact","aws_high_impact","total_low_impact","total_base_impact","total_high_impact","estimated_total_cost_low","estimated_total_cost_base","estimated_total_cost_high"):
            self.assertIn(key,self.projection.scenario_plans[0])
            self.assertIn(key,self.projection.recommendation_registry[0])

    def test_presentation_has_no_blank_semantic_cells_and_impacts_are_numeric(self):
        collections=(
            self.projection.executive_metrics, self.projection.executive_opportunities,
            self.projection.warehouse_portfolio, self.projection.scenario_plans, self.projection.recommendation_registry,
        )
        for rows in collections:
            for row in rows:
                self.assertTrue(all(v is not None and (not isinstance(v,str) or v != "") for v in row.values()))
        impact_keys = [c.key for c in wc.PORTFOLIO_COLUMNS + wc.SCENARIO_COLUMNS + wc.REGISTRY_COLUMNS if "impact" in c.key and "type" not in c.key and c.number_format]
        for rows in (self.projection.warehouse_portfolio,self.projection.scenario_plans,self.projection.recommendation_registry):
            for row in rows:
                for key in impact_keys:
                    if key in row and row[key] != wc.DISPLAY_UNAVAILABLE:
                        self.assertIsInstance(row[key], (int,float), key)
                        self.assertNotIsInstance(row[key], bool, key)
        for rows in collections:
            for row in rows:
                for value in row.values():
                    if isinstance(value,str):
                        self.assertFalse(value.startswith("Save USD"))
                        self.assertFalse(value.startswith("Cost +USD"))

    def test_identifier_columns_are_text_formatted(self):
        for columns in (wc.PORTFOLIO_COLUMNS,wc.SCENARIO_COLUMNS,wc.REGISTRY_COLUMNS):
            for spec in columns:
                if spec.key in {"recommendation_package_id","workspace_id","warehouse_id","current_epoch_id","recommended_scenario_id","scenario_id","candidate_id","technique_id","epoch_id"}:
                    self.assertEqual(spec.number_format,"@",spec.key)

    def test_workbook_contract_column_counts_and_long_identifier_rendering(self):
        self.assertEqual((len(wc.PORTFOLIO_COLUMNS),len(wc.SCENARIO_COLUMNS),len(wc.REGISTRY_COLUMNS)),(58,42,53))
        self.assertEqual(wr._excel_text("1234567890123456"), "'1234567890123456")
        self.assertEqual(wr._excel_text("wh-synth-001"), "wh-synth-001")

    def test_package_writer_places_workbook_in_output_dir(self):
        with tempfile.TemporaryDirectory() as td:
            files=p.package_locked_contract(Path(td),self.packages[:2],self.evidence[:2])
            self.assertEqual(Path(files["workbook"]).name,"workbook.xlsx")
            self.assertTrue(Path(files["workbook"]).exists())
            manifest=json.loads(Path(files["manifest"]).read_text())
            self.assertEqual(manifest["workbook_contract_version"],"1.1.0")
            self.assertEqual(manifest["workbook_status"],"GENERATED")


if __name__=="__main__":
    unittest.main()
