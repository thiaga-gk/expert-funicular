"""Realizer policy (src/realizer/governor/policy.py): ``RealizerPolicy`` and the checks that apply it.

``readiness``, ``validate_performance``, ``counterfactual_eligible``, ``economic_quality_result``, ``authority_eligible``
and ``savings_improvement_material`` return, and raise, the results stored in ``tests/fixtures/realizer/expectations/``
(generated from the single-file release) over every observation of the 41 scenarios and over malformed observations.
"""
import copy
import dataclasses
import math
import unittest
from decimal import Decimal

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, canonical, fixture_package, outcome  # noqa: E402
from realizer.config import settings  # noqa: E402
from realizer.governor import policy  # noqa: E402

POLICY_CHECKS = ("readiness", "validate_performance", "economic_quality_result")
POLICIES = [{}, {"minimum_days": 3, "minimum_queries": 10}, {"max_normalized_p95_runtime_regression_pct": "4", "realized_ratio_min": "0.9"},
            {"require_representative_regime": False, "minimum_attributable_spend_pct": 80, "minimum_price_coverage_pct": "90.5"},
            {"require_confirmed_origin_for_hard_kpi": False, "minimum_material_savings_delta_pct": 1}]


def make_policy(mapping):
    return policy.RealizerPolicy.from_mapping(mapping)


