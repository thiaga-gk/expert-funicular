"""Realizer reviewer capability (src/realizer/reviewer/): ``run``, the validation episode and the recognition status.

For every run of the 41 scenarios, the validation episode ``run`` assembles and the recognition status of the
measurement, recorded with the oracle's per-append duplicate suppression, are exactly the oracle's VALIDATION_READY,
VALIDATION_STARTED, VALIDATION_COMPLETED and ECONOMIC_MEASUREMENT_RECORDED entries. The Reviewer only assembles: it
passes the estimate through untouched, adds no check, never raises, writes and logs nothing, and keeps a failure of the
episode as ``halt`` with the payloads decided before it.
"""
import collections
import dataclasses
import inspect
import logging
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.parity import Opaque, compare_measurement_section, engine, fixture_package, oengine, outcome, run_sequence, staged_scenario_runs, tree  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import CounterfactualRecord, MeasurementFacts, RealizationEstimate, ReviewResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.reviewer import economics, reviewer, validation  # noqa: E402


class EveryScenarioRunTests(unittest.TestCase):
    def test_reviewed_episode_and_recognition_are_the_oracle_ledger_entries_for_every_run_of_every_scenario(self):
        seen = collections.Counter()
        for run in staged_scenario_runs():
            with self.subTest(scenario=run.scenario, run=run.index):
                result = compare_measurement_section(self, run)
                self.assertEqual(run.modular_tree, run.oracle_tree)
                if result is None:
                    continue
                context, analysis, governance, reconciliation, estimate, review = result
                self.assertIs(review.analysis, analysis)
                self.assertIs(review.governance, governance)
                self.assertIs(review.reconciliation, reconciliation)
                episode = review.validation
                self.assertTrue(episode.validation_episode_id.startswith("VAL-"))
                self.assertEqual(episode.completed_payload["hard_guardrails_pass"], estimate.validation_passed)
                seen[f"validation {episode.completed_payload['state']}"] += 1
                if estimate.measurement is None:
                    self.assertIsNone(review.recognition_status)
                else:
                    seen[f"recognition {review.recognition_status}"] += 1
        for kind in ("validation PASSED", "validation FAILED", "recognition CONFIRMED", "recognition REJECTED", "recognition PROVISIONAL"):
            self.assertGreater(seen[kind], 0, f"{kind}: {dict(seen)}")


