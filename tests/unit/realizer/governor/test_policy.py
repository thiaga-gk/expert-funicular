"""Realizer policy (src/realizer/governor/policy.py): ``RealizerPolicy`` and the checks that apply it.

``readiness``, ``validate_performance``, ``counterfactual_eligible``, ``economic_quality_result``, ``authority_eligible``
and ``savings_improvement_material`` return, and raise, exactly what the frozen oracle's copies did, over every
observation of the 41 scenarios and over malformed observations; the thresholds are inline-identical.
"""
import copy
import dataclasses
import unittest
from decimal import Decimal

from tests.unit.realizer.parity import Opaque, canonical, engine, fixture_package, oengine, outcome  # noqa: E402
from realizer.config import settings  # noqa: E402
from realizer.governor import policy  # noqa: E402

POLICY_CHECKS = ("readiness", "validate_performance", "economic_quality_result")
POLICIES = [{}, {"minimum_days": 3, "minimum_queries": 10}, {"max_normalized_p95_runtime_regression_pct": "4", "realized_ratio_min": "0.9"},
            {"require_representative_regime": False, "minimum_attributable_spend_pct": 80, "minimum_price_coverage_pct": "90.5"},
            {"require_confirmed_origin_for_hard_kpi": False, "minimum_material_savings_delta_pct": 1}]


def both_policies(mapping):
    return policy.RealizerPolicy.from_mapping(mapping), oengine.RealizerPolicy.from_mapping(mapping)


def scenario_observations():
    for name in canonical.scenario_names():
        for i, run in enumerate(canonical.load_scenario(name)["runs"]):
            if run.get("observation"):
                yield f"{name}[{i}]", run["observation"], run.get("policy")


class PolicyClassTests(unittest.TestCase):
    def test_engine_reexports_the_governor_names(self):
        for name in ("RealizerPolicy", "policy_to_dict", "readiness", "validate_performance", "counterfactual_eligible",
                     "economic_quality_result", "authority_eligible", "savings_improvement_material"):
            self.assertIs(getattr(engine, name), getattr(policy, name))

    def test_defaults_snapshot_id_and_dict_equal_the_oracle(self):
        new, old = both_policies(None)
        self.assertEqual(dataclasses.asdict(new), dataclasses.asdict(old))
        self.assertEqual(new.snapshot_id, old.snapshot_id)
        self.assertTrue(new.snapshot_id.startswith("PSNAP-"))
        self.assertEqual(policy.policy_to_dict(new), oengine.policy_to_dict(old))
        self.assertEqual(new.policy_version, settings.POLICY_VERSION)
        self.assertEqual([f.name for f in dataclasses.fields(policy.RealizerPolicy)], [f.name for f in dataclasses.fields(oengine.RealizerPolicy)])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            new.minimum_days = 1

    def test_from_mapping_and_snapshot_ids_for_overrides(self):
        ids = set()
        for mapping in POLICIES + [{"unknown": 1}, None, {"minimum_days": "9"}]:
            with self.subTest(mapping=mapping):
                new, old = both_policies(mapping)
                self.assertEqual(policy.policy_to_dict(new), oengine.policy_to_dict(old))
                self.assertEqual(new.snapshot_id, old.snapshot_id)
                ids.add(new.snapshot_id)
        self.assertGreater(len(ids), 4)

    def test_from_mapping_errors_equal_the_oracle(self):
        for mapping in [{"realized_ratio_min": "abc"}, {"minimum_attributable_spend_pct": Opaque()}, {"minimum_days": 1, "realized_ratio_min": [1]}]:
            with self.subTest(mapping=mapping):
                new = outcome(policy.RealizerPolicy.from_mapping, mapping)
                old = outcome(oengine.RealizerPolicy.from_mapping, mapping)
                self.assertEqual(new, old)
                self.assertNotEqual(new[0], "ok")

    def test_snapshot_id_and_dict_errors_equal_the_oracle_for_malformed_field_values(self):
        for field, value in [("minimum_days", Opaque()), ("policy_version", {1, 2}), ("minimum_queries", float("nan")), ("realized_ratio_min", Decimal("NaN"))]:
            with self.subTest(field=field):
                new = dataclasses.replace(policy.RealizerPolicy(), **{field: value})
                old = dataclasses.replace(oengine.RealizerPolicy(), **{field: value})
                self.assertEqual(outcome(lambda: new.snapshot_id), outcome(lambda: old.snapshot_id))
                self.assertEqual(outcome(policy.policy_to_dict, new), outcome(oengine.policy_to_dict, old))


