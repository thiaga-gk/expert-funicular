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
        p._DEMO_ROWS_CACHE.clear()  # re-simulate, not just re-read the memo
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
                if scenario not in {"C03", "C52"}:  # C03 has no retained event; C52 bills a few month-end days only
                    self.assertGreater(checked, 0)

    def test_billing_matches_warehouse_events_in_every_epoch(self):
        analysis_day = p.DEMO_ANALYSIS_TIME[:10]
        for scenario, (e, _) in self.by_scenario.items():
            for ep in e.epochs:
                rows = [r for r in e.daily_cost_365 if r["epoch_start"] == ep.epoch_start]
                self.assertFalse([r for r in rows if r["usage_date"] >= analysis_day], scenario)  # today is never billed
                days = ep.cost["cost_days"]
                if days < 30 or not ep.events.get("cluster_minutes"):
                    continue  # short epochs: events include the unbilled part of today
                # Events cover the time after the warehouse's first retained event; compare billing over the same days.
                covered_from = ep.events["event_coverage_start"][:10]
                expected = ep.events["cluster_minutes"] / 60.0 * p.SQL_DBU_PER_CLUSTER_HOUR[ep.config.warehouse_size]
                billed = sum(r["dbu_quantity"] + r["retracted_dbu_quantity"] - r["restated_dbu_quantity"] for r in rows if r["usage_date"] >= covered_from)
                with self.subTest(scenario=scenario, epoch=ep.epoch_start):
                    self.assertLess(abs(billed / expected - 1.0), 0.035)

    def test_each_warehouse_shows_its_golden_signature(self):
        wall = 365 * 1440.0

        def always_up(v):
            # Running the whole time events can see: from the first retained event to the analysis time.
            covered = (p.parse_datetime(p.DEMO_ANALYSIS_TIME) - p.parse_datetime(v["event_coverage_start"])).total_seconds() / 60.0
            return v["uptime_minutes"] > 0.99 * covered

        def last30_uptime(e):
            return sum(d["uptime_minutes"] for d in (e.current_epoch.resource_daily or [])[-30:])

        def at_max_ever_above_recent(e):
            recent = max((d["max_cluster_count"] for d in (e.current_epoch.resource_daily or [])[-30:]), default=0)
            return e.current_epoch.events["max_observed_clusters"] > recent

        checks = {
            "C01": lambda e, c, q, v: c.auto_stop_minutes == 0 and v["starts_30"] <= 1 and always_up(v) and q["queries_30"] > 0,
            "C02": lambda e, c, q, v: c.warehouse_type == "SERVERLESS" and v["starts_30"] >= 60,
            # Running for over a year with no restart: no retained warehouse event, so running time is unknown (not idle).
            "C03": lambda e, c, q, v: c.auto_stop_minutes > 0 and q["queries_30"] == 0 and "uptime_minutes" not in v
                   and e.current_epoch.resource_daily is None and len(e.daily_cost_365) == 365,
            "C04": lambda e, c, q, v: e.current_epoch.gaps["p90_gap_minutes"] < c.auto_stop_minutes and always_up(v),
            "C05": lambda e, c, q, v: q["job_queries_30"] >= 0.9 * q["queries_30"] and v["uptime_minutes"] < 0.2 * wall,
            "C06": lambda e, c, q, v: q["spill_query_pct_30"] == 0 and v["time_at_min_pct"] == 100 and c.max_clusters > 1,
            "C07": lambda e, c, q, v: q["spill_query_pct_30"] >= 20,
            "C08": lambda e, c, q, v: v["time_at_max_pct"] > 80 and q["queued_query_pct_30"] > 5,
            "C09": lambda e, c, q, v: v["max_observed_clusters"] * 3 <= c.max_clusters,
            "C10": lambda e, c, q, v: c.warehouse_type == "SERVERLESS" and c.min_clusters > 1 and v["time_at_min_pct"] >= 90,
            "C11": lambda e, c, q, v: c.min_clusters == c.max_clusters > 1,
            "C12": lambda e, c, q, v: v["scale_ups_30"] > v["starts_30"] and q["queued_query_pct_30"] > 0,
            "C13": lambda e, c, q, v: v["starts_30"] >= 0.9 * q["queries_30"] and q["p95_wait_compute_ms_when_waited_30"] >= 60000,
            "C14": lambda e, c, q, v: c.warehouse_type == "SERVERLESS" and c.auto_stop_minutes > 5,
            "C16": lambda e, c, q, v: c.warehouse_type == "SERVERLESS" and always_up(v),
            "C18": lambda e, c, q, v: c.warehouse_channel == "PREVIEW",
            "C21": lambda e, c, q, v: q["result_cache_pct_30"] >= 50,
            "C22": lambda e, c, q, v: q["spill_query_pct_30"] <= 5 and q["p95_spill_bytes_when_spilled_30"] >= 4 * p.GIB,
            "C25": lambda e, c, q, v: c.auto_stop_minutes == 0 and q["active_days_30"] <= 23,
            "C26": lambda e, c, q, v: q["failure_cancel_pct_30"] >= 10,
            "C30": lambda e, c, q, v: any(r["retracted_dbu_quantity"] > 0 for r in e.daily_cost_365),
            "C32": lambda e, c, q, v: any(r["unpriced_dbu_quantity"] > 0 for r in e.daily_cost_365),
            "C37": lambda e, c, q, v: at_max_ever_above_recent(e),
            "C38": lambda e, c, q, v: q["queries_30"] > 0.3 * q["epoch_queries"],
            "C39": lambda e, c, q, v: e.current_epoch.tags["environment"] == "nonprod",
            "C41": lambda e, c, q, v: q["genie_queries_30"] >= 0.9 * q["queries_30"],
            "C42": lambda e, c, q, v: q["queued_query_pct_30"] >= 50,
            "C45": lambda e, c, q, v: c.min_clusters == 2 and v["time_at_min_pct"] >= 99 and always_up(v),
            "C46": lambda e, c, q, v: q["queued_query_pct_30"] >= 30 and v["time_at_max_pct"] < 5,
            "C47": lambda e, c, q, v: c.warehouse_size == "2X_SMALL",
            "C48": lambda e, c, q, v: e.current_epoch.epoch_start < "2025-10-01",
            "C49": lambda e, c, q, v: c.auto_stop_minutes == 10 and v["starts_30"] >= 0.9 * q["queries_30"],
            "C50": lambda e, c, q, v: c.max_clusters == 1 and q["queued_query_pct_30"] >= 35,
            "C51": lambda e, c, q, v: 1 - 5.0 * q["queries_30"] / last30_uptime(e) >= 0.9,
            "C52": lambda e, c, q, v: len({r["usage_date"] for r in e.daily_cost_365}) <= 13,
        }
        for scenario, check in checks.items():
            e, _ = self.by_scenario[scenario]
            ep = e.current_epoch
            with self.subTest(scenario=scenario):
                self.assertTrue(check(e, ep.config, ep.query, ep.events))

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

    def test_c37_ceiling_is_kept_for_the_half_year_peak(self):
        c = self.candidate("C37", "max_clusters")
        self.assertEqual((c["status"], c["recommended_value"], c["blockers"]), ("NO_CHANGE", 8, ["PEAK_OUTSIDE_RECENT_WINDOW"]))
        size = self.candidate("C37", "warehouse_size")
        self.assertEqual(size["status"], "NO_CHANGE")
        self.assertIn("PEAK_OUTSIDE_RECENT_WINDOW", size["blockers"])

    def test_c18_preview_channel_is_observed_only(self):
        e, pkg = self.by_scenario["C18"]
        self.assertEqual(pkg["current_config"]["warehouse_channel"], "PREVIEW")
        self.assertNotIn("warehouse_channel", [c["knob"] for c in pkg["candidates"]])
        steps = [s for sc in pkg["scenarios"] for s in sc["execution_plan"]]
        self.assertNotIn("warehouse_channel", [s["knob"] for s in steps])
        evidence = pkg["evidence"].values() if isinstance(pkg["evidence"], dict) else pkg["evidence"]
        self.assertEqual([x["value"] for x in evidence if x["metric"] == "warehouse_channel"], ["PREVIEW"])

    def test_c38_capacity_cuts_wait_while_volume_grows(self):
        for knob in ("warehouse_size", "max_clusters"):
            c = self.candidate("C38", knob)
            self.assertEqual(c["status"], "NO_CHANGE", knob)
            self.assertIn("QUERY_VOLUME_GROWING", c["blockers"], knob)

    def test_c41_genie_warehouse_is_never_moved_to_classic(self):
        self.assertNotEqual(self.candidate("C41", "warehouse_type")["recommended_value"], "CLASSIC")

    def test_c48_long_unchanged_warehouse_is_analyzed(self):
        e, pkg = self.by_scenario["C48"]
        self.assertLess(e.current_epoch.epoch_start, "2025-01-01")
        self.assertEqual(len(pkg["candidates"]), 5)

    def test_c50_single_cluster_increase_goes_to_peak_demand_and_about_doubles_the_cost(self):
        # About 19 concurrent queries all day: 2 clusters of demand + 1 headroom. Demand fills the second cluster all
        # day (base ~ +100% of cost); the worst case runs both added clusters whenever the warehouse sat at its ceiling.
        e, pkg = self.by_scenario["C50"]
        c = self.candidate("C50", "max_clusters")
        self.assertEqual((c["current_value"], c["recommended_value"]), (1, 3))
        cost = pkg["cost_baseline"]["forward_365d"]["total_cost"]
        savings = c["independent_economics"]["savings_365d"]["total"]
        self.assertLess(abs(-savings["base"] / cost - 1.0), 0.05)
        self.assertLess(abs(-savings["low"] / cost - 2.0), 0.05)

    # --- Auto-stop: cost-minimizing value on measured running idle time ----------------------------------------
    def auto_stop(self, scenario):
        c = self.candidate(scenario, "auto_stop_minutes")
        return c["status"], c["recommended_value"], c

    def test_c01_c25_always_on_goes_to_the_cheapest_value_within_two_percent_and_the_cold_start_budget(self):
        for scenario in ("C01", "C25"):
            status, value, c = self.auto_stop(scenario)
            self.assertEqual(status, "ACTIONABLE", scenario)
            self.assertTrue(10 <= value <= 60, (scenario, value))
            self.assertGreater(c["independent_economics"]["savings_365d"]["total"]["base"], 20000, scenario)

    def test_c02_c10_serverless_goes_to_five(self):
        for scenario in ("C02", "C10"):
            self.assertEqual(self.auto_stop(scenario)[:2], ("ACTIONABLE", 5), scenario)

    def test_c05_nightly_etl_goes_to_the_ten_minute_floor(self):
        self.assertEqual(self.auto_stop("C05")[:2], ("ACTIONABLE", 10))

    def test_c13_lowered_when_it_adds_no_cold_starts(self):
        # Queries every 45 minutes: the warehouse already restarts for each one, so 10 adds no restart.
        self.assertEqual(self.auto_stop("C13")[:2], ("ACTIONABLE", 10))

    def test_c39_nonprod_tag_gets_the_larger_cold_start_budget(self):
        import copy
        status, value, _ = self.auto_stop("C39")
        self.assertEqual((status, value), ("ACTIONABLE", 10))  # adds restarts for about 5% of queries: within 10%
        e = copy.deepcopy(self.by_scenario["C39"][0])
        e.current_epoch.tags["environment"] = "staging"  # staging stays production: 2% budget
        c = next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "auto_stop_minutes")
        self.assertEqual((c.status, c.blockers), ("NO_CHANGE", ["COLD_START_BUDGET_EXCEEDED"]))

    def test_c45_steady_warehouse_gets_no_zero_value_auto_stop_change(self):
        status, value, c = self.auto_stop("C45")
        self.assertEqual((status, value), ("NO_CHANGE", 30))

    def test_c49_auto_stop_is_raised_when_restarts_saved_outweigh_idle_added(self):
        status, value, c = self.auto_stop("C49")
        self.assertEqual((status, value), ("TEST_REQUIRED", 15))
        self.assertIn("RESTART_BILLING_UNVERIFIED", c["blockers"])

    def test_c51_mostly_idle_removes_far_more_than_sixty_percent(self):
        e, _ = self.by_scenario["C51"]
        status, value, _ = self.auto_stop("C51")
        self.assertEqual((status, value), ("ACTIONABLE", 10))
        w = p.evaluate_warehouses([e], self.t)[0]
        raw = next(c for c in w.candidates if c.knob == "auto_stop_minutes").metadata["raw_savings_fraction"]
        self.assertGreater(raw, 0.8)

    def test_c53_c54_long_auto_stop_drops_to_the_value_covering_daytime_gaps(self):
        # Gaps of ~87 minutes (C53) and ~190 minutes (C54): the smallest 5-minute value at least 10% longer than the gap
        # keeps every daytime gap without a restart and cuts the overnight tail.
        for scenario, value in (("C53", 100), ("C54", 210)):
            status, got, c = self.auto_stop(scenario)
            self.assertEqual((status, got), ("ACTIONABLE", value), scenario)
            self.assertGreater(c["independent_economics"]["savings_365d"]["total"]["base"], 5000, scenario)

    def test_a01_serverless_auto_stop_below_five_is_left_alone(self):
        c = self.candidate("A01", "auto_stop_minutes")
        self.assertEqual((c["current_value"], c["status"]), (1, "NO_CHANGE"))

    def test_a02_pro_classic_auto_stop_target_is_never_below_ten(self):
        for scenario, (e, _) in self.by_scenario.items():
            if e.current_epoch.config.warehouse_type in {"PRO", "CLASSIC"}:
                c = self.candidate(scenario, "auto_stop_minutes")
                if c["recommended_value"] != c["current_value"]:
                    self.assertGreaterEqual(c["recommended_value"], 10, scenario)

    def test_a05_serverless_region_check_is_unknown_unless_the_workspace_runs_serverless(self):
        seen = set()
        for scenario, (e, pkg) in self.by_scenario.items():
            region = [c for s in pkg["scenarios"] for c in s["compatibility"]["checks"] if c["check"] == "aws_region_serverless_availability"]
            if not region:
                continue
            observed = bool(e.billing_features.get("workspace_serverless_observed_from_system_compute_warehouses"))
            self.assertEqual(region[0]["status"], "PASS" if observed else "UNKNOWN", scenario)
            seen.add(observed)
        self.assertEqual(seen, {False, True})

    def test_a04_cluster_targets_stay_within_limits(self):
        for scenario in self.by_scenario:
            self.assertLessEqual(self.candidate(scenario, "max_clusters")["recommended_value"], 40, scenario)
            self.assertLessEqual(self.candidate(scenario, "min_clusters")["recommended_value"], 30, scenario)


if __name__ == "__main__":
    unittest.main()