class RecognitionStatusTests(unittest.TestCase):
    def test_truth_table(self):
        self.assertEqual(economics.recognition_status(True, True), "CONFIRMED")
        self.assertEqual(economics.recognition_status(True, False), "PROVISIONAL")
        self.assertEqual(economics.recognition_status(False, True), "REJECTED")
        self.assertEqual(economics.recognition_status(False, False), "REJECTED")
        self.assertEqual({economics.recognition_status(a, b) for a in (True, False) for b in (True, False)}, {m.value for m in rc.RecognitionStatus})

    def test_expression_is_the_oracle_measurement_payload_expression(self):
        oracle_source = inspect.getsource(oengine.process_observation)
        expression = ('rc.RecognitionStatus.CONFIRMED.value if validation["hard_guardrails_pass"] and cf_claim_eligible else '
                      'rc.RecognitionStatus.REJECTED.value if not validation["hard_guardrails_pass"] else rc.RecognitionStatus.PROVISIONAL.value')
        self.assertIn(expression, oracle_source)
        moved = expression.replace('validation["hard_guardrails_pass"]', "validation_passed").replace("cf_claim_eligible", "counterfactual_claim_eligible")
        self.assertIn(f"return {moved}", inspect.getsource(economics.recognition_status))


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def stages(self, observation, out=Path("/nonexistent-out"), policy=None):
        context = engine.build_context(RealizationRequest(self.pkg, observation, out, policy=policy))
        analysis = engine.analyzer.run(context)
        governance = engine.governor.run(context, analysis)
        return context, analysis, governance, engine.reconciler.run(context, analysis, governance)

    def review(self, observation, estimate=None, **kwargs):
        stages = self.stages(observation, **kwargs)
        return reviewer.run(*stages, estimate if estimate is not None else engine.estimator.run(*stages))

    def test_nothing_is_reviewed_unless_validation_is_due(self):
        for label, observation in {"lineage only": None, "not ready": {**self.base, "elapsed_days": 1},
                                   "nothing applied": {**self.base, "config": oengine.package_config(self.pkg)}}.items():
            with self.subTest(case=label):
                stages = self.stages(observation)
                estimate = RealizationEstimate()
                review = reviewer.run(*stages, estimate)
                self.assertEqual(review, ReviewResult(*stages[1:], estimate=estimate))

    def test_episode_payloads(self):
        review = self.review(self.base)
        episode = review.validation
        policy_id = engine.RealizerPolicy().snapshot_id
        ready = engine.readiness(self.base, engine.RealizerPolicy())
        self.assertEqual(episode.ready_payload, {"validation_episode_id": episode.validation_episode_id, "readiness": ready, "policy_snapshot_id": policy_id})
        self.assertEqual(episode.started_payload, {"validation_episode_id": episode.validation_episode_id,
                                                   "window_start_utc": self.base["window_start_utc"], "window_end_utc": self.base["window_end_utc"]})
        self.assertEqual(episode.completed_payload["state"], "PASSED")
        self.assertEqual(review.recognition_status, "CONFIRMED")
        # the window falls back to the effective and observed times
        bare = {k: v for k, v in self.base.items() if k not in ("window_start_utc", "window_end_utc")}
        started = self.review(bare).validation.started_payload
        self.assertEqual((started["window_start_utc"], started["window_end_utc"]), (bare["applied_at_utc"], bare["observed_at_utc"]))

    def test_reviewer_adds_no_check_and_passes_the_estimate_through(self):
        stages = self.stages(self.base)
        # an estimate no real run produces: failed guardrails with an implausible measurement, and a pending halt
        odd = RealizationEstimate(
            validation_passed=False,
            counterfactual=CounterfactualRecord("CF-X", "M", False, None),
            economics={"available": True, "realization_state": "REALIZED"},
            measurement=MeasurementFacts("MEAS-X", None, None, "CF-Y", "M", {}, {}, "REALIZED", "-1", Opaque()),
            halt=KeyError("estimate"),
        )
        review = reviewer.run(*stages, odd)
        self.assertIs(review.estimate, odd)
        self.assertIsNone(review.halt)
        self.assertEqual(review.recognition_status, "REJECTED")
        self.assertEqual(review.validation, self.review(self.base).validation)

    def test_run_never_raises_and_halts_with_the_oracle_exception(self):
        cases = {
            "policy snapshot": ({}, {"realized_ratio_min": "NaN"}, None),
            "validation id": ({"window": {"start_utc": "x"}, "window_start_utc": Opaque()}, None, None),
            "performance": ({"performance": 5}, None, "started_payload"),
            "performance value": ({"performance": {"normalized_p95_runtime_regression_pct": "abc"}}, None, "started_payload"),
        }
        for label, (patch, mapping, last) in cases.items():
            observation = {**self.base, **patch}
            with self.subTest(case=label):
                policy = None if mapping is None else engine.RealizerPolicy.from_mapping(mapping)
                review = self.review(observation, policy=policy)
                (old,), _ = run_sequence(oengine, self.pkg, [observation], mapping)
                self.assertEqual((type(review.halt).__name__, str(review.halt)), old)
                self.assertIsNone(review.recognition_status)
                if last is None:
                    self.assertIsNone(review.validation)
                else:
                    self.assertIsNotNone(review.validation.ready_payload)
                    self.assertIsNotNone(review.validation.started_payload)
                    self.assertIsNone(review.validation.completed_payload)

    def test_plan_episode_fills_in_recording_order(self):
        context, analysis, governance, reconciliation = self.stages({**self.base, "performance": 5})
        fields = {}
        result = outcome(validation.plan_episode, fields, reconciliation.active.application_episode_id, analysis.observation, governance)
        self.assertEqual(result, ("AttributeError", "'int' object has no attribute 'get'"))
        self.assertEqual(list(fields), ["validation_episode_id", "ready_payload", "started_payload"])

    def test_run_is_pure_and_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            engine.realize(self.pkg, self.base, out)
            before = tree(out)
            stages = self.stages(self.base, out)
            estimate = engine.estimator.run(*stages)
            with self.assertNoLogs("realizer", logging.DEBUG):
                review = reviewer.run(*stages, estimate)
            self.assertEqual(tree(out), before)
        for value in (review, review.validation):
            with self.subTest(part=type(value).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, "halt" if value is review else "ready_payload", None)


if __name__ == "__main__":
    unittest.main()
