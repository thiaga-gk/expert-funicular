from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from publisher import publisher
from publisher.common import ids  # noqa: E402
from publisher.analyzer.collector.demo import DEMO_FLEET, demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.common.ids import epoch_id  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.reporter.json_reporter import package_locked_contract  # noqa: E402
from publisher.reviewer.package import validate_locked_package_schema  # noqa: E402
from tests.golden import case_names, load_case
from publisher.contracts import reporter_contract as rc


class LockedReporterIntegrationTests(unittest.TestCase):
    def _packages(self):
        evidence = demo_evidence()
        thresholds = Thresholds()
        packages = publisher.build_recommendation_packages(evidence, thresholds)
        return evidence, thresholds, packages

    def test_demo_emits_exact_locked_contract(self):
        _, _, packages = self._packages()
        self.assertEqual(len(packages), 1)
        package = packages[0]
        package.validate()
        payload = package.to_dict()
        validate_locked_package_schema(payload)
        self.assertEqual(
            set(payload),
            {
                "recommendation_package_id", "contract_version", "warehouse", "current_config",
                "workload_profile", "cost_baseline", "assessment", "evidence", "candidates",
                "scenarios", "recommended_plan", "package_economics", "realization", "provenance",
                "cost_profile", "observed_change", "carried_forward_plan", "run_scope",
            },
        )
        self.assertEqual(payload["contract_version"], "3.1.0")

    def test_package_base_is_authoritative_and_contingent_upside_is_separate(self):
        """Audit H1: steps awaiting re-evaluation never feed the package base; their upside is published apart."""
        _, _, packages = self._packages()
        package = packages[0]
        plan = next(s for s in package.scenarios if s.scenario_id == package.recommended_plan.scenario_id)
        econ = plan.portfolio_economics
        pending = [x for x in plan.execution_plan if x.execution_state == rc.ExecutionState.PENDING_REEVALUATION]
        self.assertTrue(pending)
        for step in plan.execution_plan:
            if step.projected_economics is not None:
                self.assertEqual(
                    step.projected_economics.pricing_basis == rc.PricingBasis.CONTINGENT_PENDING_REEVALUATION,
                    step in pending,
                )
        # The package base is exactly the authoritative scenario base, with no pending-step savings in it.
        self.assertEqual(package.package_economics.estimated_savings_365d.base, econ.savings_365d.total.base)
        self.assertEqual(package.package_economics.scenario_envelope.recommended_base.savings_365d.total, econ.savings_365d.total.base)
        self.assertGreater(econ.contingent_savings_365d.total.base, econ.savings_365d.total.base)
        # Round-trips through JSON and the committed Draft 2020-12 schema.
        payload = package.to_dict()
        validate_locked_package_schema(payload)
        plan_json = next(x for x in payload["scenarios"] if x["scenario_id"] == plan.scenario_id)
        self.assertIsNotNone(plan_json["portfolio_economics"]["contingent_savings_365d"])
        self.assertTrue(any(
            (st["projected_economics"] or {}).get("pricing_basis") == "CONTINGENT_PENDING_REEVALUATION"
            for st in plan_json["execution_plan"]
        ))

    def test_content_ids_and_full_hash_are_recomputable(self):
        _, _, packages = self._packages()
        package = packages[0]
        self.assertEqual(package.recommendation_package_id, package.expected_package_id())
        self.assertEqual(package.provenance.package_hash_full_sha256, package.full_package_hash())
        self.assertTrue(package.recommendation_package_id.startswith("PKG-"))
        self.assertTrue(all(c.candidate_id.startswith("CAND-") for c in package.candidates))
        self.assertTrue(all(s.scenario_id.startswith("SCN-") for s in package.scenarios))
        self.assertTrue(all(k.startswith("EVID-") for k in package.evidence))

    def test_package_id_and_full_hash_are_recomputable_from_the_serialized_package(self):
        """A consumer (the Realizer) recomputes the id and full hash from the JSON it reads, with the dataclass's payload."""
        evidence = demo_evidence() + synthetic_demo_evidence()
        packages = publisher.build_recommendation_packages(evidence, Thresholds())
        self.assertTrue(any(p.observed_change is not None for p in packages))
        for package in packages:
            with self.subTest(warehouse=package.warehouse.warehouse_id):
                document = json.loads(json.dumps(package.to_dict(), sort_keys=True))
                payload = rc.package_hash_payload_from_document(document)
                self.assertEqual(rc.sha256_hex(payload), rc.sha256_hex(package.package_hash_payload()))
                self.assertEqual(rc.content_id("PKG", payload), package.recommendation_package_id)
                self.assertEqual(rc.sha256_hex(payload), package.provenance.package_hash_full_sha256)

    def test_same_evidence_produces_identical_locked_package(self):
        evidence = demo_evidence()
        thresholds = Thresholds()
        a = publisher.build_recommendation_packages(evidence, thresholds)[0]
        b = publisher.build_recommendation_packages(evidence, thresholds)[0]
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(a.recommendation_package_id, b.recommendation_package_id)

    def test_package_economics_is_scenario_envelope_not_candidate_sum(self):
        _, _, packages = self._packages()
        package = packages[0]
        pe = package.package_economics
        self.assertIsNotNone(pe.estimated_savings_365d)
        assessable = [s for s in package.scenarios if s.scenario_id in pe.included_scenario_ids]
        expected_low = min(s.portfolio_economics.savings_365d.total.low for s in assessable if s.portfolio_economics.savings_365d.total)
        expected_high = max(s.portfolio_economics.savings_365d.total.high for s in assessable if s.portfolio_economics.savings_365d.total)
        recommended = next(s for s in package.scenarios if s.scenario_id == package.recommended_plan.scenario_id)
        expected_base = recommended.portfolio_economics.savings_365d.total.base
        self.assertEqual(pe.estimated_savings_365d, rc.Range3(expected_low, expected_base, expected_high))
        candidate_sum = sum(
            c.independent_economics.savings_365d.total.base
            for c in package.candidates
            if c.independent_economics.savings_365d.total is not None
        )
        self.assertNotEqual(round(candidate_sum, 2), round(pe.estimated_savings_365d.base, 2))

    def test_realization_is_present_and_not_applied(self):
        _, _, packages = self._packages()
        realization = packages[0].realization
        self.assertEqual(realization.status, rc.RealizationStatus.NOT_APPLIED)
        self.assertEqual(realization.realization_records, [])
        self.assertEqual(realization.aggregate.measured_candidates, 0)

    def test_canonical_packager_writes_locked_json_packages_plus_workbook_projection(self):
        evidence, thresholds, packages = self._packages()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            paths = package_locked_contract(out, packages, evidence)
            package_files = sorted((out / "packages").glob("*.json"))
            self.assertEqual(len(package_files), 1)
            payload = json.loads(package_files[0].read_text())
            validate_locked_package_schema(payload)
            self.assertNotIn("report.json", {p.name for p in out.iterdir()})
            self.assertIn("workbook.xlsx", {p.name for p in out.iterdir()})
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["reporter_contract_version"], "3.1.0")
            self.assertEqual(manifest["workbook_contract_version"], "1.6.0")
            self.assertEqual(manifest["workbook_status"], "GENERATED")
            self.assertIn("workbook", paths)
            self.assertIn("manifest", paths)

    def test_multiwarehouse_run_emits_one_contract_package_per_identity(self):
        base = demo_evidence()[0]
        second = replace(
            base,
            workspace_id="9999999999999999",
            warehouse_id="wh-demo-002",
            warehouse_name="BI - Marketing",
            current_epoch_id=epoch_id("9999999999999999", "wh-demo-002", base.current_epoch.epoch_start),
            epochs=[
                replace(
                    ep,
                    workspace_id="9999999999999999",
                    warehouse_id="wh-demo-002",
                    warehouse_name="BI - Marketing",
                    epoch_id=epoch_id("9999999999999999", "wh-demo-002", ep.epoch_start),
                )
                for ep in base.epochs
            ],
        )
        evidence = [base, second]
        thresholds = Thresholds()
        packages = publisher.build_recommendation_packages(evidence, thresholds)
        self.assertEqual(len(packages), 2)
        self.assertEqual(len({p.recommendation_package_id for p in packages}), 2)
        self.assertEqual({(p.warehouse.workspace_id, p.warehouse.warehouse_id) for p in packages}, {
            (base.workspace_id, base.warehouse_id),
            (second.workspace_id, second.warehouse_id),
        })

    def test_all_golden_scenarios_adapt_to_locked_contract(self):
        for name in case_names():
            payload = load_case(name)
            if "evidence" not in payload:
                continue  # collector-level fixture (raw snapshots), covered by tests/golden/test_golden_publisher.py
            evidence = [evidence_from_dict(x) for x in payload["evidence"]]
            thresholds = Thresholds()
            packages = publisher.build_recommendation_packages(evidence, thresholds)
            self.assertGreaterEqual(len(packages), 1, name)
            for package in packages:
                package.validate()
                validate_locked_package_schema(package.to_dict())


