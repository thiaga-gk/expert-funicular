"""Estimator attribution (src/realizer/estimator/attribution.py): ``attribution_result`` and
``authoritative_cumulative_realization``, moved unchanged from the oracle.

Both return, and raise, exactly what the oracle's copies did: attribution over origins, overlap counts, conformance,
verdicts, available and unavailable economics, each financial-quality gate failing, absent gates and the
confirmed-origin policy; the cumulative helper over valid and malformed costs. Overlap and no-double-count are also
pinned to explicit values.
"""
import inspect
import itertools
import unittest

from tests.unit.realizer.parity import Unprintable, engine, oengine, outcome  # noqa: E402
from realizer.estimator import attribution  # noqa: E402

GATES = ("attributable_spend", "price_coverage", "currency_consistency", "correction_chain", "material_cost_components")
DISQUALIFIER = {"attributable_spend": "MATERIAL_SPEND_UNRECONCILED", "price_coverage": "PRICE_COVERAGE_INCOMPLETE",
                "currency_consistency": "CURRENCY_MISMATCH", "correction_chain": "CORRECTION_CHAIN_INCOMPLETE",
                "material_cost_components": "MATERIAL_COST_COMPONENT_UNAVAILABLE"}


def gates(failing=None):
    out = {name: {"pass": name != failing} for name in GATES}
    out["pass"] = failing is None
    return out


ECONOMICS = {
    "available": {"available": True, "successful_realized_savings": "1000.00", "quality_gates": gates()},
    "unavailable": {"available": False, "successful_realized_savings": None, "quality_gates": gates()},
    "no gates": {"available": True, "successful_realized_savings": "1000.00"},
    "zero savings": {"available": True, "successful_realized_savings": "0.00", "quality_gates": gates()},
    **{f"{gate} fails": {"available": True, "successful_realized_savings": "1000.00", "quality_gates": gates(gate)} for gate in GATES},
}


def both(mapping):
    return engine.RealizerPolicy.from_mapping(mapping), oengine.RealizerPolicy.from_mapping(mapping)


class MovedNamesTests(unittest.TestCase):
    def test_moved_unchanged_and_reexported(self):
        for name in ("attribution_result", "authoritative_cumulative_realization"):
            with self.subTest(name=name):
                moved = getattr(attribution, name)
                self.assertIs(getattr(engine, name), moved)
                self.assertEqual(inspect.getsource(moved), inspect.getsource(getattr(oengine, name)))


class AttributionGridTests(unittest.TestCase):
    def test_grid_equals_the_oracle(self):
        origins = ["CONFIRMED", "INFERRED", "UNKNOWN", "INDEPENDENT", "NOT_ATTRIBUTABLE", "confirmed", "bogus", None, ""]
        overlaps = [0, 1, "2", None, ""]
        apps = [{"implementation_conformance": c, **scope} for c in ("EXACT", "MODIFIED", "CONTRARY", None)
                for scope in ({}, {"application_scope": "SCENARIO"})]
        count = 0
        claims = set()
        for app, origin, overlap, passed, eligible, (label, economic), mapping in itertools.product(
                apps, origins, overlaps, (True, False), (True, False), ECONOMICS.items(), (None, {"require_confirmed_origin_for_hard_kpi": False})):
            observation = {"origin_attribution": origin, "overlap_count": overlap, "attribution_scope": "PORTFOLIO"}
            new_policy, old_policy = both(mapping)
            new = outcome(attribution.attribution_result, app, observation, passed, eligible, economic, new_policy)
            self.assertEqual(new, outcome(oengine.attribution_result, app, observation, passed, eligible, economic, old_policy), (label, origin, overlap))
            if new[0] == "ok":
                claims.add((new[1]["attribution_claim_status"], new[1]["attribution_state"]))
            count += 1
        self.assertGreater(count, 20000)
        self.assertEqual(claims, {("CLAIMABLE", "HIGH_CONFIDENCE"), ("OBSERVABLE_ONLY", "PARTIAL_ATTRIBUTION"),
                                  ("OBSERVABLE_ONLY", "CORRELATED_ONLY"), ("NOT_ATTRIBUTABLE", "NOT_ATTRIBUTABLE")})

    def test_absent_gates_are_computed_from_the_observation(self):
        observation = {"origin_attribution": "CONFIRMED", "attributable_spend_pct": "90"}
        new_policy, old_policy = both(None)
        result = attribution.attribution_result({"implementation_conformance": "EXACT"}, observation, True, True, ECONOMICS["no gates"], new_policy)
        self.assertEqual(result, oengine.attribution_result({"implementation_conformance": "EXACT"}, observation, True, True, ECONOMICS["no gates"], old_policy))
        self.assertEqual(result["hard_disqualifiers"], ["MATERIAL_SPEND_UNRECONCILED"])

    def test_malformed_input_raises_the_oracle_exception(self):
        cases = {
            "overlap not a number": ({"overlap_count": "abc"}, ECONOMICS["available"]),
            "overlap not finite": ({"overlap_count": float("inf")}, ECONOMICS["available"]),
            "origin unprintable": ({"origin_attribution": Unprintable()}, ECONOMICS["available"]),  # caught: UNKNOWN
            "savings not a number": ({}, {**ECONOMICS["available"], "successful_realized_savings": "abc"}),
            "gates not a mapping": ({}, {**ECONOMICS["available"], "quality_gates": 5}),
            "absent gates, malformed observation": ({"price_coverage_pct": "NaN"}, ECONOMICS["no gates"]),
            "observation not a mapping": (5, ECONOMICS["available"]),
        }
        for label, (observation, economic) in cases.items():
            with self.subTest(case=label):
                new_policy, old_policy = both(None)
                app = {"implementation_conformance": "EXACT"}
                self.assertEqual(outcome(attribution.attribution_result, app, observation, True, True, economic, new_policy),
                                 outcome(oengine.attribution_result, app, observation, True, True, economic, old_policy))


