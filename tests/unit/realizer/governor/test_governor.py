"""Realizer governor capability (src/realizer/governor/governor.py): ``run`` and its ``GovernanceResult``.

The Governor evaluates every policy check up front and hands each over as a ``Deferred``; ``get()`` returns what the
oracle's inline call returned, or raises the same exception, and ``run`` itself never raises on an observation.
"""
import copy
import dataclasses
import logging
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.parity import Opaque, canonical, engine, fixture_package, oengine, outcome, tree  # noqa: E402
from realizer.common.utils import Deferred  # noqa: E402
from realizer.contracts.governance import GovernanceResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.governor import governor  # noqa: E402

CHECKS = {
    "readiness": ("readiness", True), "performance": ("validate_performance", True),
    "counterfactual": ("counterfactual_eligible", False), "economic_quality": ("economic_quality_result", True),
}


def govern(observation, policy_mapping=None, out=Path("/nonexistent-out")):
    package = fixture_package()
    policy = None if policy_mapping is None else engine.RealizerPolicy.from_mapping(policy_mapping)
    context = engine.build_context(RealizationRequest(package, observation, out, policy=policy))
    analysis = engine.analyzer.run(context) if observation else engine.analyzer.run(engine.build_context(RealizationRequest(package, None, out)))
    return context, governor.run(context, analysis)


def observation_of(**patch):
    return {**oengine.demo_observation(fixture_package()), **patch}


class RunTests(unittest.TestCase):
    def test_engine_runs_the_governor_module(self):
        self.assertIs(engine.governor, governor)

    def test_lineage_only_run_holds_the_policy_and_no_checks(self):
        context, result = govern(None)
        self.assertIsInstance(result, GovernanceResult)
        self.assertIs(result.policy, context.policy)
        self.assertEqual(result.policy_snapshot_id.get(), context.policy.snapshot_id)
        for field in CHECKS:
            self.assertIsNone(getattr(result, field))

    def test_checks_equal_the_oracle_for_every_scenario_observation(self):
        count = 0
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            for step in scenario["runs"]:
                observation = step.get("observation")
                if not observation:
                    continue
                with self.subTest(scenario=name):
                    policy = engine.RealizerPolicy.from_mapping(step.get("policy"))
                    opolicy = oengine.RealizerPolicy.from_mapping(step.get("policy"))
                    analysis = engine.analyzer.run(engine.build_context(RealizationRequest(scenario["package_doc"], observation, Path("/nonexistent-out"), policy=policy)))
                    result = governor.run(engine.build_context(RealizationRequest(scenario["package_doc"], observation, Path("/nonexistent-out"), policy=policy)), analysis)
                    self.assertEqual(result.policy_snapshot_id.get(), opolicy.snapshot_id)
                    for field, (fn, takes_policy) in CHECKS.items():
                        deferred = getattr(result, field)
                        self.assertIsInstance(deferred, Deferred)
                        reference = outcome(getattr(oengine, fn), copy.deepcopy(observation), *([opolicy] if takes_policy else []))
                        self.assertEqual(outcome(deferred.get), reference, field)
                    count += 1
        self.assertGreater(count, 40)

    def test_malformed_observation_never_fails_run_and_get_raises_the_oracle_exception(self):
        cases = {
            "readiness": {"elapsed_days": "abc"}, "performance": {"performance": 5},
            "counterfactual": {"economics": "x"}, "economic_quality": {"attributable_spend_pct": "abc"},
        }
        for field, patch in cases.items():
            with self.subTest(field=field):
                observation = observation_of(**patch)
                context, result = govern(observation)
                fn, takes_policy = CHECKS[field]
                reference = outcome(getattr(oengine, fn), copy.deepcopy(observation), *([oengine.RealizerPolicy()] if takes_policy else []))
                self.assertNotEqual(reference[0], "ok")
                self.assertEqual(outcome(getattr(result, field).get), reference)
                # the other checks are unaffected by this field
                for other, (ofn, otakes) in CHECKS.items():
                    if other != field:
                        expected = outcome(getattr(oengine, ofn), copy.deepcopy(observation), *([oengine.RealizerPolicy()] if otakes else []))
                        self.assertEqual(outcome(getattr(result, other).get), expected)

    def test_a_raised_check_keeps_the_exception_and_raises_it_on_every_get(self):
        _, result = govern(observation_of(elapsed_days="abc"))
        self.assertIsNone(result.readiness.value)
        self.assertIsInstance(result.readiness.error, ValueError)
        for _ in range(2):
            with self.assertRaises(ValueError) as caught:
                result.readiness.get()
            self.assertIs(caught.exception, result.readiness.error)

    def test_policy_whose_snapshot_id_fails_does_not_fail_run(self):
        package = fixture_package()
        bad = dataclasses.replace(engine.RealizerPolicy(), minimum_days=Opaque())
        context = engine.build_context(RealizationRequest(package, observation_of(), Path("/nonexistent-out"), policy=bad))
        analysis = engine.analyzer.run(context)
        result = governor.run(context, analysis)
        reference = outcome(lambda: dataclasses.replace(oengine.RealizerPolicy(), minimum_days=Opaque()).snapshot_id)
        self.assertEqual(outcome(result.policy_snapshot_id.get), reference)
        self.assertEqual(reference[0], "TypeError")

    def test_run_is_pure_it_writes_and_logs_nothing(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp, self.assertNoLogs("realizer", logging.DEBUG):
            out = Path(tmp)
            context = engine.build_context(RealizationRequest(package, observation_of(), out))
            analysis = engine.analyzer.run(context)
            governor.run(context, analysis)
            self.assertEqual(tree(out), {})

    def test_result_is_frozen(self):
        _, result = govern(observation_of())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.readiness = None


if __name__ == "__main__":
    unittest.main()
