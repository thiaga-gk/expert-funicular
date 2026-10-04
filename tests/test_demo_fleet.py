"""The --demo fleet: one simulated warehouse per golden scenario (publisher.DEMO_FLEET).

Checks that the fleet is physically consistent (billing follows simulated cluster-hours and list prices), that every
warehouse names a real golden scenario row, and that warehouses whose golden row passes behave as that row expects.
Rows that still fail are in the fleet so the demo shows today's behavior; they gain assertions here as their fixes land.
"""
import logging
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

GOLDEN_TABLE = ROOT / "docs" / "proposals" / "golden-scenarios.md"
CAPACITY_CUT = {"warehouse_size", "max_clusters", "min_clusters"}


class DemoFleetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        cls.evidence = p.synthetic_demo_evidence()
        cls.packages = [pkg.to_dict() for pkg in p.build_recommendation_packages(cls.evidence, cls.t)]
        cls.by_scenario = {}
        for e, pkg in zip(sorted(cls.evidence, key=lambda x: (x.workspace_id, x.warehouse_id)),
                          sorted(cls.packages, key=lambda x: (x["warehouse"]["workspace_id"], x["warehouse"]["warehouse_id"]))):
            cls.by_scenario[e.current_epoch.tags["golden_scenario"]] = (e, pkg)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def candidate(self, scenario, knob):
        return next(c for c in self.by_scenario[scenario][1]["candidates"] if c["knob"] == knob)

    # --- Fleet shape -------------------------------------------------------------------------------------------
    def test_every_warehouse_is_one_golden_scenario(self):
        rows = set(re.findall(r"^\| ([CA]\d{2}) \|", GOLDEN_TABLE.read_text(), re.M))
        scenarios = [s["scenario"] for s in p.DEMO_FLEET]
        self.assertEqual(len(scenarios), len(set(scenarios)), "one warehouse per scenario")
        self.assertTrue(set(scenarios) <= rows, sorted(set(scenarios) - rows))
        self.assertEqual(len(self.packages), len(p.DEMO_FLEET))
        self.assertEqual(set(self.by_scenario), set(scenarios))

    def test_fleet_is_deterministic(self):
        again = [pkg.to_dict() for pkg in p.build_recommendation_packages(p.synthetic_demo_evidence(), self.t)]
        self.assertEqual(sorted(x["recommendation_package_id"] for x in again), sorted(x["recommendation_package_id"] for x in self.packages))

    # --- Physical consistency ----------------------------------------------------------------------------------
    def test_billing_follows_simulated_cluster_hours_and_list_prices(self):
        for scenario, (e, _) in self.by_scenario.items():
            ep = e.current_epoch
            rate = p.SQL_DBU_PER_CLUSTER_HOUR[ep.config.warehouse_size]
            price = p.DEMO_SKU_PRICES[p.DEMO_SKU_BY_TYPE[ep.config.warehouse_type]]
            billed = {r["usage_date"]: r for r in e.daily_cost_365 if r["epoch_start"][:10] == ep.epoch_start[:10]}
            with self.subTest(scenario=scenario):
                checked = 0
                for day in ep.resource_daily or []:
                    row = billed.get(day["usage_day"])
                    if row is None or day["cluster_minutes"] <= 0 or row["retracted_dbu_quantity"]:
                        continue
                    expected = day["cluster_minutes"] / 60.0 * rate
                    self.assertLessEqual(abs(row["dbu_quantity"] / expected - 1.0), 0.0301, day["usage_day"])
                    if row["list_cost"] is not None:
                        self.assertAlmostEqual(row["list_cost"], row["dbu_quantity"] * price, places=4)
                    checked += 1
                if scenario != "C52":  # month-end only: may have no billed day in the 120-day series besides a few
                    self.assertGreater(checked, 0)

    def test_running_time_never_exceeds_wall_clock(self):
        for scenario, (e, _) in self.by_scenario.items():
            for day in e.current_epoch.resource_daily or []:
                self.assertLessEqual(day["uptime_minutes"], 1440.0 + 1e-6, (scenario, day["usage_day"]))
                self.assertGreaterEqual(day["cluster_minutes"] + 1e-6, day["uptime_minutes"] * e.current_epoch.config.min_clusters * (day["uptime_minutes"] > 0), scenario)

    def test_workspace_size_rates_rise_with_size(self):
        # Billed DBU per cluster-hour follows the published size table, so observed rates rise with size.
        for e in self.evidence:
            rates = e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
            ordered = [rates[s] for s in p.SIZE_ORDER if s in rates]
            self.assertEqual(ordered, sorted(ordered), e.workspace_id)

    # --- Golden rows that pass ---------------------------------------------------------------------------------
    def test_c19_newly_changed_config_waits_with_a_reevaluation_time(self):
        _, pkg = self.by_scenario["C19"]
        self.assertEqual(pkg["assessment"]["overall_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("CURRENT_CONFIG_EPOCH_TOO_NEW", self.candidate("C19", "warehouse_size")["blockers"])
        self.assertIn("earliest_reevaluation_time", str(pkg))
        self.assertFalse([c for c in pkg["candidates"] if c["status"] in {"ACTIONABLE", "TEST_REQUIRED"}])

    def test_c20_sparse_new_warehouse_abstains_with_unknown_cost(self):
        _, pkg = self.by_scenario["C20"]
        self.assertIsNone(pkg["cost_baseline"]["forward_365d"]["dbu_cost"])
        self.assertFalse([c for c in pkg["candidates"] if c["status"] in {"ACTIONABLE", "TEST_REQUIRED"}])

    def test_c26_failure_heavy_warehouse_gets_no_capacity_cut(self):
        _, pkg = self.by_scenario["C26"]
        for c in pkg["candidates"]:
            if c["knob"] not in CAPACITY_CUT or c["status"] not in {"ACTIONABLE", "TEST_REQUIRED"}:
                continue
            if c["knob"] == "warehouse_size":
                self.assertGreater(p.SIZE_ORDER.index(c["recommended_value"]), p.SIZE_ORDER.index(c["current_value"]))
            else:
                self.assertGreaterEqual(c["recommended_value"], c["current_value"], c["knob"])
        self.assertIn("FAILURE_CANCEL_RATE_HIGH", self.candidate("C26", "warehouse_size")["blockers"])

    def test_c30_billing_correction_share_is_reported(self):
        _, pkg = self.by_scenario["C30"]
        share = [x["value"] for x in pkg["evidence"].values() if x["metric"] == "billing_correction_dbu_share_365d"]
        self.assertEqual(len(share), 1)
        self.assertGreater(share[0], 0.0)

    def test_c32_unpriced_sku_cost_is_unknown_not_zero(self):
        _, pkg = self.by_scenario["C32"]
        self.assertIsNone(pkg["cost_baseline"]["actual_365d"]["dbu_cost"])
        self.assertIsNone(pkg["cost_baseline"]["forward_365d"]["dbu_cost"])

    def test_c33_photon_is_observed_never_recommended(self):
        e, pkg = self.by_scenario["C06"]  # the over-sized warehouse runs without Photon
        self.assertEqual(e.photon_observation.status, "DISABLED")
        self.assertFalse([c for c in pkg["candidates"] if "photon" in c["knob"]])

    def test_c41_genie_warehouse_is_never_moved_to_classic(self):
        self.assertNotEqual(self.candidate("C41", "warehouse_type")["recommended_value"], "CLASSIC")

    def test_c48_long_unchanged_warehouse_is_analyzed(self):
        e, pkg = self.by_scenario["C48"]
        self.assertLess(e.current_epoch.epoch_start, "2025-01-01")
        self.assertEqual(len(pkg["candidates"]), 6)

    def test_a01_serverless_auto_stop_below_five_is_left_alone(self):
        c = self.candidate("A01", "auto_stop_minutes")
        self.assertEqual((c["current_value"], c["status"]), (1, "NO_CHANGE"))

    def test_a02_pro_classic_auto_stop_target_is_never_below_ten(self):
        for scenario, (e, _) in self.by_scenario.items():
            if e.current_epoch.config.warehouse_type in {"PRO", "CLASSIC"}:
                c = self.candidate(scenario, "auto_stop_minutes")
                if c["recommended_value"] != c["current_value"]:
                    self.assertGreaterEqual(c["recommended_value"], 10, scenario)

    def test_a04_cluster_targets_stay_within_limits(self):
        for scenario in self.by_scenario:
            self.assertLessEqual(self.candidate(scenario, "max_clusters")["recommended_value"], 40, scenario)
            self.assertLessEqual(self.candidate(scenario, "min_clusters")["recommended_value"], 30, scenario)


if __name__ == "__main__":
    unittest.main()
