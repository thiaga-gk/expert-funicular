"""Realizer reconciler matching (src/realizer/reconciler/matching.py).

``_directional_relation``, ``compare_step`` and ``choose_application`` return the values (or raise the exception type
and message) stored in ``tests/fixtures/realizer/expectations/`` on valid and malformed input. ``match`` gathers the
plan-authority statements (selected scenario, authority changes, scenario scope) the single-file release made inline
and returns the stored values for every observation of every scenario and on malformed packages.
"""
import copy
import itertools
import json
import unittest
from decimal import Decimal
from enum import Enum

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import canonical, fixture_package, outcome  # noqa: E402
from realizer.analyzer.analysis import observed_config, package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.reconciliation import PlanMatch  # noqa: E402
from realizer.reconciler import matching  # noqa: E402

MIGRATION = "SCN-1DFDCB17E0622D63C6758AB4"
VALUES = [None, 0, 1, 2, 3, 4, -1, 30, 120, "1", "2.5", " 3 ", "abc", "", True, False, 1.5, float("nan"), "NaN", "Infinity",
          Decimal("2"), [1], {"a": 1}, "MEDIUM", "LARGE", "SMALL", "X_LARGE", "medium", "PRO", "SERVERLESS"]
KNOBS = ["warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes", "enable_photon", "warehouse_type",
         "warehouse_channel", "unknown_knob", None]


def plain(value):
    """Enums by value, containers recursively, everything else as is."""
    if isinstance(value, Enum):
        return ("enum", value.value)
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def as_tuple(result: PlanMatch):
    return result.selected_scenario_id, list(result.step_matches), result.primary, list(result.authority_changes), result.scenario_scope


def scenario_observations():
    for name in canonical.scenario_names():
        scenario = canonical.load_scenario(name)
        for index, step in enumerate(scenario["runs"]):
            if step.get("observation"):
                yield name, index, scenario["package_doc"], step["observation"]


def packages():
    fixture = fixture_package()
    two_step = json.loads((canonical.PACKAGES / "GR-019-two-ready-steps.json").read_text(encoding="utf-8"))
    return [fixture, two_step]


def plan_steps(package):
    for scenario in package["scenarios"]:
        for step in scenario["execution_plan"]:
            yield step


class DirectionalRelationTests(unittest.TestCase):
    def test_every_value_triple(self):
        small = [None, 0, 1, 2, 3, -1, "2", "2.5", "abc", True, 1.5, float("nan"), "NaN", "Infinity", Decimal("3"), [1]]
        results = [plain(outcome(matching._directional_relation, source, recommended, observed))
                   for source, recommended, observed in itertools.product(small, repeat=3)]
        self.assertEqual(len(results), len(small) ** 3)
        expect.check(self, "results", results, spot={"count": len(results), "distinct": sorted({json.dumps(r, default=str) for r in results})})

    def test_relations(self):
        self.assertEqual(plain(matching._directional_relation(120, 30, 60)), [("enum", "MODIFIED"), "LESS_AGGRESSIVE"])
        self.assertEqual(plain(matching._directional_relation(4, 3, 2)), [("enum", "MODIFIED"), "MORE_AGGRESSIVE"])
        self.assertEqual(plain(matching._directional_relation(4, 3, 5)), [("enum", "CONTRARY"), None])
        self.assertEqual(plain(matching._directional_relation(4, 4, 5)), [("enum", "UNRELATED"), None])
        self.assertEqual(plain(matching._directional_relation("x", 3, 5)), [("enum", "UNRELATED"), None])


