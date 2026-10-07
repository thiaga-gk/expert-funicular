"""Realizer estimator capability (src/realizer/estimator/estimator.py): ``run`` and its ``RealizationEstimate``.

For every run of the 41 scenarios, the counterfactual, measurement and billing-correction facts ``run`` decides,
recorded after the validation episode with the per-append duplicate suppression, are exactly the entries the engine
appended for those slots (a prefix of the stored baseline's ledger), and ``run`` is called only when validation is due. ``run`` never raises, writes and
logs nothing, and keeps a failure as ``halt`` together with the facts decided before it.
"""
import collections
import contextlib
import copy
import dataclasses
import logging
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import (  # noqa: E402
    Opaque, Untestable, compare_measurement_section, correction, fixture_package, observation_gate_open, run_sequence, staged_scenario_runs, tree,
)
from realizer.analyzer import analyzer  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.realization import CounterfactualRecord, RealizationEstimate  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.estimator import economics, estimator  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402

class EveryScenarioRunTests(unittest.TestCase):
    def test_estimated_facts_are_the_ledger_entries_for_every_run_of_every_scenario(self):
        seen = collections.Counter()
        for run in staged_scenario_runs():
            with self.subTest(scenario=run.scenario, run=run.index):
                result = compare_measurement_section(self, run)
                if result is None:
                    seen["not due"] += 1
                    continue
                _, _, _, _, estimate, _ = result
                seen["due"] += 1
                seen["cf recorded" if estimate.counterfactual.payload is not None else "cf not recorded"] += 1
                if estimate.measurement is None:
                    seen["economics unavailable"] += 1
                    self.assertFalse(estimate.economics["available"])
                    self.assertIsNone(estimate.correction)
                    continue
                economics = estimate.measurement.economics
                self.assertIs(economics, estimate.economics)
                self.assertEqual(list(economics)[-1], "quality_gates")
                self.assertIs(estimate.measurement.attribution["economic_quality_gates"], economics["quality_gates"])
                seen[f"state {economics['realization_state']}"] += 1
                seen[f"denominator {economics['denominator_source']}"] += 1
                seen[f"claim {estimate.measurement.attribution['attribution_claim_status']}"] += 1
                seen["correction" if estimate.correction is not None else "no correction"] += 1
        # (every scenario carries a counterfactual cost; the paths without one are RunTests and test_economics cases)
        for kind in ("not due", "due", "cf recorded", "correction", "no correction",
                     "state REALIZED", "state UNDER_REALIZED", "state VALIDATION_FAILED", "state BLOCKED", "state MONITORING",
                     "denominator PUBLISHER_CANDIDATE", "denominator REESTIMATED_APPLIED_SCOPE",
                     "claim CLAIMABLE", "claim OBSERVABLE_ONLY", "claim NOT_ATTRIBUTABLE"):
            self.assertGreater(seen[kind], 0, f"{kind}: {dict(seen)}")


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def stages(self, observation, out, package=None, policy=None, gate=True):
        """The stages' results up to the Reconciler; ``gate=False`` opens the loader's observation gate for a malformed observation."""
        with contextlib.nullcontext() if gate else observation_gate_open():
            context = build_context(RealizationRequest(package or self.pkg, observation, out, policy=policy))
        analysis = analyzer.run(context)
        governance = governor.run(context, analysis)
        return context, analysis, governance, reconciler.run(context, analysis, governance)

    def estimate(self, observation, out=Path("/nonexistent-out"), package=None, policy=None, gate=True):
        return estimator.run(*self.stages(observation, out, package, policy, gate))

    def test_nothing_is_estimated_unless_validation_is_due(self):
        cases = {
            "lineage only": None,
            "nothing applied": {**self.base, "config": package_config(self.pkg)},
            "not ready": {**self.base, "elapsed_days": 1},
            "contrary": {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}},
            "reconciliation halted": {**self.base, "rollback_requested": Untestable()},
        }
        for label, observation in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.estimate(observation, gate=False), RealizationEstimate())

    def test_unready_or_unapplied_input_the_estimate_needs_is_never_read(self):
        # nothing is estimated, so malformed performance and economics cannot fail the estimate
        observation = {**self.base, "elapsed_days": 1, "performance": {"normalized_p95_runtime_regression_pct": "abc"}, "economics": 5}
        self.assertEqual(self.estimate(observation, gate=False), RealizationEstimate())

    def test_estimate_of_the_demo_observation(self):
        estimate = self.estimate(self.base)
        self.assertIsNone(estimate.halt)
        self.assertTrue(estimate.validation_passed)
        self.assertIsInstance(estimate.counterfactual, CounterfactualRecord)
        self.assertTrue(estimate.counterfactual.counterfactual_id.startswith("CF-"))
        self.assertEqual(estimate.counterfactual.payload["counterfactual_old_cost"], "10000.00")
        demo = self.base["economics"]   # the demo realizes the applied step's published savings pro rata over its 30 days
        self.assertEqual(estimate.economics["realized_savings_period"], str(Decimal(demo["counterfactual_old_cost"]) - Decimal(demo["observed_new_cost"])))
        self.assertTrue(estimate.measurement.measurement_id.startswith("MEAS-"))
        self.assertEqual(estimate.measurement.counterfactual_id, estimate.counterfactual.counterfactual_id)
        self.assertEqual(estimate.measurement.correction_status, "ORIGINAL")
        self.assertIsNone(estimate.correction)

    def test_run_is_pure_it_writes_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(self.pkg, self.base, out)
            before = tree(out)
            corrected = {**self.base, "correction": correction()}
            stages = self.stages(corrected, out)
            with self.assertNoLogs("realizer", logging.DEBUG):
                estimate = estimator.run(*stages)
            self.assertIsNotNone(estimate.correction)
            self.assertEqual(tree(out), before)

    def test_estimate_and_its_parts_are_frozen(self):
        estimate = self.estimate({**self.base, "correction": correction()})
        for value in (estimate, estimate.counterfactual, estimate.measurement, estimate.correction):
            with self.subTest(part=type(value).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, dataclasses.fields(value)[0].name, None)

    def test_economics_are_a_new_mapping_with_the_quality_gates_last(self):
        context, analysis, governance, reconciliation = self.stages(self.base, Path("/nonexistent-out"))
        estimate = estimator.run(context, analysis, governance, reconciliation)
        gates = governance.economic_quality.get()
        bare = economics.calculate_economics(self.pkg, reconciliation.active.payload, self.base, context.policy, True, True)
        self.assertEqual(estimate.economics, {**bare, "quality_gates": gates})
        self.assertEqual(list(estimate.economics), list(bare) + ["quality_gates"])
        self.assertIs(estimate.economics["quality_gates"], gates)
        self.assertEqual(estimator.run(context, analysis, governance, reconciliation), estimate)  # nothing was mutated

    def test_run_never_raises_and_halts_with_the_exception_after_the_facts_decided_before_it(self):
        econ = self.base["economics"]
        cases = {
            # (observation patch, the last fact decided before the failure)
            "performance": ({"performance": {"normalized_p95_runtime_regression_pct": "abc"}}, None),
            "counterfactual eligibility": ({"economics": 5}, "validation_passed"),
            "counterfactual cost": ({"economics": {**econ, "counterfactual_old_cost": "abc"}}, "validation_passed"),
            "counterfactual id": ({"workload": Opaque()}, "validation_passed"),
            "economics": ({"economics": {**econ, "observed_new_cost": "abc"}}, "counterfactual"),
            "quality gates": ({"financial_quality": {**self.base["financial_quality"], "attributable_spend_pct": "NaN"}}, "counterfactual"),
            "attribution": ({"overlap_count": "abc"}, "economics"),
            "measurement id": ({"correction": 5}, "economics"),
            "correction": ({"correction": {"corrects_entry_id": Untestable()}}, "measurement"),
        }
        order = ["validation_passed", "counterfactual", "economics", "measurement", "correction"]
        for label, (patch, last) in cases.items():
            observation = {**self.base, **patch}
            with self.subTest(case=label):
                estimate = self.estimate(observation, gate=False)
                self.assertIsInstance(estimate.halt, Exception)
                (raised,), _ = run_sequence(self.pkg, [observation], observation_gate=False)
                self.assertEqual((type(estimate.halt).__name__, str(estimate.halt)), raised)
                expect.check(self, label, (type(estimate.halt).__name__, str(estimate.halt)))
                decided = order[:order.index(last) + 1] if last else []
                for name in order:
                    present = getattr(estimate, name) is not None
                    self.assertEqual(present, name in decided, name)

    def test_unavailable_economics_stop_the_estimate_without_reading_the_quality_gates(self):
        observation = {**self.base, "economics": {**self.base["economics"], "counterfactual_old_cost": None},
                       "financial_quality": {**self.base["financial_quality"], "attributable_spend_pct": "NaN"}, "overlap_count": "abc",
                       "correction": {"corrects_entry_id": Untestable()}}
        estimate = self.estimate(observation, gate=False)
        self.assertIsNone(estimate.halt)
        self.assertFalse(estimate.economics["available"])
        self.assertIsNone(estimate.counterfactual.payload)
        self.assertEqual(estimate.counterfactual.counterfactual_id,
                         economics.make_cf_id(self.stages(observation, Path("/x"), gate=False)[3].active.application_episode_id, observation, self.pkg, RealizerPolicy()))
        self.assertIsNone(estimate.measurement)
        self.assertIsNone(estimate.correction)

    def test_a_currency_other_than_the_packages_is_never_claimable(self):
        base = {**self.base, "elapsed_days": 30}  # past the size canary's guard window, so only the currency can block
        econ = base["economics"]
        self.assertEqual(self.estimate(base).measurement.attribution["attribution_claim_status"], "CLAIMABLE")
        euro = self.estimate({**base, "economics": {**econ, "currency": "EUR"}})
        self.assertIsNone(euro.halt)
        gate = euro.economics["quality_gates"]["currency_consistency"]
        self.assertEqual((gate["pass"], gate["currency"], gate["package_currency"]), (False, "EUR", "USD"))
        self.assertFalse(euro.economics["quality_gates"]["pass"])
        attribution = euro.measurement.attribution
        self.assertIn("CURRENCY_MISMATCH", attribution["hard_disqualifiers"])
        self.assertNotEqual(attribution["attribution_claim_status"], "CLAIMABLE")
        self.assertEqual(attribution["recommendation_attributed_realized_savings"], "0.00")
        self.assertEqual(euro.economics["currency"], "EUR", "the observed value is kept in its own currency, not erased")

    def test_a_correlated_only_counterfactual_is_not_claim_eligible(self):
        econ = self.base["economics"]
        for method in ("CORRELATED_ONLY", "NO_DEFENSIBLE_COUNTERFACTUAL"):
            with self.subTest(method=method):
                estimate = self.estimate({**self.base, "economics": {**econ, "counterfactual_method": method}})
                self.assertIsNone(estimate.halt)
                self.assertEqual(estimate.economics["realization_state"], "BLOCKED")
                self.assertEqual(estimate.economics["successful_realized_savings"], "0.00")
                self.assertIsNone(estimate.economics["annualized_realized_savings"])
                self.assertIsNone(estimate.economics["realization_ratio"])
                self.assertIn("NO_DEFENSIBLE_COUNTERFACTUAL", estimate.measurement.attribution["hard_disqualifiers"])
                self.assertNotEqual(estimate.measurement.attribution["attribution_claim_status"], "CLAIMABLE")

    def test_billing_correction_falls_back_to_the_previous_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
            corrected = {**self.base, "correction": correction()}
            context, analysis, governance, reconciliation = self.stages(corrected, out)
            estimate = estimator.run(context, analysis, governance, reconciliation)
            prior = analysis.history.prior_measurement
            self.assertIsNotNone(prior)
            self.assertEqual(estimate.correction.corrects_entry_id, prior.ledger_entry_id)
            self.assertEqual(estimate.correction.payload, {
                "correction_key": "c1", "type": "RESTATEMENT", "version": 2, "reason": None,
                "measurement_id": estimate.measurement.measurement_id, "corrects_entry_id": prior.ledger_entry_id,
            })
            self.assertEqual(estimate.measurement.correction_status, "RESTATEMENT")


if __name__ == "__main__":
    unittest.main()
