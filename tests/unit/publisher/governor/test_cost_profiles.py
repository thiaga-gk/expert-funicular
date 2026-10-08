"""Cost profiles (src/publisher/governor/cost_profiles.py, src/publisher/config/cost_profiles.py): the profile values,
which warehouse gets which profile, and how the profile reaches the package identity, the gates and the workbook."""
import ast
import copy
import dataclasses
import fnmatch
import json
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.config.cost_profiles import COST_PROFILES, CostProfileSelection, parse_cost_profile_flags  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402
from publisher.contracts import workbook_contract as wc  # noqa: E402
from publisher.governor.cost_profiles import (  # noqa: E402
    AGGRESSIVE, BALANCED, CONSERVATIVE, PROFILED_FIELDS, applied_thresholds, assign_cost_profiles, profile_values,
    thresholds_for_profile,
)
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.reviewer.package import review_contract  # noqa: E402

C06 = ROOT / "tests" / "golden" / "publisher" / "cases" / "c06-oversized-warehouse" / "source" / "evidence.json"
# Families a profile must never change: evidence floors, product facts, estimator/forecast/confidence parameters,
# classification rules, the failure-cancel guard and the rules that add capacity.
NEVER_PROFILED = ("confidence_*", "forecast_*", "realization_*", "*_realization_*", "*_runtime_multiplier_*", "photon_*",
                  "plan_risk_*", "readiness_*", "min_economics_*", "*sample_size", "failure_cancel_*", "*_increase_max",
                  "*upsize*", "restart_cost_*", "nonprod_*", "billing_*", "trend_*", "migration_*", "cost_factor_*",
                  "severity_*", "concurrency_min_*", "max_cluster_target_*", "max_cluster_headroom_*", "time_at_max_pct_saturated")


def c06(env="production", p95=1500):
    items = json.loads(C06.read_text(encoding="utf-8"))
    items[0]["epochs"][-1]["tags"]["environment"] = env
    items[0]["epochs"][-1]["query"]["p95_execution_ms_30"] = p95
    return [evidence_from_dict(x) for x in items]


