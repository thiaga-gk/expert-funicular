from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from publisher.contracts.reporter_contract import (  # noqa: E402
    CONTRACT_VERSION,
    BoundName,
    ComponentRanges,
    EstimationStatus,
    ExecutionState,
    PricingBasis,
    StepProjection,
    Confidence,
    ConfidenceLevel,
    Range3,
    RecommendationStatus,
    RealizationStatus,
    canonical_json_for_hash,
    content_id,
)
from publisher.contracts.reporter_schema import build_schema
from examples.build_contract_example import build_package



class ReporterContractTests(unittest.TestCase):
    def test_example_dataclass_validates(self) -> None:
        package = build_package()
        package.validate()
        self.assertEqual(package.contract_version, CONTRACT_VERSION)

    def test_example_json_validates_against_schema(self) -> None:
        schema = json.loads((ROOT / "schema/recommendation-package.schema.json").read_text())
        data = json.loads((ROOT / "examples/recommendation-package.example.json").read_text())
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data),
            key=lambda e: list(e.path),
        )
        self.assertEqual([], errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors))

    def test_generated_schema_matches_committed_schema(self) -> None:
        committed = json.loads((ROOT / "schema/recommendation-package.schema.json").read_text())
        self.assertEqual(build_schema(), committed)

    def test_cluster_limits_match_databricks_and_the_publisher(self) -> None:
        # Databricks: max_num_clusters <= 40 and min_num_clusters <= min(max_num_clusters, 30).
        props = build_schema()["$defs"]["CurrentConfig"]["properties"]
        self.assertEqual((props["min_clusters"]["minimum"], props["min_clusters"]["maximum"]), (1, 30))
        self.assertEqual((props["max_clusters"]["minimum"], props["max_clusters"]["maximum"]), (1, 40))
        from publisher.config.defaults import MAX_CLUSTERS_SUPPORTED_LIMIT, MIN_CLUSTERS_SUPPORTED_LIMIT
        self.assertEqual(MIN_CLUSTERS_SUPPORTED_LIMIT, props["min_clusters"]["maximum"])
        self.assertEqual(MAX_CLUSTERS_SUPPORTED_LIMIT, props["max_clusters"]["maximum"])

    def test_min_clusters_above_thirty_is_rejected_by_schema_and_validator(self) -> None:
        schema = json.loads((ROOT / "schema/recommendation-package.schema.json").read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        data = json.loads((ROOT / "examples/recommendation-package.example.json").read_text())
        config = build_package().current_config
        for minimum, ok in ((30, True), (31, False)):
            with self.subTest(min_clusters=minimum):
                data["current_config"]["min_clusters"] = minimum
                data["current_config"]["max_clusters"] = 40
                schema_errors = [e for e in validator.iter_errors(data) if list(e.path)[:2] == ["current_config", "min_clusters"]]
                self.assertEqual(not schema_errors, ok)
                candidate = dataclasses.replace(config, min_clusters=minimum, max_clusters=40)
                if ok:
                    candidate.validate()
                else:
                    with self.assertRaises(ValueError):
                        candidate.validate()

    def test_ids_are_deterministic(self) -> None:
        a = build_package()
        b = build_package()
        self.assertEqual(a.recommendation_package_id, b.recommendation_package_id)
        self.assertEqual([x.candidate_id for x in a.candidates], [x.candidate_id for x in b.candidates])
        self.assertEqual([x.scenario_id for x in a.scenarios], [x.scenario_id for x in b.scenarios])
        self.assertEqual(a.provenance.evidence_fingerprint, b.provenance.evidence_fingerprint)

    def test_generated_at_does_not_change_package_id(self) -> None:
        a = build_package()
        b = dataclasses.replace(
            a,
            provenance=dataclasses.replace(a.provenance, generated_at="2026-10-02T01:00:00Z"),
        )
        self.assertEqual(a.expected_package_id(), b.expected_package_id())

    def test_evidence_change_changes_package_id(self) -> None:
        a = build_package()
        first_key = next(iter(a.evidence))
        changed_record = dataclasses.replace(a.evidence[first_key], value=999.0)
        changed_evidence = dict(a.evidence)
        changed_evidence[first_key] = changed_record
        # Package id is tied to the declared evidence fingerprint.  A reviewer must
        # refresh the fingerprint when evidence changes; the helper catches stale state.
        b = dataclasses.replace(a, evidence=changed_evidence)
        self.assertNotEqual(b.evidence_fingerprint_expected(), a.provenance.evidence_fingerprint)

    def test_package_envelope_base_comes_from_recommended_scenario(self) -> None:
        package = build_package()
        envelope = package.package_economics.scenario_envelope
        self.assertEqual(package.recommended_plan.scenario_id, envelope.recommended_base.scenario_id)
        self.assertEqual(BoundName.BASE, envelope.recommended_base.bound)

    def test_package_economics_is_total_envelope_not_component_range(self) -> None:
        package = build_package()
        rng = package.package_economics.estimated_savings_365d
        self.assertEqual(4000, rng.low)
        self.assertEqual(6500, rng.base)
        self.assertEqual(21793.82, rng.high)
        # Per-bound DBU/AWS attribution stays attached to the source scenario.
        self.assertEqual(3000, package.package_economics.scenario_envelope.minimum_savings.savings_365d.dbu)
        self.assertEqual(1000, package.package_economics.scenario_envelope.minimum_savings.savings_365d.aws)

    def test_candidate_status_counts_match_assessment(self) -> None:
        package = build_package()
        counts = {status: 0 for status in RecommendationStatus}
        for candidate in package.candidates:
            counts[candidate.status] += 1
        summary = package.assessment.summary
        self.assertEqual(counts[RecommendationStatus.ACTIONABLE], summary.actionable_count)
        self.assertEqual(counts[RecommendationStatus.TEST_REQUIRED], summary.test_required_count)
        self.assertEqual(counts[RecommendationStatus.NO_CHANGE], summary.no_change_count)

    def test_realization_is_present_even_before_application(self) -> None:
        package = build_package()
        self.assertEqual(RealizationStatus.NOT_APPLIED, package.realization.status)
        self.assertEqual([], package.realization.realization_records)

    # --- contract 1.1.0: authoritative headline vs contingent per-step upside (audit H1) -------------------
    @staticmethod
    def _plan_a(package):
        return next(s for s in package.scenarios if s.scenario_name.startswith("Plan A"))

    @staticmethod
    def _with_scenario(package, scenario):
        return dataclasses.replace(package, scenarios=[scenario if s.scenario_id == scenario.scenario_id else s for s in package.scenarios])

    def test_example_prices_pending_steps_contingent_and_headline_authoritative(self) -> None:
        plan = self._plan_a(build_package())
        steps = [x for x in plan.execution_plan if x.projected_economics is not None]
        for step in steps:
            pending = step.execution_state == ExecutionState.PENDING_REEVALUATION
            self.assertEqual(pending, step.projected_economics.pricing_basis == PricingBasis.CONTINGENT_PENDING_REEVALUATION)
        last_auth = [x for x in steps if x.projected_economics.pricing_basis == PricingBasis.AUTHORITATIVE][-1]
        self.assertEqual(last_auth.projected_economics.running_savings_365d, plan.portfolio_economics.savings_365d)
        self.assertGreater(plan.portfolio_economics.contingent_savings_365d.total.base, plan.portfolio_economics.savings_365d.total.base)

    def test_pending_step_priced_authoritative_is_rejected(self) -> None:
        package = build_package()
        plan = self._plan_a(package)
        pending = next(x for x in plan.execution_plan if x.execution_state == ExecutionState.PENDING_REEVALUATION)
        bad = dataclasses.replace(pending, projected_economics=StepProjection(PricingBasis.AUTHORITATIVE, pending.projected_economics.running_savings_365d))
        steps = [bad if x.step == pending.step else x for x in plan.execution_plan]
        with self.assertRaisesRegex(ValueError, "PENDING_REEVALUATION step must be priced CONTINGENT"):
            self._with_scenario(package, dataclasses.replace(plan, execution_plan=steps)).validate()

    def test_scenario_headline_must_equal_last_authoritative_step(self) -> None:
        package = build_package()
        plan = self._plan_a(package)
        leaked = dataclasses.replace(plan.portfolio_economics, savings_365d=plan.portfolio_economics.contingent_savings_365d)
        with self.assertRaisesRegex(ValueError, "last authoritative step"):
            self._with_scenario(package, dataclasses.replace(plan, portfolio_economics=leaked)).validate()

    def test_contingent_economics_require_a_pending_step_and_both_fields(self) -> None:
        package = build_package()
        plan_b = next(s for s in package.scenarios if s.scenario_name.startswith("Plan B"))
        plan_a = self._plan_a(package)
        econ = dataclasses.replace(
            plan_b.portfolio_economics,
            contingent_estimated_cost_365d=plan_a.portfolio_economics.contingent_estimated_cost_365d,
            contingent_savings_365d=plan_a.portfolio_economics.contingent_savings_365d,
        )
        with self.assertRaisesRegex(ValueError, "require at least one PENDING_REEVALUATION step"):
            self._with_scenario(package, dataclasses.replace(plan_b, portfolio_economics=econ)).validate()
        half = dataclasses.replace(plan_a.portfolio_economics, contingent_estimated_cost_365d=None)
        with self.assertRaisesRegex(ValueError, "published together or both null"):
            self._with_scenario(package, dataclasses.replace(plan_a, portfolio_economics=half)).validate()

    def test_contingent_savings_must_match_last_pending_step(self) -> None:
        package = build_package()
        plan = self._plan_a(package)
        shifted = ComponentRanges(None, None, Range3(1.0, 2.0, 3.0))
        econ = dataclasses.replace(plan.portfolio_economics, contingent_savings_365d=shifted)
        with self.assertRaisesRegex(ValueError, "last pending step"):
            self._with_scenario(package, dataclasses.replace(plan, portfolio_economics=econ)).validate()

    def test_unavailable_candidate_economics_must_be_null_not_zero(self) -> None:
        package = build_package()
        candidate = next(c for c in package.candidates if c.independent_economics.savings_365d.total is not None)
        econ = dataclasses.replace(candidate.independent_economics, estimation_status=EstimationStatus.UNAVAILABLE)
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE estimate must have null ranges"):
            econ.validate()
        null = ComponentRanges(None, None, None)
        dataclasses.replace(econ, estimated_cost_365d=null, savings_365d=null).validate()  # null is fine

    def test_unavailable_scenario_economics_must_be_null_not_zero(self) -> None:
        plan = self._plan_a(build_package())
        econ = dataclasses.replace(plan.portfolio_economics, estimation_status=EstimationStatus.UNAVAILABLE)
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE estimate must have null ranges"):
            econ.validate()
        null = ComponentRanges(None, None, None)
        dataclasses.replace(
            econ, estimated_cost_365d=null, savings_365d=null, contingent_estimated_cost_365d=None, contingent_savings_365d=None
        ).validate()

    def test_range_order_is_enforced(self) -> None:
        with self.assertRaises(ValueError):
            Range3(10, 5, 20).validate(field_name="bad")

    def test_unknown_confidence_requires_null_score(self) -> None:
        Confidence(None, ConfidenceLevel.UNKNOWN).validate()
        with self.assertRaises(ValueError):
            Confidence(0.8, ConfidenceLevel.UNKNOWN).validate()

    def test_canonical_hash_normalizes_numeric_spelling(self) -> None:
        self.assertEqual(
            canonical_json_for_hash({"x": 1.0, "y": [2.5000]}),
            canonical_json_for_hash({"x": 1, "y": [2.5]}),
        )

    def test_content_id_uses_24_hex_display_suffix(self) -> None:
        value = content_id("TEST", {"a": 1})
        self.assertRegex(value, r"^TEST-[A-F0-9]{24}$")


if __name__ == "__main__":
    unittest.main()