class CompareStepTests(unittest.TestCase):
    def test_every_plan_step_against_every_observed_configuration(self):
        configs = [json.loads(c) for c in sorted({json.dumps(observed_config(o, p), sort_keys=True) for _, _, p, o in scenario_observations()})]
        results = []
        for package in packages():
            for step in plan_steps(package):
                for obs_cfg in configs:
                    results.append(plain(outcome(matching.compare_step, step, obs_cfg)))
        self.assertGreater(len(results), 100)
        expect.check(self, "results", results, spot={"count": len(results), "distinct": sorted({json.dumps(r) for r in results})})

    def test_every_knob_and_value_combination(self):
        triples = [(None, 1, 2), (4, 3, 2), (4, 3, 3), (4, 3, 4), (4, 3, 5), (4, 3, "x"), ("LARGE", "MEDIUM", "SMALL"),
                   ("LARGE", "MEDIUM", "X_LARGE"), ("LARGE", "MEDIUM", "medium"), ("LARGE", "MEDIUM", "HUGE"), (True, False, True),
                   (True, False, "yes"), ("PRO", "SERVERLESS", "CLASSIC"), ("CURRENT", "PREVIEW", "OTHER"), (1, 1, 2)]
        results = []
        for knob in KNOBS:
            for source, recommended, observed in triples + [(a, b, c) for a, b, c in itertools.product(VALUES[:12], repeat=3)][:300]:
                step = {"knob": knob, "current_value": source, "recommended_value": recommended}
                for obs_cfg in ({knob: observed} if knob is not None else {"None": observed}, {}):
                    results.append(plain(outcome(matching.compare_step, step, obs_cfg)))
        self.assertGreater(len(results), 5000)
        expect.check(self, "results", results, spot={"count": len(results), "kinds": sorted({r[0] for r in results}),
                                                   "distinct": sorted({json.dumps(r) for r in results if r[0] == "ok"})})

    def test_malformed_input_raises(self):
        for index, (step, obs_cfg) in enumerate(((5, {}), ({"knob": "x"}, 5), (None, {}), ({"knob": "max_clusters"}, None), ("abc", {"x": 1}))):
            with self.subTest(step=step, obs_cfg=obs_cfg):
                new = outcome(matching.compare_step, step, obs_cfg)
                self.assertNotEqual(new[0], "ok")
                expect.check(self, f"case{index}", new)


class ChooseApplicationTests(unittest.TestCase):
    def test_every_scenario_observation_and_scenario_choice(self):
        results = {}
        for name, index, package, observation in scenario_observations():
            obs_cfg = observed_config(observation, package)
            for scenario_id in (None, package["recommended_plan"]["scenario_id"], MIGRATION, "SCN-UNKNOWN", ""):
                results[f"{name}/{index}/{scenario_id}"] = plain(outcome(matching.choose_application, copy.deepcopy(package), obs_cfg, scenario_id=scenario_id))
        self.assertGreater(len(results), 200)
        expect.check(self, "results", results, spot={"count": len(results), "kinds": sorted({r[0] for r in results.values()})})

    def test_primary_is_the_first_authoritative_change_and_one_of_the_matches(self):
        package = packages()[1]
        obs_cfg = observed_config({"config": {"warehouse_size": "MEDIUM", "max_clusters": 3}}, package)
        results, primary = matching.choose_application(package, obs_cfg)
        self.assertIs(primary, results[0])
        self.assertEqual([r["implementation_conformance"] for r in results], ["EXACT", "EXACT", "NOT_APPLIED", "NOT_APPLIED"])
        self.assertEqual(primary["step"], 1)

    def test_malformed_packages_raise(self):
        obs_cfg = observed_config(demo_observation(fixture_package()), fixture_package())
        for label, corrupt in malformed_packages().items():
            package = copy.deepcopy(fixture_package())
            corrupt(package)
            with self.subTest(case=label):
                new = outcome(matching.choose_application, copy.deepcopy(package), obs_cfg)
                self.assertNotEqual(new[0], "ok", label)
                expect.check(self, label, new)


