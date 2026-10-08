"""Run scope end to end below the CLI: subset invariant, scenario construction, safety inclusions and contract rules."""
import dataclasses
import itertools
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.config.defaults import ACTIVE_KNOBS  # noqa: E402
from publisher.config.loader import resolve_scope  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402
from publisher.contracts import workbook_contract as wc  # noqa: E402
from publisher.contracts.analysis import FleetAnalysis  # noqa: E402
from publisher.contracts.request import MODE_REPLAY, PublisherRequest  # noqa: E402
from publisher.contracts.scope import FULL_SCOPE, RunScope  # noqa: E402
from publisher.governor import governor  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402
from publisher.packager import packager  # noqa: E402
from publisher.reviewer import reviewer  # noqa: E402
from tests.golden.publisher import case_names, load_case  # noqa: E402
from tests.golden.publisher.harness import recompute_forecast  # noqa: E402

ALL_SCOPES = [RunScope(knobs=tuple(sorted(c))) for n in range(1, len(ACTIVE_KNOBS) + 1) for c in itertools.combinations(ACTIVE_KNOBS, n)]
IN_PLACE = {"warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes"}


def packages_for(analysis, scope, t):
    """Reviewed packages for an analysis under a scope (the analysis is shared, so each scope costs no re-analysis)."""
    context = p.build_context(PublisherRequest(mode=MODE_REPLAY, output_dir=Path(".")), t, scope)
    governance = governor.govern(analysis, scope)
    optimization = optimizer.run(context, analysis, governance)
    reviewed = reviewer.review_candidates(context, analysis, governance, optimization)
    return reviewer.review_package(context, analysis, governance, packager.run(context, analysis, governance, reviewed)).packages


def analysis_of(evidence, t):
    return FleetAnalysis(list(evidence), analyzer.analyze(evidence, t))


def rollback(c):
    return "CANARY_GUARDRAIL_BREACHED" in c.blockers


class SubsetInvariantTests(unittest.TestCase):
    """Every golden case under every non-empty scope: valid packages whose candidates are the full run's, unchanged."""

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_scoped_candidates_are_a_byte_identical_subset_of_the_full_run(self):
        self.assertEqual(len(ALL_SCOPES), 31)
        for name in case_names():
            case = load_case(name)
            if "evidence" not in case or case.get("scope"):
                continue
            evidence = [evidence_from_dict(x) for x in case["evidence"]]
            if (case.get("expect") or {}).get("recompute_forecast"):
                for e in evidence:
                    recompute_forecast(p, e)
            analysis = analysis_of(evidence, self.t)
            full = {pk.warehouse.warehouse_id: pk for pk in packages_for(analysis, FULL_SCOPE, self.t)}
            for scope in ALL_SCOPES:
                for pk in packages_for(analysis, scope, self.t):
                    with self.subTest(case=name, warehouse=pk.warehouse.warehouse_id, scope=scope.describe()):
                        self.check(full[pk.warehouse.warehouse_id], pk, scope)

    def check(self, full, pk, scope):
        full_by_knob = {c.knob.value: c for c in full.candidates}
        got = {c.knob.value: c for c in pk.candidates}
        expected_knobs = set(scope.knobs) | {k for k, c in full_by_knob.items() if rollback(c)}
        self.assertEqual(set(got), expected_knobs)
        for knob, c in got.items():
            self.assertEqual(rc.to_primitive(c), rc.to_primitive(full_by_knob[knob]), knob)
        types = {s.scenario_type for s in pk.scenarios}
        recommended = next(s for s in pk.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED)
        self.assertNotEqual(recommended.scenario_type, rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION)
        self.assertEqual(rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION in types, "warehouse_type" in scope.knobs)
        has_in_place = bool(IN_PLACE & set(scope.knobs)) or any(rollback(c) for c in pk.candidates)
        self.assertEqual(rc.ScenarioType.IN_PLACE_OPTIMIZATION in types, has_in_place)
        self.assertEqual(rc.ScenarioType.NO_CHANGE_BASELINE in types, not has_in_place)
        if scope.is_full:
            self.assertIsNone(pk.run_scope)
            self.assertEqual(rc.to_primitive(pk), rc.to_primitive(full))
        else:
            self.assertEqual(sorted(k.value for k in pk.run_scope.knobs()), sorted(scope.knobs))


class ScopedScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = Thresholds()
        idle = [evidence_from_dict(x) for x in load_case("idle-expensive-warehouse")["evidence"]]
        cls.idle = analysis_of(idle, cls.t)
        fleet = synthetic_demo_evidence(len(p.DEMO_FLEET))
        rolled = [e for e in fleet if e.warehouse_id == "wh-demo-040"]
        cls.rolled = analysis_of(rolled, cls.t)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def package(self, analysis, *specs):
        return packages_for(analysis, resolve_scope(specs), self.t)[0]

    def test_out_of_scope_rollback_is_kept_as_a_safety_inclusion_and_runs_first(self):
        pk = self.package(self.rolled, "current:auto-stop")
        self.assertEqual(sorted(c.knob.value for c in pk.candidates), ["auto_stop_minutes", "warehouse_size"])
        size = next(c for c in pk.candidates if c.knob == rc.TunableKnob.WAREHOUSE_SIZE)
        self.assertEqual([(x.knob, x.candidate_id, x.reason_code) for x in pk.run_scope.safety_inclusions],
                         [(rc.TunableKnob.WAREHOUSE_SIZE, size.candidate_id, "CANARY_GUARDRAIL_BREACHED")])
        plan = next(s for s in pk.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED)
        self.assertEqual(plan.execution_plan[0].candidate_id, size.candidate_id)
        self.assertEqual(wc.run_scope_text(pk), "Current type: Auto-stop (+ safety: Size rollback)")

    def test_rollback_in_scope_is_not_a_safety_inclusion(self):
        pk = self.package(self.rolled, "current:size")
        self.assertEqual([c.knob.value for c in pk.candidates], ["warehouse_size"])
        self.assertEqual(pk.run_scope.safety_inclusions, [])

    def test_migration_only_with_a_rollback_recommends_the_rollback_plan(self):
        pk = self.package(self.rolled, "migration")
        recommended = next(s for s in pk.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED)
        self.assertEqual(recommended.scenario_type, rc.ScenarioType.IN_PLACE_OPTIMIZATION)
        self.assertEqual([st.knob for st in recommended.execution_plan if st.knob], [rc.TunableKnob.WAREHOUSE_SIZE])
        self.assertIn(rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION, {s.scenario_type for s in pk.scenarios})

    def test_migration_only_recommends_the_no_change_baseline(self):
        pk = self.package(self.idle, "migration")
        recommended = next(s for s in pk.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED)
        self.assertEqual(recommended.scenario_type, rc.ScenarioType.NO_CHANGE_BASELINE)
        self.assertEqual(recommended.execution_plan, [])
        self.assertEqual(pk.recommended_plan.selection_reason_code, "CURRENT_TYPE_OUT_OF_RUN_SCOPE")
        self.assertEqual(pk.recommended_plan.next_action, "REVIEW_SCENARIO")
        self.assertEqual(pk.package_economics.estimated_savings_365d.base, 0.0)

    def test_single_technique_plan_equals_its_candidate(self):
        for spec in ("current:auto-stop", "current:size", "current:min-clusters"):
            with self.subTest(spec=spec):
                pk = self.package(self.idle, spec)
                (c,) = pk.candidates
                plan = next(s for s in pk.scenarios if s.status == rc.ScenarioStatus.RECOMMENDED)
                self.assertEqual(plan.portfolio_economics.savings_365d.total, c.independent_economics.savings_365d.total)

    def test_scope_changes_package_id_but_not_candidate_ids(self):
        full = self.package(self.idle)
        scoped = self.package(self.idle, "current:auto-stop")
        self.assertNotEqual(full.recommendation_package_id, scoped.recommendation_package_id)
        self.assertIn(scoped.candidates[0].candidate_id, {c.candidate_id for c in full.candidates})


class RunScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        t = Thresholds()
        analysis = analysis_of([evidence_from_dict(x) for x in load_case("idle-expensive-warehouse")["evidence"]], t)
        cls.full = packages_for(analysis, FULL_SCOPE, t)[0]
        cls.migration = packages_for(analysis, resolve_scope(["migration"]), t)[0]

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def assertInvalid(self, package, message):
        with self.assertRaisesRegex(ValueError, message):
            package.validate(verify_content_id=False)

    def test_full_scope_must_be_null(self):
        everything = rc.RunScope([
            rc.ScopedScenario(rc.ScenarioType.IN_PLACE_OPTIMIZATION, [rc.TunableKnob(k) for k in sorted(IN_PLACE)]),
            rc.ScopedScenario(rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION, [rc.TunableKnob.WAREHOUSE_TYPE]),
        ])
        self.assertInvalid(dataclasses.replace(self.full, run_scope=everything), "full scope is represented by run_scope null")

    def test_candidate_outside_the_scope_is_rejected(self):
        auto_stop = rc.RunScope([rc.ScopedScenario(rc.ScenarioType.IN_PLACE_OPTIMIZATION, [rc.TunableKnob.AUTO_STOP_MINUTES])])
        self.assertInvalid(dataclasses.replace(self.full, run_scope=auto_stop), "outside the run scope")

    def test_baseline_requires_a_run_scope(self):
        self.assertInvalid(dataclasses.replace(self.migration, run_scope=None), "NO_CHANGE_BASELINE scenario requires a run_scope")

    def test_scoped_knobs_must_be_sorted_and_belong_to_their_scenario(self):
        for scenario, message in (
            (rc.ScopedScenario(rc.ScenarioType.IN_PLACE_OPTIMIZATION, [rc.TunableKnob.MIN_CLUSTERS, rc.TunableKnob.AUTO_STOP_MINUTES]), "unique and sorted"),
            (rc.ScopedScenario(rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION, [rc.TunableKnob.AUTO_STOP_MINUTES]), "do not belong"),
            (rc.ScopedScenario(rc.ScenarioType.NO_CHANGE_BASELINE, [rc.TunableKnob.AUTO_STOP_MINUTES]), "cannot be scoped"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                scenario.validate()

    def test_run_scope_enters_the_package_hash(self):
        self.assertIsNone(self.full.package_hash_payload()["run_scope"])
        self.assertIsNotNone(self.migration.package_hash_payload()["run_scope"])
        tampered = dataclasses.replace(self.migration, run_scope=rc.RunScope(
            [rc.ScopedScenario(rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION, [rc.TunableKnob.WAREHOUSE_TYPE])],
            [],
        ))
        self.assertEqual(tampered.expected_package_id(), self.migration.recommendation_package_id)
        moved = dataclasses.replace(self.migration, run_scope=None)
        self.assertNotEqual(moved.expected_package_id(), self.migration.recommendation_package_id)


class ScopedWorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        t = Thresholds()
        analysis = analysis_of([evidence_from_dict(x) for x in load_case("idle-expensive-warehouse")["evidence"]], t)
        cls.full = packages_for(analysis, FULL_SCOPE, t)
        cls.auto_stop = packages_for(analysis, resolve_scope(["current:auto-stop"]), t)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def metrics(self, packages, selection=None):
        projection = wc.project_workbook(packages, [], selection)
        return {(r["section"], r["metric"]): r for r in projection.executive_metrics}, projection

    def test_scoped_workbook_shows_only_what_was_asked_for(self):
        selection = {"requested": ["wh-demo-001"], "selected_count": 1, "not_selected_count": 4}
        metrics, projection = self.metrics(self.auto_stop, selection)
        self.assertEqual(len(projection.recommendation_registry), 1)
        self.assertEqual(len(projection.scenario_plans), 1)
        self.assertEqual(projection.warehouse_portfolio[0]["run_scope"], "Current type: Auto-stop")
        self.assertEqual(metrics[("Run scope", "Scenarios and techniques")]["value"], "Current type: Auto-stop")
        self.assertEqual(metrics[("Run scope", "Warehouses selected")]["value"], 1)
        self.assertEqual(metrics[("Run scope", "Warehouses not selected")]["value"], 4)
        self.assertEqual(metrics[("Run scope", "Warehouses selected")]["notes"], "Requested: wh-demo-001")
        upside = metrics[("Experiment upside", "Migration experiment high")]
        self.assertEqual((upside["value"], upside["notes"]), (wc.DISPLAY_UNAVAILABLE, "Migration not in run scope"))

    def test_full_workbook_labels_the_full_scope(self):
        metrics, projection = self.metrics(self.full)
        self.assertEqual(projection.warehouse_portfolio[0]["run_scope"], "Full")
        self.assertEqual(metrics[("Run scope", "Scenarios and techniques")]["value"], "Full")
        self.assertEqual(metrics[("Run scope", "Warehouses selected")]["value"], wc.DISPLAY_UNAVAILABLE)
        self.assertNotEqual(metrics[("Experiment upside", "Migration experiment high")]["notes"], "Migration not in run scope")


if __name__ == "__main__":
    unittest.main()
