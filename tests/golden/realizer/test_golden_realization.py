"""Golden realization scenarios GR-001..GR-040 (docs/proposals/realizer/golden-scenarios.md) on the Realizer in src/realizer.

Each test keeps its original inline construction from ``demo_observation``; the same observations are frozen under
tests/fixtures/realizer/observations/GR-*.json with their canonical outputs in tests/fixtures/realizer/expected/
(tests/regression/realizer/test_stored_baseline.py). The scenario registry is tests/fixtures/realizer/golden-registry.json.
"""
import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402
canonical.modular()  # src/realizer, never a shadowing copy
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import make_rpkg_id  # noqa: E402
from realizer.estimator.attribution import authoritative_cumulative_realization  # noqa: E402
from realizer.estimator.economics import calculate_economics  # noqa: E402
from realizer.governor.policy import RealizerPolicy, savings_improvement_material  # noqa: E402
from realizer.packager.ledger import ledger_path, load_ledger  # noqa: E402
from realizer.realizer import realize  # noqa: E402
from realizer.reporter.reporter import legacy_compatibility  # noqa: E402
from realizer.reconciler.matching import _directional_relation  # noqa: E402


def _iso_add(value: str, days: int = 0, hours: int = 0) -> str:
    d = dt.datetime.fromisoformat(value.replace('Z', '+00:00')) + dt.timedelta(days=days, hours=hours)
    return d.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


class RealizerGoldenTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.package = canonical.load_scenario('example-exact')['package_doc']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.pkg = copy.deepcopy(self.package)

    def tearDown(self):
        self.tmp.cleanup()

    def obs(self, *, modified=False):
        return demo_observation(self.pkg, modified=modified)

    def realize_case(self, obs=None, *, package=None, policy=None, out=None):
        return realize(package or self.pkg, obs, out or self.out, policy=policy)

    def later(self, obs, *, days=1, new_application=False):
        x = copy.deepcopy(obs)
        x['observed_at_utc'] = _iso_add(obs['observed_at_utc'], days=days)
        x['recorded_at_utc'] = x['observed_at_utc']
        x['window_start_utc'] = _iso_add(obs['window_start_utc'], days=days)
        x['window_end_utc'] = x['observed_at_utc']
        if new_application:
            x['applied_at_utc'] = x['observed_at_utc']
        return x

    def set_ratio(self, obs, ratio):
        # Use an explicit same-scope denominator to make the golden ratio exact.
        expected = Decimal('12000')
        annual = expected * Decimal(str(ratio))
        period = annual * Decimal('30') / Decimal('365')
        obs['economics']['expected_annual_savings_actual_scope'] = str(expected)
        obs['economics']['counterfactual_old_cost'] = '10000'
        obs['economics']['observed_new_cost'] = str(Decimal('10000') - period)
        obs['economics']['period_days'] = 30
        return obs

    def ledger(self):
        rpkg = make_rpkg_id(self.pkg['recommendation_package_id'])
        return load_ledger(ledger_path(self.out, rpkg))

    # GR-001
    def test_gr_001_no_implementation(self):
        s = self.realize_case()
        self.assertEqual(s.current_state['realization_state'], 'NOT_APPLIED')
        self.assertEqual(s.application['state'], 'NOT_APPLIED')

    # GR-002
    def test_gr_002_exact_application_intent_unknown(self):
        o = self.obs(); o['intent_state'] = 'UNKNOWN'; o['origin_attribution'] = 'UNKNOWN'; o['actor'] = None
        s = self.realize_case(o)
        self.assertEqual(s.application['implementation_conformance'], 'EXACT')
        self.assertEqual(s.application['intent_state'], 'UNKNOWN')
        self.assertEqual(s.attribution['attribution_claim_status'], 'OBSERVABLE_ONLY')

    # GR-003
    def test_gr_003_exact_confirmed_origin(self):
        o = self.obs(); o['elapsed_days'] = 30  # the size canary's 30-day guard window has ended
        s = self.realize_case(o)
        self.assertEqual(s.attribution['origin_attribution'], 'CONFIRMED')
        self.assertEqual(s.attribution['attribution_claim_status'], 'CLAIMABLE')

    # GR-004
    def test_gr_004_modified_less_aggressive_numeric(self):
        conf, rel = _directional_relation(120, 30, 60)
        self.assertEqual(conf, rc.ImplementationConformance.MODIFIED)
        self.assertEqual(rel, 'LESS_AGGRESSIVE')
        app = {'implementation_conformance':'MODIFIED', 'application_scope':'CANDIDATE', 'step':1, 'scenario_id':self.pkg['recommended_plan']['scenario_id']}
        o = self.obs(modified=True); o['economics']['expected_annual_savings_actual_scope'] = '80000'
        e = calculate_economics(self.pkg, app, o, RealizerPolicy(), True, True)
        self.assertEqual(e['denominator_source'], 'REESTIMATED_APPLIED_SCOPE')

    # GR-005
    def test_gr_005_modified_more_aggressive_can_realize(self):
        s = self.realize_case(self.obs(modified=True))
        self.assertEqual(s.application['implementation_conformance'], 'MODIFIED')
        self.assertEqual(s.application['modification_relation'], 'MORE_AGGRESSIVE')
        self.assertEqual(s.current_state['realization_state'], 'REALIZED')

    # GR-006
    def test_gr_006_contrary_implementation(self):
        o = self.obs(); o['config']['warehouse_size'] = 'X_LARGE'
        s = self.realize_case(o)
        self.assertEqual(s.application['implementation_conformance'], 'CONTRARY')
        self.assertEqual(s.regeneration['status'], 'REQUESTED')
        self.assertFalse(s.economics.get('available', False))
        # The opposite of the recommendation invalidates the plan (R08); the legacy projection can only say APPLIED.
        self.assertEqual(s.current_state['realization_state'], 'INVALIDATED')
        legacy = legacy_compatibility(s)
        self.assertEqual((legacy['status'], legacy['legacy_realization_status']), ('LOSSY', 'APPLIED'))
        self.assertIn('INVALIDATED_COLLAPSES_TO_LEGACY_APPLIED', legacy['reasons'])

    # GR-007
    def test_gr_007_unrelated_change(self):
        # Nothing the plan recommends changed (a knob the plan does not cover cannot be told apart from no change).
        o = self.obs(); o['config'] = package_config(self.pkg)
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'NOT_APPLIED')
        self.assertEqual(s.application['state'], 'NOT_APPLIED')

    # GR-008
    def test_gr_008_applied_but_immature(self):
        o = self.obs(); o['elapsed_days'] = 3; o['query_count'] = 45
        s = self.realize_case(o)
        self.assertEqual(s.application['state'], 'APPLIED')
        self.assertEqual(s.validation['state'], 'NOT_READY')
        self.assertEqual(s.maturity['state'], 'COLLECTING')

    # GR-009
    def test_gr_009_rare_workload_waits_for_regime(self):
        o = self.obs(); o['elapsed_days'] = 10; o['query_count'] = 200; o['representative_regime'] = False
        s = self.realize_case(o)
        self.assertEqual(s.validation['state'], 'NOT_READY')

    # GR-010
    def test_gr_010_exact_realized_success(self):
        s = self.realize_case(self.obs())
        self.assertEqual(s.current_state['realization_state'], 'REALIZED')
        self.assertGreater(Decimal(s.economics['successful_realized_savings']), 0)

    # GR-011
    def test_gr_011_safe_under_realized_60pct(self):
        o = self.set_ratio(self.obs(), Decimal('0.60'))
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'UNDER_REALIZED')
        self.assertEqual(s.economics['classification_detail'], 'UNDER_REALIZED')

    # GR-012
    def test_gr_012_safe_below_50_requests_reopt(self):
        o = self.set_ratio(self.obs(), Decimal('0.40'))
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'UNDER_REALIZED')
        self.assertTrue(s.economics['reoptimization_recommended'])

    # GR-013
    def test_gr_013_positive_savings_p95_failure(self):
        o = self.obs(); o['performance']['normalized_p95_runtime_regression_pct'] = '30'  # above the balanced profile's 25
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'VALIDATION_FAILED')
        self.assertEqual(Decimal(s.economics['successful_realized_savings']), 0)
        self.assertGreater(Decimal(s.economics['observed_economic_delta']), 0)
        self.assertIsNone(s.economics['annualized_realized_savings'])   # a failed measurement publishes no run-rate (R13)
        self.assertIsNone(s.economics['realization_ratio'])

    # GR-014
    def test_gr_014_reliability_failure(self):
        o = self.obs(); o['performance']['reliability_pass'] = False
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'VALIDATION_FAILED')
        self.assertIsNone(s.economics['annualized_realized_savings'])
        self.assertIsNone(s.economics['realization_ratio'])

    # GR-015
    def test_gr_015_no_defensible_counterfactual(self):
        o = self.obs(); o['economics']['counterfactual_method'] = 'NO_DEFENSIBLE_COUNTERFACTUAL'
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'BLOCKED')
        self.assertEqual(Decimal(s.economics['successful_realized_savings']), 0)
        self.assertIn('NO_DEFENSIBLE_COUNTERFACTUAL', s.attribution['hard_disqualifiers'])
        self.assertIsNone(s.economics['annualized_realized_savings'])   # a blocked measurement publishes no run-rate (R13)
        self.assertIsNone(s.economics['realization_ratio'])

    # GR-016
    def test_gr_016_modified_missing_same_scope_denominator(self):
        o = self.obs(modified=True); o['economics']['expected_annual_savings_actual_scope'] = None
        s = self.realize_case(o)
        self.assertIsNone(s.economics['realization_ratio'])
        self.assertEqual(s.economics['classification_detail'], 'VALIDATED_VALUE_UNCLASSIFIED')

    # GR-017
    def test_gr_017_first_plan_a_step_realized_downstream_pending(self):
        s = self.realize_case(self.obs())
        self.assertEqual(s.application['plan_progress']['state'], 'PARTIALLY_COMPLETED')
        plan = next(x for x in self.pkg['scenarios'] if x['scenario_id'] == self.pkg['recommended_plan']['scenario_id'])['execution_plan']
        pending = [x for x in plan if x['execution_state'] == 'PENDING_REEVALUATION']
        self.assertGreater(len(pending), 0)
        self.assertEqual(s.application['plan_progress']['pending_reevaluation_steps'], len(pending))
        self.assertEqual(s.regeneration['status'], 'REQUESTED')

    # GR-018
    def test_gr_018_modified_plan_a_regenerates_from_actual(self):
        s = self.realize_case(self.obs(modified=True))
        self.assertEqual(s.regeneration['status'], 'REQUESTED')
        req = next(self.out.glob('realization/regeneration/*.json'))
        payload = json.loads(req.read_text())
        self.assertEqual(payload['actual_config']['warehouse_size'], 'SMALL')

    # GR-019
    def test_gr_019_multiple_compatible_changes_scenario_scope(self):
        # the fixture whose plan has two authority-eligible steps (size READY_FOR_CANARY, auto-stop READY); both are applied
        p = canonical.load_scenario('GR-019')['package_doc']
        scn = next(x for x in p['scenarios'] if x['scenario_id'] == p['recommended_plan']['scenario_id'])
        eligible = [x for x in scn['execution_plan'] if x['step_type'] == 'CANDIDATE' and x['execution_state'] in ('READY', 'READY_FOR_CANARY')]
        self.assertEqual([x['knob'] for x in eligible], ['warehouse_size', 'auto_stop_minutes'])
        o = demo_observation(p)
        for step in eligible:
            o['config'][step['knob']] = step['recommended_value']
        o['economics']['expected_annual_savings_actual_scope'] = '15000'
        s = self.realize_case(o, package=p)
        self.assertEqual(s.application['application_scope'], 'SCENARIO')
        self.assertEqual(len(s.application['applied_step_refs']), 2)
        self.assertEqual(s.attribution['attribution_scope'], 'SCENARIO')

    # GR-020
    def test_gr_020_pending_downstream_change_not_credited(self):
        o = self.obs(); o['config'] = package_config(self.pkg)
        plan = next(x for x in self.pkg['scenarios'] if x['scenario_id'] == self.pkg['recommended_plan']['scenario_id'])['execution_plan']
        pending = next(x for x in plan if x['knob'] == 'max_clusters' and x['execution_state'] == 'PENDING_REEVALUATION')
        o['config']['max_clusters'] = pending['recommended_value']
        s = self.realize_case(o)
        self.assertEqual(s.current_state['realization_state'], 'NOT_APPLIED')
        self.assertFalse(s.economics.get('available', False))
        types = [e.event_type.value for e in self.ledger()]
        self.assertIn('INDEPENDENT_CHANGE_OBSERVED', types)

    # GR-021
    def test_gr_021_plan_b_migration_scenario_realization(self):
        alt = next(s for s in self.pkg['scenarios'] if s['scenario_type'] == 'WAREHOUSE_TYPE_MIGRATION')
        o = self.obs(); o['applied_scenario_id'] = alt['scenario_id']; o['config'] = package_config(self.pkg); o['config']['warehouse_type'] = 'SERVERLESS'
        o['economics']['expected_annual_savings_actual_scope'] = None
        s = self.realize_case(o)
        self.assertEqual(s.application['application_scope'], 'SCENARIO')
        self.assertEqual(s.application['implementation_conformance'], 'EXACT')
        self.assertEqual(s.economics['denominator_source'], 'PUBLISHER_SCENARIO')
        # The migration range is pre-canary: reported, never classified against.
        self.assertEqual((s.economics['classification_basis'], s.current_state['realization_state']), ('INFORMATIONAL', 'MONITORING'))

    # GR-022
    def test_gr_022_superseded_package_can_continue_applied_measurement(self):
        o = self.obs(); self.realize_case(o)
        o2 = self.later(o); o2['replacement_package_id'] = 'PKG-' + 'A'*24
        s = self.realize_case(o2)
        self.assertEqual(s.source_recommendation['disposition'], 'SUPERSEDED')
        self.assertEqual(s.application['state'], 'APPLIED')
        self.assertIn(s.current_state['realization_state'], {'REALIZED','UNDER_REALIZED','MONITORING'})

    # GR-023
    def test_gr_023_economic_handoff_uses_same_boundary_without_overlap(self):
        o = self.obs(); old = self.realize_case(o)
        boundary = _iso_add(o['observed_at_utc'], days=1)
        drift = self.later(o, days=1, new_application=True); drift['config']['min_clusters'] = 2; drift['economics'] = None
        old2 = self.realize_case(drift)
        self.assertEqual(old2.application['effective_to_utc'], boundary)
        # A replacement package starts its own interval at the same boundary.
        new_start = boundary
        self.assertEqual(old2.application['effective_to_utc'], new_start)

    # GR-024
    def test_gr_024_config_drift_closes_interval(self):
        o = self.obs(); self.realize_case(o)
        # a knob no step changes (the size back at LARGE would be a rollback, not a drift)
        d = self.later(o, days=2, new_application=True); d['config']['min_clusters'] = 2; d['economics'] = None
        s = self.realize_case(d)
        self.assertEqual(s.current_state['realization_state'], 'DRIFTED')
        self.assertEqual(s.application['state'], 'DRIFTED')
        self.assertEqual(s.regeneration['status'], 'REQUESTED')

    # GR-025
    def test_gr_025_rollback_preserves_history(self):
        o = self.obs(); self.realize_case(o)
        rb = self.later(o, days=2, new_application=True); rb['config'] = package_config(self.pkg); rb['rollback_observed'] = True; rb['economics'] = None
        s = self.realize_case(rb)
        self.assertEqual(s.current_state['realization_state'], 'ROLLED_BACK')
        self.assertGreater(s.history['entry_count'], 1)
        # back at the package's own configuration: no drift and nothing to regenerate
        types = [e.event_type.value for e in self.ledger()]
        self.assertIn('ROLLBACK_OBSERVED', types)
        self.assertNotIn('DRIFT_DETECTED', types)
        self.assertEqual(s.drift['state'], 'NONE')
        reasons = [r for e in self.ledger() if e.event_type == rc.EventType.REGENERATION_REQUESTED for r in e.payload['reasons']]
        self.assertNotIn('CONFIGURATION_DRIFT', reasons)

    # GR-026
    def test_gr_026_billing_restatement_appends_and_recomputes(self):
        o = self.obs(); first = self.realize_case(o)
        first_value = Decimal(first.economics['realized_savings_period'])
        o2 = self.later(o); o2['economics']['observed_new_cost'] = '9200'; o2['correction'] = {'type':'RESTATEMENT','key':'oct','version':2,'reason':'late billing','corrects_entry_id':None}
        s = self.realize_case(o2)
        self.assertEqual(s.history['correction_count'], 1)
        correction = next(e for e in self.ledger() if e.event_type == rc.EventType.BILLING_CORRECTION_RECORDED)
        self.assertIsNotNone(correction.corrects_entry_id)
        self.assertLess(Decimal(s.economics['realized_savings_period']), first_value)
        self.assertEqual(s.history['application_episode_count'], 1)

    # GR-027
    def test_gr_027_provider_price_effects_keep_economic_basis_separate(self):
        o = self.obs(); o['economics']['economic_basis'] = 'CONSTANT_PRICE'; self.realize_case(o)
        o2 = self.later(o); o2['economics']['economic_basis'] = 'POSTED_DOLLAR'; self.realize_case(o2)
        bases = [e.payload['economics']['economic_basis'] for e in self.ledger() if e.event_type == rc.EventType.ECONOMIC_MEASUREMENT_RECORDED]
        self.assertIn('CONSTANT_PRICE', bases); self.assertIn('POSTED_DOLLAR', bases)

    # GR-028
    def test_gr_028_workload_growth_uses_counterfactual_not_naive_before_after(self):
        o = self.obs(); o['economics'].update({'counterfactual_old_cost':'150000','observed_new_cost':'120000','expected_annual_savings_actual_scope':'365000','period_days':365})
        s = self.realize_case(o)
        self.assertEqual(Decimal(s.economics['realized_savings_period']), Decimal('30000'))

    # GR-029
    def test_gr_029_negative_realization_preserved(self):
        o = self.obs(); o['economics'].update({'counterfactual_old_cost':'10000','observed_new_cost':'11000','expected_annual_savings_actual_scope':'12000'})
        s = self.realize_case(o)
        self.assertEqual(Decimal(s.economics['realized_savings_period']), Decimal('-1000'))
        self.assertEqual(s.current_state['realization_state'], 'UNDER_REALIZED')

    # GR-030
    def test_gr_030_overlap_blocks_recommendation_attribution(self):
        o = self.obs(); o['overlap_count'] = 1
        s = self.realize_case(o)
        self.assertEqual(s.attribution['attribution_claim_status'], 'OBSERVABLE_ONLY')
        self.assertEqual(Decimal(s.attribution['recommendation_attributed_realized_savings']), 0)
        self.assertIn('PORTFOLIO_SCOPE_OVERLAP', s.attribution['hard_disqualifiers'])

    # GR-031
    def test_gr_031_independent_similar_change_not_optimizer_attributed(self):
        o = self.obs(); o['origin_attribution'] = 'INDEPENDENT'
        s = self.realize_case(o)
        self.assertEqual(s.application['implementation_conformance'], 'EXACT')
        self.assertEqual(s.attribution['origin_attribution'], 'INDEPENDENT')
        self.assertEqual(Decimal(s.attribution['recommendation_attributed_realized_savings']), 0)

    # GR-032
    def test_gr_032_duplicate_event_idempotent(self):
        o = self.obs(); a = self.realize_case(o); count = a.history['entry_count']; b = self.realize_case(o)
        self.assertEqual(count, b.history['entry_count'])
        self.assertEqual(a.snapshot_id, b.snapshot_id)

    # GR-033
    def test_gr_033_deterministic_replay(self):
        o = self.obs()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            sa = self.realize_case(o, out=Path(a)); sb = self.realize_case(o, out=Path(b))
            self.assertEqual(sa.snapshot_id, sb.snapshot_id)
            self.assertEqual(rc.canonical_json(sa.to_dict()), rc.canonical_json(sb.to_dict()))

    # GR-034
    def test_gr_034_policy_change_revalidates_without_rewriting_history(self):
        o = self.obs(); o['performance']['normalized_p95_runtime_regression_pct'] = '4.5'
        p1 = RealizerPolicy(max_normalized_p95_runtime_regression_pct=Decimal('5'))
        first = self.realize_case(o, policy=p1); self.assertEqual(first.validation['state'], 'PASSED')
        o2 = self.later(o)
        p2 = RealizerPolicy(max_normalized_p95_runtime_regression_pct=Decimal('4'))
        second = self.realize_case(o2, policy=p2)
        self.assertEqual(second.validation['state'], 'FAILED')
        validations = [e for e in self.ledger() if e.event_type == rc.EventType.VALIDATION_COMPLETED]
        self.assertGreaterEqual(len(validations), 2)
        self.assertNotEqual(validations[0].payload['policy_snapshot_id'], validations[-1].payload['policy_snapshot_id'])

    # GR-035
    def test_gr_035_churn_below_5pct_suppressed(self):
        self.assertFalse(savings_improvement_material('400000','412000', RealizerPolicy()))

    # GR-036
    def test_gr_036_material_improvement_eligible(self):
        self.assertTrue(savings_improvement_material('400000','430000', RealizerPolicy()))

    # GR-037
    def test_gr_037_conflicting_source_evidence_blocks_claim(self):
        o = self.obs(); o['evidence_quality'] = 'CONFLICTING'
        s = self.realize_case(o)
        self.assertEqual(s.validation['state'], 'NOT_READY')
        self.assertFalse(s.economics.get('available', False))

    # GR-038
    def test_gr_038_aws_modeled_not_cash(self):
        o = self.obs()
        # The components add up to the realized total: the DBU part is the rest of it.
        dbu = Decimal(o['economics']['counterfactual_old_cost']) - Decimal(o['economics']['observed_new_cost']) - Decimal('50')
        o['economics'].update({'dbu_realized':str(dbu),'aws_economic_realized':'50','aws_cash_realized':None,'aws_evidence_type':'MODELED'})
        s = self.realize_case(o)
        self.assertEqual(s.economics['aws_economic_realized'], '50.00')
        self.assertIsNone(s.economics['aws_cash_realized'])
        self.assertEqual(s.economics['aws_evidence_type'], 'MODELED')

    # GR-039
    def test_gr_039_cumulative_authoritative_not_blind_incremental_sum(self):
        x = authoritative_cumulative_realization('1000','810',['100','60','50'])
        self.assertEqual(Decimal(x['authoritative']), Decimal('190'))
        self.assertEqual(Decimal(x['diagnostic_incremental_sum']), Decimal('210'))
        self.assertEqual(Decimal(x['interaction_delta']), Decimal('-20'))

    # GR-040
    def test_gr_040_mature_ttm_is_earned_from_365_covered_days(self):
        # Two runs of 200 days each, the second starting where the first ended: realized to date adds both (6000.00), the
        # trailing year is the second window plus the latest 165 days of the first (200 x 20.00 + 165 x 10.00), and
        # the run-rate is the second window alone (4000.00 x 365 / 200). After the first run alone: 200 covered days, not mature.
        def run(window_start, window_end, observed, elapsed, old_cost, new_cost):
            o = self.obs()
            o.update({'window_start_utc': window_start, 'window_end_utc': window_end, 'observed_at_utc': observed,
                      'recorded_at_utc': observed, 'elapsed_days': elapsed})
            o['economics'].update({'period_days': 200, 'counterfactual_old_cost': old_cost, 'observed_new_cost': new_cost})
            return self.realize_case(o)
        first = run('2026-10-02T00:00:00Z', '2027-04-20T00:00:00Z', '2027-04-20T12:00:00Z', 200, '10000.00', '8000.00')
        self.assertNotEqual(first.maturity['state'], 'MATURE')
        self.assertEqual(first.economics['realized_savings_since_application'], '2000.00')
        self.assertEqual(first.economics['realized_to_date']['days_covered'], '200.00')
        self.assertIsNone(first.economics['mature_ttm_realized_savings'])
        s = run('2027-04-20T00:00:00Z', '2027-11-06T00:00:00Z', '2027-11-06T12:00:00Z', 400, '12000.00', '8000.00')
        self.assertEqual(s.maturity['state'], 'MATURE')
        self.assertEqual(s.economics['realized_savings_since_application'], '6000.00')
        self.assertEqual(s.economics['mature_ttm_realized_savings'], '5650.00')
        self.assertEqual(s.economics['annualized_realized_savings'], '7300.00')
        self.assertNotEqual(s.economics['mature_ttm_realized_savings'], s.economics['annualized_realized_savings'])


class GoldenRegistryTests(unittest.TestCase):
    def test_registry_lists_gr_001_to_040_and_each_test_exists(self):
        registry = json.loads((canonical.FIXTURES / 'golden-registry.json').read_text(encoding='utf-8'))
        ids = [s['scenario_id'] for s in registry['scenarios']]
        self.assertEqual(ids, [f'GR-{n:03d}' for n in range(1, 41)])
        self.assertEqual(registry['scenario_count'], 40)
        for s in registry['scenarios']:
            module, cls, method = s['test_id'].rsplit('.', 2)
            self.assertEqual((module, cls), ('tests.golden.realizer.test_golden_realization', 'RealizerGoldenTests'), s['test_id'])
            self.assertTrue(hasattr(RealizerGoldenTests, method), s['test_id'])


if __name__ == '__main__':
    unittest.main()
