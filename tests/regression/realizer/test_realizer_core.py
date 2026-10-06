"""Core behavior of the frozen single-file Realizer oracle, on the frozen v2.8-era PKG fixture."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical, oracle  # noqa: E402

realizer = oracle.engine()
rc = oracle.rc()
realization_package_schema = oracle.realizer_schema().realization_package_schema


class RealizerCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = canonical.load_scenario('example-exact')['package_doc']

    def test_source_package_is_not_mutated(self):
        pkg = copy.deepcopy(self.pkg)
        before = json.dumps(pkg, sort_keys=True)
        with tempfile.TemporaryDirectory() as td:
            realizer.realize(pkg, realizer.demo_observation(pkg), Path(td))
        self.assertEqual(before, json.dumps(pkg, sort_keys=True))

    def test_repeated_stable_observation_reuses_application_episode(self):
        pkg = copy.deepcopy(self.pkg)
        obs = realizer.demo_observation(pkg)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            a = realizer.realize(pkg, obs, out)
            later = copy.deepcopy(obs)
            later['observed_at_utc'] = realizer.utc_iso(realizer.dt.datetime.fromisoformat(obs['observed_at_utc'].replace('Z','+00:00')) + realizer.dt.timedelta(days=1))
            later['recorded_at_utc'] = later['observed_at_utc']
            later['window_end_utc'] = later['observed_at_utc']
            b = realizer.realize(pkg, later, out)
            self.assertEqual(a.application['active_episode_id'], b.application['active_episode_id'])
            self.assertEqual(b.history['application_episode_count'], 1)

    def test_snapshot_validates_against_schema(self):
        with tempfile.TemporaryDirectory() as td:
            s = realizer.realize(self.pkg, realizer.demo_observation(self.pkg), Path(td))
            jsonschema.validate(s.to_dict(), realization_package_schema())

    def test_legacy_compatibility_does_not_fabricate_unknown_actor(self):
        obs = realizer.demo_observation(self.pkg)
        obs['actor'] = None
        with tempfile.TemporaryDirectory() as td:
            s = realizer.realize(self.pkg, obs, Path(td))
            c = realizer.legacy_compatibility(s)
            self.assertEqual(c['status'], 'LOSSY')
            self.assertIn('LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN', c['reasons'])
            self.assertIsNone(s.application['actor'])

    def test_replay_through_reconstructs_prior_state(self):
        obs = realizer.demo_observation(self.pkg)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            full = realizer.realize(self.pkg, obs, out)
            created_time = self.pkg['provenance']['generated_at']
            prior = realizer.realize(self.pkg, None, out, replay_through=created_time)
            self.assertEqual(prior.current_state['realization_state'], 'NOT_APPLIED')
            self.assertNotEqual(prior.snapshot_id, full.snapshot_id)

    def test_snapshot_as_of_is_ledger_derived(self):
        obs = realizer.demo_observation(self.pkg)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            s = realizer.realize(self.pkg, obs, out)
            entries = realizer.load_ledger(realizer.ledger_path(out, s.realization_package_id))
            cp = realizer.ledger_checkpoint(entries)
            self.assertEqual(s.as_of_utc, cp['latest_recorded_at_utc'])

    def test_financial_quality_gates_block_strict_attribution_without_erasing_observed_value(self):
        obs = realizer.demo_observation(self.pkg)
        obs.update({
            'attributable_spend_pct': '90',
            'price_coverage_pct': '99',
            'currency_consistency_pct': '99',
            'correction_chain_complete': False,
            'material_cost_component_available': False,
        })
        with tempfile.TemporaryDirectory() as td:
            s = realizer.realize(self.pkg, obs, Path(td))
            hard = set(s.attribution['hard_disqualifiers'])
            self.assertIn('MATERIAL_SPEND_UNRECONCILED', hard)
            self.assertIn('PRICE_COVERAGE_INCOMPLETE', hard)
            self.assertIn('CURRENCY_MISMATCH', hard)
            self.assertIn('CORRECTION_CHAIN_INCOMPLETE', hard)
            self.assertIn('MATERIAL_COST_COMPONENT_UNAVAILABLE', hard)
            self.assertEqual(s.attribution['attribution_claim_status'], 'OBSERVABLE_ONLY')
            self.assertNotEqual(s.attribution['validated_observed_realization'], '0.00')
            self.assertEqual(s.attribution['recommendation_attributed_realized_savings'], '0.00')
            self.assertFalse(s.attribution['economic_quality_gates']['pass'])


if __name__ == '__main__':
    unittest.main()