def plain(value):
    """*value* as data an expectation can store: Opaque and non-finite numbers become text."""
    if isinstance(value, Opaque):
        return "Opaque()"
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, Decimal) and not value.is_finite():
        return repr(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def plain_outcome(result):
    return plain(list(result))


def scenario_observations():
    for name in canonical.scenario_names():
        for i, run in enumerate(canonical.load_scenario(name)["runs"]):
            if run.get("observation"):
                yield f"{name}[{i}]", run["observation"], run.get("policy")


class PolicyClassTests(unittest.TestCase):
    def test_defaults_snapshot_id_and_dict(self):
        new = make_policy(None)
        self.assertTrue(new.snapshot_id.startswith("PSNAP-"))
        self.assertEqual(new.policy_version, settings.POLICY_VERSION)
        expect.check(self, "defaults", {"fields": dataclasses.asdict(new), "snapshot_id": new.snapshot_id, "dict": policy.policy_to_dict(new)})
        expect.check(self, "field names", [f.name for f in dataclasses.fields(policy.RealizerPolicy)])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            new.minimum_days = 1

    def test_from_mapping_and_snapshot_ids_for_overrides(self):
        results = {}
        for index, mapping in enumerate(POLICIES + [{"unknown": 1}, None, {"minimum_days": "9"}]):
            new = make_policy(mapping)
            results[f"{index} {mapping}"] = {"dict": policy.policy_to_dict(new), "snapshot_id": new.snapshot_id}
        ids = {entry["snapshot_id"] for entry in results.values()}
        self.assertGreater(len(ids), 4)
        expect.check(self, "policies", results, spot={"count": len(results), "snapshot_ids": {key: entry["snapshot_id"] for key, entry in results.items()}})

    def test_from_mapping_errors(self):
        for index, mapping in enumerate([{"realized_ratio_min": "abc"}, {"minimum_attributable_spend_pct": Opaque()}, {"minimum_days": 1, "realized_ratio_min": [1]}]):
            with self.subTest(mapping=mapping):
                new = outcome(policy.RealizerPolicy.from_mapping, mapping)
                self.assertNotEqual(new[0], "ok")
                expect.check(self, f"mapping {index}", plain_outcome(new))

    def test_snapshot_id_and_dict_errors_for_malformed_field_values(self):
        for field, value in [("minimum_days", Opaque()), ("policy_version", {1, 2}), ("minimum_queries", float("nan")), ("realized_ratio_min", Decimal("NaN"))]:
            with self.subTest(field=field):
                new = dataclasses.replace(policy.RealizerPolicy(), **{field: value})
                expect.check(self, field, [plain_outcome(outcome(lambda: new.snapshot_id)), plain_outcome(outcome(policy.policy_to_dict, new))])


class CheckTests(unittest.TestCase):
    def check_outcome(self, name, observation, mapping):
        return outcome(getattr(policy, name), copy.deepcopy(observation), make_policy(mapping))

    def test_checks_on_every_scenario_observation_and_policy(self):
        seen = {name: 0 for name in POLICY_CHECKS + ("counterfactual_eligible",)}
        results = {}
        for label, observation, mapping in scenario_observations():
            for index, override in enumerate([mapping] + POLICIES):
                for name in POLICY_CHECKS:
                    result = self.check_outcome(name, observation, override)
                    seen[name] += result[0] == "ok"
                    results[f"{label}/{index}/{name}"] = plain_outcome(result)
                new = outcome(policy.counterfactual_eligible, copy.deepcopy(observation))
                seen["counterfactual_eligible"] += new[0] == "ok"
                results[f"{label}/{index}/counterfactual_eligible"] = plain_outcome(new)
        self.assertTrue(all(count > 100 for count in seen.values()), seen)
        expect.check(self, "results", results, spot={"count": len(results), "ok": seen,
                                                     "exceptions": sorted({r[0] for r in results.values() if r[0] != "ok"})})

    def test_readiness_thresholds(self):
        base = {"elapsed_days": 7, "query_count": 100, "representative_regime": True, "evidence_quality": "sufficient"}
        self.assertTrue(policy.readiness(base, policy.RealizerPolicy())["ready"])
        for index, patch in enumerate(({"elapsed_days": 6}, {"query_count": 99}, {"representative_regime": False}, {"evidence_quality": "PARTIAL"})):
            with self.subTest(patch=patch):
                self.assertFalse(policy.readiness({**base, **patch}, policy.RealizerPolicy())["ready"])
                expect.check(self, f"patch {index}", plain_outcome(self.check_outcome("readiness", {**base, **patch}, None)))
        self.assertTrue(policy.readiness({**base, "representative_regime": False}, policy.RealizerPolicy(require_representative_regime=False))["ready"])

    def test_malformed_observations_raise(self):
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
        grids = {name: {} for name in POLICY_CHECKS + ("counterfactual_eligible",)}
        for index, patch in enumerate(malformed):
            observation = {**base, **patch}
            for name in POLICY_CHECKS:
                result = self.check_outcome(name, observation, None)
                grids[name][f"{index} {sorted(patch)}"] = plain_outcome(result)
                if result[0] != "ok":
                    raised.add((name, tuple(patch)))
            new = outcome(policy.counterfactual_eligible, copy.deepcopy(observation))
            grids["counterfactual_eligible"][f"{index} {sorted(patch)}"] = plain_outcome(new)
            if new[0] != "ok":
                raised.add(("counterfactual_eligible", tuple(patch)))
        checks = {name for name, _ in raised}
        self.assertEqual(checks, set(POLICY_CHECKS) | {"counterfactual_eligible"}, "each check has a malformed input that raises")
        for name, grid in grids.items():
            expect.check(self, name, grid, spot={"count": len(grid), "exceptions": {key: r[0] for key, r in grid.items() if r[0] != "ok"}})

    def test_a_malformed_field_only_breaks_the_check_that_reads_it(self):
        """Laziness at the check level: each check ignores fields of the others."""
        economics_bad = {"economics": "x"}
        results = {
            "readiness": self.check_outcome("readiness", economics_bad, None),
            "validate_performance": self.check_outcome("validate_performance", economics_bad, None),
            "economic_quality_result": self.check_outcome("economic_quality_result", economics_bad, None),
            "counterfactual_eligible": outcome(policy.counterfactual_eligible, economics_bad),
            "readiness, performance bad": self.check_outcome("readiness", {"performance": 5, "attributable_spend_pct": "x"}, None),
            "performance, elapsed bad": self.check_outcome("validate_performance", {"elapsed_days": "x"}, None),
        }
        for key in ("readiness", "validate_performance", "economic_quality_result", "readiness, performance bad", "performance, elapsed bad"):
            self.assertEqual(results[key][0], "ok", key)
        self.assertEqual(results["counterfactual_eligible"][0], "AttributeError")
        expect.check(self, "results", {key: plain_outcome(value) for key, value in results.items()})

    def test_authority_eligible(self):
        results = {}
        for state in [None, "READY", "READY_FOR_CANARY", "PENDING_REEVALUATION", "BLOCKED", "INVALIDATED", "blocked", "", 5, "SCENARIO_APPLIED"]:
            results[f"state {state!r}"] = policy.authority_eligible({"execution_state": state})
        for step in [{}, None, [], "x", 5]:
            results[f"step {step!r}"] = plain_outcome(outcome(policy.authority_eligible, step))
        self.assertEqual(settings.NON_AUTHORITATIVE_EXECUTION_STATES, frozenset({"PENDING_REEVALUATION", "BLOCKED", "INVALIDATED"}))
        for index, step in enumerate(fixture_package()["scenarios"][0]["execution_plan"]):
            results[f"plan step {index}"] = policy.authority_eligible(step)
        expect.check(self, "results", results)

    def test_savings_improvement_material(self):
        values = [None, 0, "0", -5, "400000", 412000, "430000", "abc", "NaN", Decimal("1E+3"), Opaque(), []]
        results = {}
        for old in values:
            for new in values:
                for mapping in (None, {"minimum_material_savings_delta_pct": 1}):
                    results[f"{old!r}|{new!r}|{mapping}"] = plain_outcome(outcome(policy.savings_improvement_material, old, new, make_policy(mapping)))
        self.assertFalse(policy.savings_improvement_material("400000", "412000", policy.RealizerPolicy()))
        self.assertTrue(policy.savings_improvement_material("400000", "430000", policy.RealizerPolicy()))
        expect.check(self, "results", results, spot={"count": len(results), "true": sum(1 for r in results.values() if r == ["ok", True]),
                                                     "exceptions": sorted({r[0] for r in results.values() if r[0] != "ok"})})


if __name__ == "__main__":
    unittest.main()
