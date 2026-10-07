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
from tests.unit.realizer.helpers import Opaque, canonical, fixture_package, outcome, with_quality  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.errors import InputError  # noqa: E402
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
        scenario = canonical.load_scenario(name)
        currency = policy.package_currency(scenario["package_doc"])
        for i, run in enumerate(scenario["runs"]):
            if run.get("observation"):
                yield f"{name}[{i}]", run["observation"], run.get("policy"), currency


def complete_observation():
    """A complete, valid 2.0.0 observation of the fixture package (every key present)."""
    return demo_observation(fixture_package())


PACKAGE_CURRENCY = policy.package_currency(fixture_package())


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
        for index, mapping in enumerate(POLICIES + [None, {}]):
            new = make_policy(mapping)
            results[f"{index} {mapping}"] = {"dict": policy.policy_to_dict(new), "snapshot_id": new.snapshot_id}
        ids = {entry["snapshot_id"] for entry in results.values()}
        self.assertGreater(len(ids), 4)
        expect.check(self, "policies", results, spot={"count": len(results), "snapshot_ids": {key: entry["snapshot_id"] for key, entry in results.items()}})

    def test_from_mapping_errors(self):
        for index, mapping in enumerate([{"realized_ratio_min": "abc"}, {"minimum_attributable_spend_pct": Opaque()}, {"minimum_days": 1, "realized_ratio_min": [1]}]):
            with self.subTest(mapping=mapping):
                new = outcome(policy.RealizerPolicy.from_mapping, mapping)
                self.assertEqual(new[0], "InputError")
                expect.check(self, f"mapping {index}", plain_outcome(new))

    def test_from_mapping_refuses_each_kind_of_bad_policy(self):
        """One case per rule: the refusal is an ``InputError`` (a ``ValueError``) naming the setting, never its value."""
        cases = {
            "not a mapping": ([1, 2], "a policy file must be a JSON object"),
            "a string": ("minimum_days", "a policy file must be a JSON object"),
            "unknown key": ({"minimum_validation_days": 3}, "unknown policy setting.s.: minimum_validation_days"),
            "several unknown keys, sorted": ({"zzz": 1, "aaa": 2}, "unknown policy setting.s.: aaa, zzz"),
            "int given as a string": ({"minimum_days": "9"}, "minimum_days must be a non-negative integer"),
            "int given as a float": ({"minimum_queries": 10.5}, "minimum_queries must be a non-negative integer"),
            "int given as a bool": ({"minimum_days": True}, "minimum_days must be a non-negative integer"),
            "negative int": ({"remeasure_days_after_invalidating_change": -1}, "remeasure_days_after_invalidating_change must be a non-negative integer"),
            "decimal given as a bool": ({"realized_ratio_min": True}, "realized_ratio_min must be a number"),
            "decimal given as a list": ({"realized_ratio_min": [1]}, "realized_ratio_min must be a number"),
            "decimal given as null": ({"realized_ratio_min": None}, "realized_ratio_min must be a number"),
            "decimal given as text": ({"realized_ratio_min": "abc"}, "realized_ratio_min must be a number"),
            "decimal given as empty text": ({"minimum_material_savings_delta_pct": ""}, "minimum_material_savings_delta_pct must be a number"),
            "non-finite decimal": ({"realized_ratio_min": "NaN"}, "realized_ratio_min is out of range"),
            "infinite decimal": ({"max_normalized_p95_runtime_regression_pct": "Infinity"}, "max_normalized_p95_runtime_regression_pct is out of range"),
            "negative decimal": ({"minimum_material_savings_delta_pct": -1}, "minimum_material_savings_delta_pct is out of range"),
            "attributable spend above 100": ({"minimum_attributable_spend_pct": 100.5}, "minimum_attributable_spend_pct is out of range"),
            "price coverage above 100": ({"minimum_price_coverage_pct": "101"}, "minimum_price_coverage_pct is out of range"),
            "currency consistency above 100": ({"required_currency_consistency_pct": 1000}, "required_currency_consistency_pct is out of range"),
            "bool given as a string": ({"require_representative_regime": "false"}, "require_representative_regime must be true or false"),
            "bool given as an int": ({"require_confirmed_origin_for_hard_kpi": 1}, "require_confirmed_origin_for_hard_kpi must be true or false"),
            "string given as a number": ({"policy_version": 2}, "policy_version must be a non-empty string"),
            "empty string": ({"policy_version": ""}, "policy_version must be a non-empty string"),
            "under-realized minimum above the realized minimum": ({"under_realized_ratio_min": "0.9"}, "under_realized_ratio_min must not exceed realized_ratio_min"),
            "ratios reversed by overriding the realized minimum": ({"realized_ratio_min": "0.3"}, "under_realized_ratio_min must not exceed realized_ratio_min"),
        }
        for label, (mapping, message) in cases.items():
            with self.subTest(case=label):
                with self.assertRaisesRegex(InputError, message) as caught:
                    policy.RealizerPolicy.from_mapping(mapping)
                self.assertIsInstance(caught.exception, ValueError)
                self.assertNotIn("hunter2", str(caught.exception))

    def test_a_refusal_never_echoes_the_value(self):
        for mapping in ({"realized_ratio_min": "hunter2"}, {"minimum_days": "hunter2"}, {"policy_version": 12345}, {"hunter2": 1}):
            with self.subTest(mapping=mapping), self.assertRaises(InputError) as caught:
                policy.RealizerPolicy.from_mapping(mapping)
            if "hunter2" not in mapping:
                self.assertNotIn("hunter2", str(caught.exception))
                self.assertNotIn("12345", str(caught.exception))

    def test_from_mapping_accepts_valid_overrides_and_their_boundaries(self):
        accepted = {
            "ints, including zero": ({"minimum_days": 0, "minimum_queries": 10, "remeasure_days_after_invalidating_change": 14}, {}),
            "decimals as numbers": ({"realized_ratio_min": 0.9, "max_normalized_p95_runtime_regression_pct": 4}, {"realized_ratio_min": "0.9", "max_normalized_p95_runtime_regression_pct": "4"}),
            "decimals as numeric strings": ({"realized_ratio_min": "0.95", "minimum_material_savings_delta_pct": "2.50"}, {"realized_ratio_min": "0.95", "minimum_material_savings_delta_pct": "2.5"}),
            "percentages at 0 and 100": ({"minimum_attributable_spend_pct": 100, "minimum_price_coverage_pct": 0, "required_currency_consistency_pct": "100"}, {"minimum_attributable_spend_pct": "100", "minimum_price_coverage_pct": "0", "required_currency_consistency_pct": "100"}),
            "a ratio above 1 (a ratio is not a percentage)": ({"realized_ratio_min": 1.5}, {"realized_ratio_min": "1.5"}),
            "equal ratio thresholds": ({"realized_ratio_min": "0.6", "under_realized_ratio_min": "0.6"}, {"realized_ratio_min": "0.6", "under_realized_ratio_min": "0.6"}),
            "booleans": ({"require_representative_regime": False, "require_confirmed_origin_for_hard_kpi": False}, {}),
            "a custom policy version": ({"policy_version": "custom"}, {}),
        }
        defaults = policy.policy_to_dict(policy.RealizerPolicy())
        for label, (mapping, expected_decimals) in accepted.items():
            with self.subTest(case=label):
                new = policy.RealizerPolicy.from_mapping(mapping)
                got = policy.policy_to_dict(new)
                for key, value in mapping.items():
                    want = expected_decimals.get(key, value)
                    self.assertEqual(got[key], want, key)
                for key in set(defaults) - set(mapping):
                    self.assertEqual(got[key], defaults[key], f"{key} keeps its default")
                self.assertNotEqual(new.snapshot_id, policy.RealizerPolicy().snapshot_id)

    def test_empty_mapping_and_none_are_the_defaults(self):
        for value in (None, {}):
            self.assertEqual(policy.RealizerPolicy.from_mapping(value), policy.RealizerPolicy())

    def test_snapshot_id_and_dict_errors_for_malformed_field_values(self):
        for field, value in [("minimum_days", Opaque()), ("policy_version", {1, 2}), ("minimum_queries", float("nan")), ("realized_ratio_min", Decimal("NaN"))]:
            with self.subTest(field=field):
                new = dataclasses.replace(policy.RealizerPolicy(), **{field: value})
                expect.check(self, field, [plain_outcome(outcome(lambda: new.snapshot_id)), plain_outcome(outcome(policy.policy_to_dict, new))])