def malformed_packages():
    def recommended(package):
        sid = package["recommended_plan"]["scenario_id"]
        return next(s for s in package["scenarios"] if s["scenario_id"] == sid)

    def step_not_a_number(p):
        recommended(p)["execution_plan"][0]["step"] = "abc"

    def step_not_a_mapping(p):
        recommended(p)["execution_plan"].insert(0, 5)

    def invalidates_not_iterable(p):
        recommended(p)["execution_plan"][0]["invalidates"] = 5

    def validation_not_a_mapping(p):
        recommended(p)["execution_plan"][0]["validation"] = 5

    def execution_plan_not_a_list(p):
        recommended(p)["execution_plan"] = 5

    def scenario_not_a_mapping(p):
        p["scenarios"].insert(0, 5)

    def scenarios_missing_and_plan_not_a_mapping(p):
        p["recommended_plan"] = 5

    return {f.__name__: f for f in (step_not_a_number, step_not_a_mapping, invalidates_not_iterable, validation_not_a_mapping,
                                    execution_plan_not_a_list, scenario_not_a_mapping, scenarios_missing_and_plan_not_a_mapping)}


class MatchTests(unittest.TestCase):
    def test_match_for_every_scenario_observation(self):
        seen = {"scope": 0, "changes": 0, "primary": 0, "other_scenario": 0}
        results = {}
        for name, index, package, observation in scenario_observations():
            obs_cfg = observed_config(observation, package)
            with self.subTest(scenario=name, run=index):
                result = matching.match(copy.deepcopy(package), observation, obs_cfg)
                self.assertIsInstance(result, PlanMatch)
                results[f"{name}/{index}"] = plain(as_tuple(result))
                seen["scope"] += result.scenario_scope
                seen["changes"] += bool(result.authority_changes)
                seen["primary"] += result.primary is not None
                seen["other_scenario"] += bool(observation.get("applied_scenario_id"))
        self.assertTrue(all(seen.values()), seen)
        expect.check(self, "results", results, spot={"count": len(results), **seen})

    def test_primary_and_authority_changes_are_the_step_matches_themselves(self):
        _, _, package, observation = next((n, i, p, o) for n, i, p, o in scenario_observations() if n == "GR-019")
        result = matching.match(package, observation, observed_config(observation, package))
        self.assertTrue(result.scenario_scope)
        self.assertEqual(len(result.authority_changes), 2)
        for change in result.authority_changes:
            self.assertTrue(any(change is m for m in result.step_matches))
        self.assertIs(result.primary, result.step_matches[0])

    def test_scenario_scope_triggers(self):
        package = fixture_package()
        base = demo_observation(package)
        source = package_config(package)
        cases = {
            "one authoritative change": (base, False),
            "migration scenario": ({**base, "applied_scenario_id": MIGRATION, "config": {**source, "warehouse_type": "SERVERLESS"}}, True),
            "another scenario id": ({**base, "applied_scenario_id": "SCN-UNKNOWN"}, True),
            "nothing applied": ({**base, "config": dict(source)}, False),
        }
        for label, (observation, expected) in cases.items():
            with self.subTest(case=label):
                obs_cfg = observed_config(observation, package)
                self.assertEqual(matching.match(package, observation, obs_cfg).scenario_scope, expected)

    def test_malformed_input_raises(self):
        observation = demo_observation(fixture_package())
        obs_cfg = observed_config(observation, fixture_package())
        cases = [(label, corrupt, observation) for label, corrupt in malformed_packages().items()]
        cases.append(("observation not a mapping", lambda p: None, 5))
        for label, corrupt, obs in cases:
            package = copy.deepcopy(fixture_package())
            corrupt(package)
            with self.subTest(case=label):
                new = outcome(matching.match, copy.deepcopy(package), obs, obs_cfg)
                self.assertNotEqual(new[0], "ok", label)
                expect.check(self, label, new)

    def test_match_is_frozen(self):
        package = fixture_package()
        observation = demo_observation(package)
        result = matching.match(package, observation, observed_config(observation, package))
        with self.assertRaises(Exception):
            result.scenario_scope = True


if __name__ == "__main__":
    unittest.main()
