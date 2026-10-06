"""Realizer governor capability (src/realizer/governor/governor.py): ``run`` and its ``GovernanceResult``.

The Governor evaluates every policy check up front and hands each over as a ``Deferred``; ``get()`` returns the stored
result of the policy check (as the single-file release's inline call did), or raises its exception, and ``run`` itself
never raises on an observation.
"""
import dataclasses
import logging
import tempfile
import unittest
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, canonical, fixture_package, outcome, tree  # noqa: E402
from realizer.analyzer import analyzer  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.utils import Deferred  # noqa: E402
from realizer.contracts.governance import GovernanceResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.realizer import build_context  # noqa: E402

CHECKS = ("readiness", "performance", "counterfactual", "economic_quality")


def govern(observation, policy_mapping=None, out=Path("/nonexistent-out")):
    package = fixture_package()
    policy = None if policy_mapping is None else RealizerPolicy.from_mapping(policy_mapping)
    context = build_context(RealizationRequest(package, observation, out, policy=policy))
    analysis = analyzer.run(context) if observation else analyzer.run(build_context(RealizationRequest(package, None, out)))
    return context, governor.run(context, analysis)


def observation_of(**patch):
    return {**demo_observation(fixture_package()), **patch}


class RunTests(unittest.TestCase):
    def test_lineage_only_run_holds_the_policy_and_no_checks(self):
        context, result = govern(None)
        self.assertIsInstance(result, GovernanceResult)
        self.assertIs(result.policy, context.policy)
        self.assertEqual(result.policy_snapshot_id.get(), context.policy.snapshot_id)
        for field in CHECKS:
            self.assertIsNone(getattr(result, field))

    def test_checks_for_every_scenario_observation(self):
        results = {}
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            for index, step in enumerate(scenario["runs"]):
                observation = step.get("observation")
                if not observation:
                    continue
                with self.subTest(scenario=name):
                    policy = RealizerPolicy.from_mapping(step.get("policy"))
                    analysis = analyzer.run(build_context(RealizationRequest(scenario["package_doc"], observation, Path("/nonexistent-out"), policy=policy)))
                    result = governor.run(build_context(RealizationRequest(scenario["package_doc"], observation, Path("/nonexistent-out"), policy=policy)), analysis)
                    entry = {"policy_snapshot_id": result.policy_snapshot_id.get()}
                    for field in CHECKS:
                        deferred = getattr(result, field)
                        self.assertIsInstance(deferred, Deferred)
                        entry[field] = list(outcome(deferred.get))
                    results[f"{name}[{index}]"] = entry
        self.assertGreater(len(results), 40)
        expect.check(self, "results", results, spot={"count": len(results), "exceptions": sorted({r[0] for e in results.values() for f, r in e.items() if f != "policy_snapshot_id" and r[0] != "ok"})})

    def test_malformed_observation_never_fails_run_and_get_raises_the_policy_exception(self):
        cases = {
            "readiness": {"elapsed_days": "abc"}, "performance": {"performance": 5},
            "counterfactual": {"economics": "x"}, "economic_quality": {"attributable_spend_pct": "abc"},
        }
        for field, patch in cases.items():
            with self.subTest(field=field):
                observation = observation_of(**patch)
                context, result = govern(observation)
                outcomes = {name: list(outcome(getattr(result, name).get)) for name in CHECKS}
                self.assertNotEqual(outcomes[field][0], "ok")
                # the other checks are unaffected by this field
                expect.check(self, field, outcomes)

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
        bad = dataclasses.replace(RealizerPolicy(), minimum_days=Opaque())
        context = build_context(RealizationRequest(package, observation_of(), Path("/nonexistent-out"), policy=bad))
        analysis = analyzer.run(context)
        result = governor.run(context, analysis)
        reference = outcome(result.policy_snapshot_id.get)
        self.assertEqual(reference[0], "TypeError")
        expect.check(self, "snapshot id", list(reference))

    def test_run_is_pure_it_writes_and_logs_nothing(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp, self.assertNoLogs("realizer", logging.DEBUG):
            out = Path(tmp)
            context = build_context(RealizationRequest(package, observation_of(), out))
            analysis = analyzer.run(context)
            governor.run(context, analysis)
            self.assertEqual(tree(out), {})

    def test_result_is_frozen(self):
        _, result = govern(observation_of())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.readiness = None


if __name__ == "__main__":
    unittest.main()