class CheckTests(unittest.TestCase):
    def check_outcome(self, name, observation, mapping, currency=PACKAGE_CURRENCY):
        """``outcome`` of the policy check *name*; the financial-quality check also takes the package's currency."""
        extra = (currency,) if name == "economic_quality_result" else ()
        return outcome(getattr(policy, name), copy.deepcopy(observation), make_policy(mapping), *extra)

    def test_checks_on_every_scenario_observation_and_policy(self):
        seen = {name: 0 for name in POLICY_CHECKS + ("counterfactual_eligible",)}
        results = {}
        for label, observation, mapping, currency in scenario_observations():
            for index, override in enumerate([mapping] + POLICIES):
                for name in POLICY_CHECKS:
                    result = self.check_outcome(name, observation, override, currency)
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
        """The loader's observation gate refuses every one of these first; the checks' own handling is pinned here."""
        def quality(**inputs):
            return {"financial_quality": {**complete_observation()["financial_quality"], **inputs}}

        malformed = [
            {"elapsed_days": "abc"}, {"elapsed_days": [1]}, {"query_count": "x"}, {"query_count": {}}, {"representative_regime": Opaque()},
            {"evidence_quality": Opaque()}, {"performance": 5}, {"performance": "x"}, {"performance": []},
            {"performance": {"normalized_p95_runtime_regression_pct": "abc"}}, {"performance": {"normalized_p95_runtime_regression_pct": "NaN"}},
            {"performance": {"normalized_p95_runtime_regression_pct": [1]}}, {"economics": "x"}, {"economics": 5}, {"economics": []},
            quality(attributable_spend_pct="abc"), quality(attributable_spend_pct=None), quality(price_coverage_pct="Infinity"),
            quality(currency_consistency_pct=[]), quality(correction_chain_complete=Opaque()), {"financial_quality": 5},
        ]
        base = complete_observation()
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
        base = complete_observation()
        economics_bad = {**base, "economics": "x"}
        both = {**with_quality(base, attributable_spend_pct="x"), "performance": 5}
        results = {
            "readiness": self.check_outcome("readiness", economics_bad, None),
            "validate_performance": self.check_outcome("validate_performance", economics_bad, None),
            "economic_quality_result": self.check_outcome("economic_quality_result", economics_bad, None),
            "counterfactual_eligible": outcome(policy.counterfactual_eligible, economics_bad),
            "readiness, performance bad": self.check_outcome("readiness", both, None),
            "performance, elapsed bad": self.check_outcome("validate_performance", {**base, "elapsed_days": "x"}, None),
        }
        for key in ("readiness", "validate_performance", "readiness, performance bad", "performance, elapsed bad"):
            self.assertEqual(results[key][0], "ok", key)
        # The currency gate reads the economics' currency, so economics that are not a mapping break it, as they break the counterfactual check.
        self.assertEqual(results["economic_quality_result"][0], "AttributeError")
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