class CheckTests(unittest.TestCase):
    def assert_same_check(self, name, observation, mapping):
        new_policy, old_policy = both_policies(mapping)
        new = outcome(getattr(policy, name), copy.deepcopy(observation), new_policy)
        old = outcome(getattr(oengine, name), copy.deepcopy(observation), old_policy)
        self.assertEqual(new, old, name)
        return new

    def test_checks_equal_the_oracle_on_every_scenario_observation_and_policy(self):
        seen = {name: 0 for name in POLICY_CHECKS + ("counterfactual_eligible",)}
        for label, observation, mapping in scenario_observations():
            for override in [mapping] + POLICIES:
                with self.subTest(observation=label, policy=override):
                    for name in POLICY_CHECKS:
                        seen[name] += self.assert_same_check(name, observation, override)[0] == "ok"
                    new = outcome(policy.counterfactual_eligible, copy.deepcopy(observation))
                    self.assertEqual(new, outcome(oengine.counterfactual_eligible, copy.deepcopy(observation)))
                    seen["counterfactual_eligible"] += new[0] == "ok"
        self.assertTrue(all(count > 100 for count in seen.values()), seen)

    def test_readiness_thresholds(self):
        base = {"elapsed_days": 7, "query_count": 100, "representative_regime": True, "evidence_quality": "sufficient"}
        self.assertTrue(policy.readiness(base, policy.RealizerPolicy())["ready"])
        for patch in ({"elapsed_days": 6}, {"query_count": 99}, {"representative_regime": False}, {"evidence_quality": "PARTIAL"}):
            with self.subTest(patch=patch):
                self.assertFalse(policy.readiness({**base, **patch}, policy.RealizerPolicy())["ready"])
                self.assert_same_check("readiness", {**base, **patch}, None)
        self.assertTrue(policy.readiness({**base, "representative_regime": False}, policy.RealizerPolicy(require_representative_regime=False))["ready"])

    def test_malformed_observations_raise_the_oracle_exception(self):
        malformed = [
            {"elapsed_days": "abc"}, {"elapsed_days": [1]}, {"query_count": "x"}, {"query_count": {}}, {"representative_regime": Opaque()},
            {"evidence_quality": Opaque()}, {"performance": 5}, {"performance": "x"}, {"performance": []},
            {"performance": {"normalized_p95_runtime_regression_pct": "abc"}}, {"performance": {"normalized_p95_runtime_regression_pct": "NaN"}},
            {"performance": {"normalized_p95_runtime_regression_pct": [1]}}, {"economics": "x"}, {"economics": 5}, {"economics": []},
            {"attributable_spend_pct": "abc"}, {"attributable_spend_pct": None}, {"price_coverage_pct": "Infinity"}, {"currency_consistency_pct": []},
            {"correction_chain_complete": Opaque()},
        ]
        base = {"elapsed_days": 8, "query_count": 250, "representative_regime": True, "performance": {"normalized_p95_runtime_regression_pct": "2", "reliability_pass": True}}
        raised = set()
        for patch in malformed:
            observation = {**base, **patch}
            for name in POLICY_CHECKS:
                with self.subTest(patch=patch, check=name):
                    result = self.assert_same_check(name, observation, None)
                    if result[0] != "ok":
                        raised.add((name, tuple(patch)))
            with self.subTest(patch=patch, check="counterfactual_eligible"):
                new = outcome(policy.counterfactual_eligible, copy.deepcopy(observation))
                self.assertEqual(new, outcome(oengine.counterfactual_eligible, copy.deepcopy(observation)))
                if new[0] != "ok":
                    raised.add(("counterfactual_eligible", tuple(patch)))
        checks = {name for name, _ in raised}
        self.assertEqual(checks, set(POLICY_CHECKS) | {"counterfactual_eligible"}, "each check has a malformed input that raises")

    def test_a_malformed_field_only_breaks_the_check_that_reads_it(self):
        """Laziness at the check level: each check ignores fields of the others, as in the oracle."""
        economics_bad = {"economics": "x"}
        self.assertEqual(self.assert_same_check("readiness", economics_bad, None)[0], "ok")
        self.assertEqual(self.assert_same_check("validate_performance", economics_bad, None)[0], "ok")
        self.assertEqual(self.assert_same_check("economic_quality_result", economics_bad, None)[0], "ok")
        self.assertEqual(outcome(policy.counterfactual_eligible, economics_bad)[0], "AttributeError")
        self.assertEqual(self.assert_same_check("readiness", {"performance": 5, "attributable_spend_pct": "x"}, None)[0], "ok")
        self.assertEqual(self.assert_same_check("validate_performance", {"elapsed_days": "x"}, None)[0], "ok")

    def test_authority_eligible(self):
        for state in [None, "READY", "READY_FOR_CANARY", "PENDING_REEVALUATION", "BLOCKED", "INVALIDATED", "blocked", "", 5, "SCENARIO_APPLIED"]:
            step = {"execution_state": state}
            with self.subTest(state=state):
                self.assertEqual(policy.authority_eligible(step), oengine.authority_eligible(step))
        for step in [{}, None, [], "x", 5]:
            with self.subTest(step=step):
                self.assertEqual(outcome(policy.authority_eligible, step), outcome(oengine.authority_eligible, step))
        self.assertEqual(settings.NON_AUTHORITATIVE_EXECUTION_STATES, frozenset({"PENDING_REEVALUATION", "BLOCKED", "INVALIDATED"}))
        for step in fixture_package()["scenarios"][0]["execution_plan"]:
            self.assertEqual(policy.authority_eligible(step), oengine.authority_eligible(step))

    def test_savings_improvement_material(self):
        values = [None, 0, "0", -5, "400000", 412000, "430000", "abc", "NaN", Decimal("1E+3"), Opaque(), []]
        for old in values:
            for new in values:
                for mapping in (None, {"minimum_material_savings_delta_pct": 1}):
                    with self.subTest(old=old, new=new, mapping=mapping):
                        new_policy, old_policy = both_policies(mapping)
                        self.assertEqual(outcome(policy.savings_improvement_material, old, new, new_policy),
                                         outcome(oengine.savings_improvement_material, old, new, old_policy))
        self.assertFalse(policy.savings_improvement_material("400000", "412000", policy.RealizerPolicy()))
        self.assertTrue(policy.savings_improvement_material("400000", "430000", policy.RealizerPolicy()))


if __name__ == "__main__":
    unittest.main()