class PublicIdentityTests(unittest.TestCase):
    """Public IDs come only from the contract helpers, never from the internal join keys (publisher.common.ids)."""

    INTERNAL_KEYS = (("epoch_id", "ZZEPOCH-"), ("finding_id", "ZZFND-"), ("recommendation_id", "ZZREC-"), ("scenario_id", "ZZSCN-"))

    @staticmethod
    def _unrelated(prefix):
        return lambda *args: prefix + hashlib.md5(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:11].upper()

    @staticmethod
    def _fleet():
        evidence = demo_evidence() + synthetic_demo_evidence()
        packages = publisher.build_recommendation_packages(evidence, Thresholds())
        return evidence, [package.to_dict() for package in packages]

    def test_public_ids_ignore_internal_join_keys(self):
        baseline_evidence, baseline = self._fleet()
        with ExitStack() as stack:
            mocks = []
            for name, prefix in self.INTERNAL_KEYS:
                mock = MagicMock(side_effect=self._unrelated(prefix))
                original = getattr(ids, name)
                # Patch the key wherever a publisher module imported it, not only in the facade.
                for module in [m for k, m in sorted(sys.modules.items()) if k == "publisher" or k.startswith("publisher.")]:
                    if getattr(module, name, None) is original:
                        stack.enter_context(patch.object(module, name, mock))
                mocks.append(mock)
            scrambled_evidence, scrambled = self._fleet()
        # The patch must really have changed the internal keys, or this test proves nothing.
        self.assertTrue(all(m.called for m in mocks))
        self.assertFalse(any(e.current_epoch_id.startswith("ZZ") for e in baseline_evidence))
        self.assertTrue(all(e.current_epoch_id.startswith("ZZEPOCH-") for e in scrambled_evidence))
        self.assertEqual(len(baseline), 1 + len(DEMO_FLEET))
        self.assertEqual(scrambled, baseline)


if __name__ == "__main__":
    unittest.main()