class AttributionCaseTests(unittest.TestCase):
    def attribute(self, observation=None, economic=None, passed=True, eligible=True, app=None, policy=None):
        return attribution.attribution_result(app or {"implementation_conformance": "EXACT"}, {"origin_attribution": "CONFIRMED", **(observation or {})},
                                              passed, eligible, economic or ECONOMICS["available"], policy or engine.RealizerPolicy())

    def test_claimable(self):
        result = self.attribute()
        self.assertEqual((result["attribution_claim_status"], result["recommendation_attributed_realized_savings"], result["hard_disqualifiers"]),
                         ("CLAIMABLE", "1000.00", []))
        self.assertEqual((result["attribution_scope"], result["overlap_count"]), ("CANDIDATE", 0))

    def test_overlap_is_never_claimed(self):
        result = self.attribute({"overlap_count": 2})
        self.assertEqual(result["hard_disqualifiers"], ["PORTFOLIO_SCOPE_OVERLAP"])
        self.assertEqual((result["attribution_claim_status"], result["attribution_state"]), ("OBSERVABLE_ONLY", "CORRELATED_ONLY"))
        self.assertEqual((result["validated_observed_realization"], result["recommendation_attributed_realized_savings"]), ("1000.00", "0.00"))

    def test_each_gate_and_disqualifier(self):
        for gate in GATES:
            with self.subTest(gate=gate):
                result = self.attribute(economic=ECONOMICS[f"{gate} fails"])
                self.assertEqual(result["hard_disqualifiers"], [DISQUALIFIER[gate]])
                self.assertEqual(result["recommendation_attributed_realized_savings"], "0.00")
        everything = self.attribute({"origin_attribution": "INDEPENDENT", "overlap_count": 1}, passed=False, eligible=False,
                                    app={"implementation_conformance": "CONTRARY"})
        self.assertEqual(everything["hard_disqualifiers"], ["APPLIED_STATE_UNMATCHED", "HARD_VALIDATION_FAILURE", "NO_DEFENSIBLE_COUNTERFACTUAL",
                                                            "PORTFOLIO_SCOPE_OVERLAP", "ORIGIN_NOT_ATTRIBUTABLE"])
        self.assertEqual(everything["attribution_claim_status"], "NOT_ATTRIBUTABLE")

    def test_confirmed_origin_policy(self):
        inferred = self.attribute({"origin_attribution": "INFERRED"})
        self.assertEqual((inferred["attribution_claim_status"], inferred["attribution_state"]), ("OBSERVABLE_ONLY", "PARTIAL_ATTRIBUTION"))
        relaxed = self.attribute({"origin_attribution": "INFERRED"}, policy=engine.RealizerPolicy.from_mapping({"require_confirmed_origin_for_hard_kpi": False}))
        self.assertEqual(relaxed["attribution_claim_status"], "CLAIMABLE")
        self.assertEqual(self.attribute({"origin_attribution": "garbage"})["origin_attribution"], "UNKNOWN")

    def test_unavailable_economics_are_not_attributable(self):
        result = self.attribute(economic=ECONOMICS["unavailable"])
        self.assertEqual((result["attribution_claim_status"], result["recommendation_attributed_realized_savings"]), ("NOT_ATTRIBUTABLE", "0.00"))


class CumulativeRealizationTests(unittest.TestCase):
    def test_no_double_count(self):
        # Incremental realizations of 100 + 60 + 50 sum to 210, but only 1000 - 810 = 190 was realized overall: the
        # authoritative value is measured against the original counterfactual, the sum is diagnostic only.
        self.assertEqual(attribution.authoritative_cumulative_realization("1000", "810", ["100", "60", "50"]),
                         {"authoritative": "190.00", "diagnostic_incremental_sum": "210.00", "interaction_delta": "-20.00"})
        self.assertEqual(attribution.authoritative_cumulative_realization("1000", "810"),
                         {"authoritative": "190.00", "diagnostic_incremental_sum": "0.00", "interaction_delta": "190.00"})

    def test_cases_equal_the_oracle(self):
        cases = [("1000", "810", ["100", "60", "50"]), (None, "810", []), ("1000", None, ["1"]), ("1000", "1200", [None, "5"]),
                 (1000, 999.995, ("0.001",)), ("abc", "1", []), ("1", "NaN", []), ("1000", "810", ["x"]), ("1000", "810", 5)]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(outcome(attribution.authoritative_cumulative_realization, *args),
                                 outcome(oengine.authoritative_cumulative_realization, *args))


if __name__ == "__main__":
    unittest.main()