class ProfileValueTests(unittest.TestCase):
    def test_every_profile_sets_exactly_the_contract_fields_and_balanced_is_the_defaults(self):
        self.assertEqual(COST_PROFILES, (AGGRESSIVE, BALANCED, CONSERVATIVE))
        for name in COST_PROFILES:
            self.assertEqual(sorted(profile_values(name)), sorted(PROFILED_FIELDS), name)
        base = Thresholds()
        self.assertIs(thresholds_for_profile(base, BALANCED), base)  # balanced is the base itself
        self.assertEqual(applied_thresholds(Thresholds()), {g: {f: profile_values(BALANCED)[f] for f in fs} for g, fs in rc.COST_PROFILE_GROUPS.items()})

    def test_profiles_order_from_conservative_to_aggressive(self):
        # Every field loosens from conservative through balanced to aggressive; which way is "looser" depends on the rule.
        tighter_when_lower = {"spill_query_pct_downsize_safe", "p95_execution_ms_downsize_safe", "p99_execution_ms_downsize_safe",
                              "time_at_max_pct_safe_reduce", "queue_wait_p95_ms_safe", "cold_start_added_share_max_pct",
                              "queue_query_pct_safe", "canary_p95_regression_pct", "growth_hold_weekly_ratio"}
        for field in PROFILED_FIELDS:
            c, b, a = (profile_values(n)[field] for n in (CONSERVATIVE, BALANCED, AGGRESSIVE))
            if field in tighter_when_lower:
                self.assertTrue(c < b < a, field)
            else:
                self.assertTrue(c > b > a, field)

    def test_safe_levels_stay_below_the_levels_that_add_capacity(self):
        for name in COST_PROFILES:
            t = thresholds_for_profile(Thresholds(), name)
            with self.subTest(profile=name):
                self.assertLess(t.queue_query_pct_safe, t.queue_query_pct_increase_max)
                self.assertLess(t.queue_wait_p95_ms_safe, t.queue_wait_p95_ms_increase_max)
                self.assertLess(t.spill_query_pct_downsize_safe, t.spill_query_pct_upsize)
                self.assertLess(t.time_at_max_pct_safe_reduce, t.time_at_max_pct_saturated)
                self.assertLessEqual(t.time_at_max_pct_saturated, t.time_at_max_pct_block_min_reduce)

    def test_no_profiled_field_belongs_to_a_family_profiles_must_not_change(self):
        offending = [f for f in PROFILED_FIELDS for pattern in NEVER_PROFILED if fnmatch.fnmatch(f, pattern)]
        self.assertEqual(offending, [])
        self.assertEqual(sorted(set(PROFILED_FIELDS) - {f.name for f in dataclasses.fields(Thresholds)}), [])

    def test_the_collector_reads_no_profiled_field(self):
        # Collection runs before profiles are resolved, so its SQL and simulation must not depend on a profile.
        read = set()
        for path in sorted((ROOT / "src" / "publisher" / "analyzer" / "collector").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr in PROFILED_FIELDS:
                    read.add(f"{path.name}:{node.attr}")
        self.assertEqual(sorted(read), [])

    def test_the_contract_example_carries_the_balanced_profile(self):
        example = json.loads((ROOT / "examples" / "recommendation-package.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["cost_profile"]["applied_thresholds"], applied_thresholds(Thresholds()))


class SelectionTests(unittest.TestCase):
    def test_parse_flags(self):
        self.assertEqual(parse_cost_profile_flags([]), CostProfileSelection())
        self.assertEqual(parse_cost_profile_flags(["Conservative", "aggressive=wh-2,1/wh-1", "aggressive=wh-2"]),
                         CostProfileSelection("conservative", (("1/wh-1", "aggressive"), ("wh-2", "aggressive"))))
        for bad in (["reckless"], ["balanced", "aggressive"], ["aggressive="], ["aggressive=wh 1"], ["aggressive=wh-1,"],
                    ["aggressive=a/b/c"], ["aggressive=wh-1", "conservative=wh-1"]):
            with self.subTest(flags=bad), self.assertRaises(PublisherError):
                parse_cost_profile_flags(bad)

    def test_precedence_warehouse_then_all_then_tag(self):
        fleet = synthetic_demo_evidence()
        dev = next(e for e in fleet if e.current_epoch.tags.get("golden_scenario") == "C39")
        prod = next(e for e in fleet if e is not dev)
        t = Thresholds()

        def choice(flags, e):
            return assign_cost_profiles(fleet, parse_cost_profile_flags(flags), t)[(e.workspace_id, e.warehouse_id)]

        self.assertEqual((choice([], dev).name, choice([], dev).source, choice([], dev).source_detail),
                         ("aggressive", "TAG_DEFAULT", "tag environment=nonprod"))
        self.assertEqual((choice([], prod).name, choice([], prod).source, choice([], prod).source_detail), ("balanced", "DEFAULT", None))
        self.assertEqual((choice(["conservative"], dev).name, choice(["conservative"], dev).source), ("conservative", "CLI_ALL"))
        flags = ["conservative", f"aggressive={prod.warehouse_id}"]
        self.assertEqual((choice(flags, prod).name, choice(flags, prod).source, choice(flags, prod).source_detail),
                         ("aggressive", "CLI_WAREHOUSE", f"--cost-profile aggressive={prod.warehouse_id}"))
        qualified = [f"balanced={dev.workspace_id}/{dev.warehouse_id}"]
        self.assertEqual(choice(qualified, dev).name, "balanced")

    def test_unknown_ambiguous_or_conflicting_warehouses_refuse_the_run(self):
        fleet = synthetic_demo_evidence(3)
        twin = copy.deepcopy(fleet[0])
        twin.workspace_id = "9999999999999999"
        t = Thresholds()
        for flags, evidence in ((["aggressive=wh-typo"], fleet), ([f"aggressive={fleet[0].warehouse_id}"], fleet + [twin]),
                                ([f"aggressive={fleet[0].warehouse_id}", f"conservative={fleet[0].workspace_id}/{fleet[0].warehouse_id}"], fleet)):
            with self.subTest(flags=flags), self.assertRaises(PublisherError):
                assign_cost_profiles(evidence, parse_cost_profile_flags(flags), t)


class LoggingTests(unittest.TestCase):
    def test_assignments_are_one_info_summary_and_a_debug_line_per_warehouse(self):
        from publisher.governor.cost_profiles import CostProfileChoice, log_assignments
        choices = {("1", "a"): CostProfileChoice("balanced", "DEFAULT", None), ("1", "b"): CostProfileChoice("aggressive", "TAG_DEFAULT", "tag env=dev"),
                   ("1", "c"): CostProfileChoice("balanced", "DEFAULT", None)}
        with self.assertLogs("publisher", "DEBUG") as logs:
            log_assignments(choices)
        self.assertEqual([r.levelname for r in logs.records], ["DEBUG"] * 3 + ["INFO"])
        self.assertEqual(logs.records[-1].getMessage(), "governor.cost_profiles warehouses=3 aggressive=1 balanced=2")


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.fleet = synthetic_demo_evidence()
        cls.by_profile = {name: {(x.warehouse.workspace_id, x.warehouse.warehouse_id): x for x in
                                 p.build_recommendation_packages(cls.fleet, Thresholds(), cost_profiles=(name,))}
                          for name in COST_PROFILES}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_a_profile_changes_which_cuts_pass_never_what_a_candidate_is_worth(self):
        balanced = self.by_profile[BALANCED]
        compared = 0
        for name in (AGGRESSIVE, CONSERVATIVE):
            for key, package in self.by_profile[name].items():
                base = {c.knob: c for c in balanced[key].candidates}
                for c in package.candidates:
                    other = base[c.knob]
                    # The Serverless -> Pro migration target is priced at its own best auto-stop within the profile's
                    # cold-start budget: the one documented exception.
                    if c.knob == rc.TunableKnob.WAREHOUSE_TYPE or c.recommended_value != other.recommended_value:
                        continue
                    compared += 1
                    self.assertEqual(c.independent_economics.savings_365d, other.independent_economics.savings_365d, (name, key, c.knob))
                self.assertEqual(package.workload_profile, balanced[key].workload_profile, (name, key))
                self.assertEqual(package.cost_baseline, balanced[key].cost_baseline, (name, key))
        self.assertGreater(compared, 100)

    def test_the_profile_enters_every_id_but_how_it_was_chosen_does_not(self):
        for key, package in self.by_profile[AGGRESSIVE].items():
            balanced = self.by_profile[BALANCED][key]
            self.assertNotEqual(package.recommendation_package_id, balanced.recommendation_package_id)
            self.assertTrue(set(c.candidate_id for c in package.candidates).isdisjoint(c.candidate_id for c in balanced.candidates))
        # The C39 warehouse is aggressive by its tag by default and by the flag here: same ids, different source.
        tagged = {(x.warehouse.workspace_id, x.warehouse.warehouse_id): x for x in p.build_recommendation_packages(self.fleet, Thresholds())}
        same = [k for k, x in tagged.items() if x.cost_profile.name == rc.CostProfileName.AGGRESSIVE]
        self.assertEqual(len(same), 1)
        by_tag, by_flag = tagged[same[0]], self.by_profile[AGGRESSIVE][same[0]]
        self.assertEqual((by_tag.cost_profile.source, by_flag.cost_profile.source), (rc.CostProfileSource.TAG_DEFAULT, rc.CostProfileSource.CLI_ALL))
        self.assertEqual(by_tag.recommendation_package_id, by_flag.recommendation_package_id)
        self.assertEqual([c.candidate_id for c in by_tag.candidates], [c.candidate_id for c in by_flag.candidates])

    def test_the_gates_reject_a_mislabelled_profile(self):
        package = next(iter(self.by_profile[BALANCED].values()))
        review_contract(package, Thresholds())
        mislabelled = dataclasses.replace(package, cost_profile=dataclasses.replace(package.cost_profile, name=rc.CostProfileName.AGGRESSIVE))
        with self.assertRaises(ValueError):
            mislabelled.validate()  # a DEFAULT profile is balanced
        tampered = copy.deepcopy(package.cost_profile.applied_thresholds)
        tampered["size"]["p95_execution_ms_downsize_safe"] = 99_999
        with self.assertRaises(ValueError):
            review_contract(dataclasses.replace(package, cost_profile=dataclasses.replace(package.cost_profile, applied_thresholds=tampered)), Thresholds())
        with self.assertRaises(ValueError):
            dataclasses.replace(package.cost_profile, source_detail="tag env=dev").validate()

    def test_the_workbook_shows_the_profile_on_three_sheets(self):
        packages = list(self.by_profile[CONSERVATIVE].values())[:2] + list(self.by_profile[AGGRESSIVE].values())[:1]
        view = wc.project_workbook(packages)
        self.assertEqual([r["cost_profile"] for r in view.warehouse_portfolio], ["conservative", "conservative", "aggressive"])
        self.assertEqual(view.warehouse_portfolio[0]["profile_source"], "CLI_ALL (--cost-profile conservative)")
        self.assertEqual({r["cost_profile"] for r in view.recommendation_registry}, {"conservative", "aggressive"})
        counts = {r["metric"]: r["value"] for r in view.executive_metrics if r["section"] == "Cost profiles"}
        self.assertEqual(counts, {"Warehouses: aggressive": 1, "Warehouses: balanced": 0, "Warehouses: conservative": 2})


class ProfileDecisionTests(unittest.TestCase):
    """The same evidence decides differently under each profile only through the profiled limits (golden C56-C58)."""

    def test_p95_limit_follows_the_profile(self):
        logging.disable(logging.CRITICAL)
        try:
            def size(evidence, flags=()):
                package = p.build_recommendation_packages(evidence, Thresholds(), cost_profiles=flags)[0]
                return next(c for c in package.candidates if c.knob == rc.TunableKnob.WAREHOUSE_SIZE)
            self.assertEqual(size(c06("dev", 7000)).status, rc.RecommendationStatus.TEST_REQUIRED)
            self.assertIn("P95_LATENCY_TOO_HIGH", size(c06("production", 7000)).blockers)
            self.assertEqual(size(c06("production", 4000)).status, rc.RecommendationStatus.TEST_REQUIRED)
            self.assertIn("P95_LATENCY_TOO_HIGH", size(c06("production", 4000), ("conservative",)).blockers)
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
