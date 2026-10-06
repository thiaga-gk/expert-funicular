import json
import tempfile
import unittest
from pathlib import Path

import publisher as p
from contracts import reporter_contract as rc


class PublisherV28RegressionTests(unittest.TestCase):
    def _package(self):
        evidence = p.synthetic_demo_evidence(1)
        report = p.build_report(evidence, p.Thresholds())
        packages = p.build_locked_recommendation_packages(report, evidence, p.Thresholds())
        self.assertEqual(len(packages), 1)
        pkg = packages[0]
        pkg.validate()
        return pkg

    def test_version_is_v28(self):
        self.assertEqual(p.PUBLISHER_VERSION, "2.8.0")
        self.assertEqual(p.BUNDLE_MODEL_VERSION, "stateful-resource-bundle-v2.8")

    def test_demo_package_has_seven_candidates_and_two_scenarios(self):
        pkg = self._package()
        self.assertEqual(len(pkg.candidates), 7)
        self.assertEqual(len(pkg.scenarios), 2)

    def test_recommendation_package_is_deterministic(self):
        a = self._package()
        b = self._package()
        self.assertEqual(a.recommendation_package_id, b.recommendation_package_id)
        # generated_at is presentation/provenance time and not identity authority.
        self.assertEqual(a.provenance.package_hash_full_sha256, b.provenance.package_hash_full_sha256)

    def test_realization_initializes_not_applied(self):
        pkg = self._package()
        self.assertEqual(pkg.realization.status, rc.RealizationStatus.NOT_APPLIED)
        self.assertEqual(pkg.realization.realization_records, [])

    def test_plan_a_downstream_steps_are_pending_after_invalidating_size(self):
        pkg = self._package()
        scn = next(s for s in pkg.scenarios if s.scenario_type == rc.ScenarioType.IN_PLACE_OPTIMIZATION)
        candidate_steps = [s for s in scn.execution_plan if s.step_type == rc.PlanStepType.CANDIDATE]
        self.assertEqual(candidate_steps[0].knob, rc.TunableKnob.WAREHOUSE_SIZE)
        self.assertEqual(candidate_steps[0].execution_state, rc.ExecutionState.READY_FOR_CANARY)
        self.assertTrue(candidate_steps[0].invalidates)
        self.assertTrue(all(s.execution_state == rc.ExecutionState.PENDING_REEVALUATION for s in candidate_steps[1:]))
        self.assertTrue(any(s.step_type == rc.PlanStepType.REMEASURE for s in scn.execution_plan))

    def test_pending_reevaluation_steps_not_priced_into_authoritative_plan_a(self):
        pkg = self._package()
        scn = next(s for s in pkg.scenarios if s.scenario_type == rc.ScenarioType.IN_PLACE_OPTIMIZATION)
        size_cand = next(c for c in pkg.candidates if c.knob == rc.TunableKnob.WAREHOUSE_SIZE)
        # With the v2.8 bundle-estimator fix, authoritative Plan A economics stop at the first invalidating
        # applied/canary step; pending downstream knobs are contingent and unpriced.
        self.assertAlmostEqual(
            scn.portfolio_economics.savings_365d.total.base,
            size_cand.independent_economics.savings_365d.total.base,
            places=2,
        )

    def test_package_economics_not_candidate_sum(self):
        pkg = self._package()
        summed = sum((c.independent_economics.savings_365d.total.base or 0.0) for c in pkg.candidates if c.independent_economics.savings_365d)
        self.assertNotAlmostEqual(pkg.package_economics.scenario_envelope.recommended_base.savings_365d.total, summed, places=2)

    def test_schema_validation_accepts_demo(self):
        pkg = self._package()
        payload = rc.to_primitive(pkg)
        p.validate_locked_package_schema(payload)

    def test_cli_packager_emits_one_package_per_identity(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            evidence = p.synthetic_demo_evidence(1)
            report = p.build_report(evidence, p.Thresholds())
            packages = p.build_locked_recommendation_packages(report, evidence, p.Thresholds())
            p.package_locked_contract(out, packages, evidence, generate_workbook=False)
            self.assertEqual(len(packages), 1)
            files = list((out / "packages").glob("*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text())
            self.assertEqual(payload["contract_version"], "1.0.0")
            self.assertEqual(len(payload["candidates"]), 7)


if __name__ == "__main__":
    unittest.main()