class NullSemanticsTests(unittest.TestCase):
    """A gate input the observation could not establish (``null``) never passes its gate; only an explicit ``False``
    fails the correctness guardrail."""

    @classmethod
    def setUpClass(cls):
        cls.base = complete_observation()
        cls.policy = policy.RealizerPolicy()

    def quality(self, observation=None, currency=PACKAGE_CURRENCY):
        return policy.economic_quality_result(observation or self.base, self.policy, currency)

    def test_the_complete_observation_passes_every_gate(self):
        self.assertTrue(policy.readiness(self.base, self.policy)["ready"])
        self.assertTrue(self.quality()["pass"])
        self.assertTrue(policy.validate_performance(self.base, self.policy)["hard_guardrails_pass"])

    def test_a_null_query_count_fails_the_minimum_queries_check(self):
        result = policy.readiness({**self.base, "query_count": None}, self.policy)
        self.assertFalse(result["ready"])
        self.assertEqual(result["checks"]["minimum_queries"], {"observed": None, "threshold": self.policy.minimum_queries, "pass": False})
        self.assertTrue(all(c["pass"] for name, c in result["checks"].items() if name != "minimum_queries"))

    def test_a_null_representative_regime_fails_the_check_unless_the_policy_does_not_require_it(self):
        result = policy.readiness({**self.base, "representative_regime": None}, self.policy)
        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["representative_regime"]["pass"])
        relaxed = policy.RealizerPolicy(require_representative_regime=False)
        self.assertTrue(policy.readiness({**self.base, "representative_regime": None}, relaxed)["ready"])

    def test_each_null_financial_quality_input_fails_its_gate_and_only_its_gate(self):
        gate_of = {"attributable_spend_pct": "attributable_spend", "price_coverage_pct": "price_coverage", "currency_consistency_pct": "currency_consistency",
                   "correction_chain_complete": "correction_chain", "material_cost_component_available": "material_cost_components"}
        for key, gate in gate_of.items():
            with self.subTest(input=key):
                result = self.quality(with_quality(self.base, **{key: None}))
                self.assertFalse(result[gate]["pass"], gate)
                self.assertFalse(result["pass"])
                self.assertEqual([name for name in gate_of.values() if not result[name]["pass"]], [gate])
        self.assertIsNone(self.quality(with_quality(self.base, attributable_spend_pct=None))["attributable_spend"]["observed_pct"])

    def test_a_flag_gate_passes_only_for_true(self):
        for key, gate in (("correction_chain_complete", "correction_chain"), ("material_cost_component_available", "material_cost_components")):
            for value, passes in ((True, True), (False, False), (None, False), (1, False), ("yes", False)):
                with self.subTest(input=key, value=value):
                    self.assertIs(self.quality(with_quality(self.base, **{key: value}))[gate]["pass"], passes)

    def test_correctness_null_passes_and_only_an_explicit_false_fails(self):
        for value, passes in ((True, True), (None, True), (False, False)):
            with self.subTest(correctness_pass=value):
                observation = {**self.base, "performance": {**self.base["performance"], "correctness_pass": value}}
                result = policy.validate_performance(observation, self.policy)
                self.assertIs(result["hard_guardrails_pass"], passes)
                self.assertEqual(result["rules"][2]["result"], "PASS" if passes else "FAIL")

    def test_reliability_null_and_p95_null_fail(self):
        for patch in ({"reliability_pass": None}, {"normalized_p95_runtime_regression_pct": None}):
            with self.subTest(patch=patch):
                observation = {**self.base, "performance": {**self.base["performance"], **patch}}
                self.assertFalse(policy.validate_performance(observation, self.policy)["hard_guardrails_pass"])

    def test_currency_gate_requires_the_package_currency(self):
        self.assertEqual(PACKAGE_CURRENCY, "USD")
        self.assertEqual(policy.package_currency(fixture_package()), "USD")
        self.assertIsNone(policy.package_currency({}))
        euro = {**self.base, "economics": {**self.base["economics"], "currency": "EUR"}}
        gate = self.quality(euro)["currency_consistency"]
        self.assertFalse(gate["pass"])
        self.assertEqual((gate["currency"], gate["package_currency"]), ("EUR", "USD"))
        self.assertFalse(self.quality(euro)["pass"])
        self.assertTrue(self.quality(euro, currency="EUR")["currency_consistency"]["pass"])
        # no economics, or no package currency: nothing to compare, so the gate does not pass
        self.assertFalse(self.quality({**self.base, "economics": None})["currency_consistency"]["pass"])
        self.assertFalse(self.quality(currency=None)["currency_consistency"]["pass"])

    def test_counterfactual_methods_only_the_four_allowed_support_a_claim(self):
        econ = self.base["economics"]
        for method in ("PAIRED_CANARY", "CALIBRATED_PRIOR_STATE", "MATCHED_HISTORICAL", "CONTROLLED_PRE_POST"):
            with self.subTest(method=method):
                self.assertEqual(policy.counterfactual_eligible({**self.base, "economics": {**econ, "counterfactual_method": method}}), (True, method))
                self.assertEqual(policy.counterfactual_eligible({**self.base, "economics": {**econ, "counterfactual_method": method, "counterfactual_old_cost": None}}),
                                 (False, "NO_DEFENSIBLE_COUNTERFACTUAL"))
        self.assertEqual(policy.counterfactual_eligible({**self.base, "economics": {**econ, "counterfactual_method": "CORRELATED_ONLY"}}), (False, "CORRELATED_ONLY"))
        self.assertEqual(policy.counterfactual_eligible({**self.base, "economics": {**econ, "counterfactual_method": "NO_DEFENSIBLE_COUNTERFACTUAL"}}),
                         (False, "NO_DEFENSIBLE_COUNTERFACTUAL"))
        # an unknown method is never claim eligible
        self.assertFalse(policy.counterfactual_eligible({**self.base, "economics": {**econ, "counterfactual_method": "GUESS"}})[0])
        self.assertEqual(policy.counterfactual_eligible({**self.base, "economics": None}), (False, "NO_DEFENSIBLE_COUNTERFACTUAL"))


if __name__ == "__main__":
    unittest.main()
