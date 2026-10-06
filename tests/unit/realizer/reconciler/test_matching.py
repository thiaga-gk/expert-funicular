"""Realizer reconciler matching (src/realizer/reconciler/matching.py) against the frozen oracle.

``_directional_relation``, ``compare_step`` and ``choose_application`` are moved unchanged, so each returns the oracle's
value, or raises the oracle's exception type and message, on valid and malformed input. ``match`` gathers the
oracle's inline plan-authority statements (selected scenario, authority changes, scenario scope) and equals them for
every observation of every scenario and on malformed packages.
"""
import copy
import itertools
import json
import unittest
from decimal import Decimal
from enum import Enum

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome  # noqa: E402
from realizer.contracts.reconciliation import PlanMatch  # noqa: E402
from realizer.reconciler import matching  # noqa: E402

MIGRATION = "SCN-1DFDCB17E0622D63C6758AB4"
VALUES = [None, 0, 1, 2, 3, 4, -1, 30, 120, "1", "2.5", " 3 ", "abc", "", True, False, 1.5, float("nan"), "NaN", "Infinity",
          Decimal("2"), [1], {"a": 1}, "MEDIUM", "LARGE", "SMALL", "X_LARGE", "medium", "PRO", "SERVERLESS"]
KNOBS = ["warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes", "enable_photon", "warehouse_type",
         "warehouse_channel", "unknown_knob", None]


def plain(value):
    """Enums (two different classes in the two engines) by value, containers recursively, everything else as is."""
    if isinstance(value, Enum):
        return ("enum", value.value)
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def same(test, new, old):
    """Compare two ``outcome`` results; NaN never equals itself, so compare its text form instead."""
    test.assertEqual(repr(plain(new)), repr(plain(old)))


def oracle_match(package, observation, obs_cfg):
    """The oracle's inline plan-authority statements at the start of its lifecycle (``process_observation``)."""
    rc = oengine.rc
    selected_scenario_id = observation.get("applied_scenario_id") or (package.get("recommended_plan") or {}).get("scenario_id")
    selected_scenario = oengine.scenario_by_id(package, selected_scenario_id)
    step_results, primary = oengine.choose_application(package, obs_cfg, scenario_id=selected_scenario_id)
    authority_changes = [
        item for item in step_results
        if item.get("authority_eligible") and item.get("implementation_conformance") in {
            rc.ImplementationConformance.EXACT.value,
            rc.ImplementationConformance.MODIFIED.value,
            rc.ImplementationConformance.CONTRARY.value,
        }
    ]
    scenario_scope = bool(
        len(authority_changes) > 1
        or (selected_scenario and selected_scenario.get("scenario_type") == "WAREHOUSE_TYPE_MIGRATION")
        or selected_scenario_id != (package.get("recommended_plan") or {}).get("scenario_id")
    )
    return selected_scenario_id, step_results, primary, authority_changes, scenario_scope


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


class ReExportTests(unittest.TestCase):
    def test_engine_names_are_the_moved_functions(self):
        for name in ("_directional_relation", "compare_step", "choose_application"):
            self.assertIs(getattr(engine, name), getattr(matching, name), name)

    def test_engine_runs_the_reconciler_module(self):
        from realizer.reconciler import reconciler
        self.assertIs(engine.reconciler, reconciler)


class DirectionalRelationTests(unittest.TestCase):
    def test_every_value_triple_equals_the_oracle(self):
        small = [None, 0, 1, 2, 3, -1, "2", "2.5", "abc", True, 1.5, float("nan"), "NaN", "Infinity", Decimal("3"), [1]]
        count = 0
        for source, recommended, observed in itertools.product(small, repeat=3):
            new = outcome(matching._directional_relation, source, recommended, observed)
            old = outcome(oengine._directional_relation, source, recommended, observed)
            same(self, new, old)
            count += 1
        self.assertEqual(count, len(small) ** 3)

    def test_relations(self):
        self.assertEqual(plain(matching._directional_relation(120, 30, 60)), [("enum", "MODIFIED"), "LESS_AGGRESSIVE"])
        self.assertEqual(plain(matching._directional_relation(4, 3, 2)), [("enum", "MODIFIED"), "MORE_AGGRESSIVE"])
        self.assertEqual(plain(matching._directional_relation(4, 3, 5)), [("enum", "CONTRARY"), None])
        self.assertEqual(plain(matching._directional_relation(4, 4, 5)), [("enum", "UNRELATED"), None])
        self.assertEqual(plain(matching._directional_relation("x", 3, 5)), [("enum", "UNRELATED"), None])


class CompareStepTests(unittest.TestCase):
    def test_every_plan_step_against_every_observed_configuration(self):
        configs = [json.loads(c) for c in {json.dumps(oengine.observed_config(o, p), sort_keys=True) for _, _, p, o in scenario_observations()}]
        count = 0
        for package in packages():
            for step in plan_steps(package):
                for obs_cfg in configs:
                    same(self, outcome(matching.compare_step, step, obs_cfg), outcome(oengine.compare_step, step, obs_cfg))
                    count += 1
        self.assertGreater(count, 100)

    def test_every_knob_and_value_combination(self):
        triples = [(None, 1, 2), (4, 3, 2), (4, 3, 3), (4, 3, 4), (4, 3, 5), (4, 3, "x"), ("LARGE", "MEDIUM", "SMALL"),
                   ("LARGE", "MEDIUM", "X_LARGE"), ("LARGE", "MEDIUM", "medium"), ("LARGE", "MEDIUM", "HUGE"), (True, False, True),
                   (True, False, "yes"), ("PRO", "SERVERLESS", "CLASSIC"), ("CURRENT", "PREVIEW", "OTHER"), (1, 1, 2)]
        for knob in KNOBS:
            for source, recommended, observed in triples + [(a, b, c) for a, b, c in itertools.product(VALUES[:12], repeat=3)][:300]:
                step = {"knob": knob, "current_value": source, "recommended_value": recommended}
                for obs_cfg in ({knob: observed} if knob is not None else {"None": observed}, {}):
                    with self.subTest(knob=knob, values=(source, recommended, observed)):
                        same(self, outcome(matching.compare_step, step, obs_cfg), outcome(oengine.compare_step, step, obs_cfg))

    def test_malformed_input_raises_the_oracle_exception(self):
        for step, obs_cfg in ((5, {}), ({"knob": "x"}, 5), (None, {}), ({"knob": "max_clusters"}, None), ("abc", {"x": 1})):
            with self.subTest(step=step, obs_cfg=obs_cfg):
                new, old = outcome(matching.compare_step, step, obs_cfg), outcome(oengine.compare_step, step, obs_cfg)
                self.assertNotEqual(old[0], "ok")
                same(self, new, old)


class ChooseApplicationTests(unittest.TestCase):
    def test_every_scenario_observation_and_scenario_choice(self):
        count = 0
        for name, index, package, observation in scenario_observations():
            obs_cfg = oengine.observed_config(observation, package)
            for scenario_id in (None, package["recommended_plan"]["scenario_id"], MIGRATION, "SCN-UNKNOWN", ""):
                with self.subTest(scenario=name, run=index, scenario_id=scenario_id):
                    new = outcome(matching.choose_application, copy.deepcopy(package), obs_cfg, scenario_id=scenario_id)
                    old = outcome(oengine.choose_application, copy.deepcopy(package), obs_cfg, scenario_id=scenario_id)
                    same(self, new, old)
                    count += 1
        self.assertGreater(count, 200)

    def test_primary_is_the_first_authoritative_change_and_one_of_the_matches(self):
        package = packages()[1]
        obs_cfg = oengine.observed_config({"config": {"warehouse_size": "MEDIUM", "max_clusters": 3}}, package)
        results, primary = matching.choose_application(package, obs_cfg)
        self.assertIs(primary, results[0])
        self.assertEqual([r["implementation_conformance"] for r in results], ["EXACT", "EXACT", "NOT_APPLIED", "NOT_APPLIED"])
        self.assertEqual(primary["step"], 1)

    def test_malformed_packages_raise_the_oracle_exception(self):
        obs_cfg = oengine.observed_config(oengine.demo_observation(fixture_package()), fixture_package())
        for label, corrupt in malformed_packages().items():
            package = copy.deepcopy(fixture_package())
            corrupt(package)
            with self.subTest(case=label):
                new = outcome(matching.choose_application, copy.deepcopy(package), obs_cfg)
                old = outcome(oengine.choose_application, copy.deepcopy(package), obs_cfg)
                self.assertNotEqual(old[0], "ok", label)
                same(self, new, old)


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
    def test_match_equals_the_oracle_for_every_scenario_observation(self):
        seen = {"scope": 0, "changes": 0, "primary": 0, "other_scenario": 0}
        for name, index, package, observation in scenario_observations():
            obs_cfg = oengine.observed_config(observation, package)
            with self.subTest(scenario=name, run=index):
                result = matching.match(copy.deepcopy(package), observation, obs_cfg)
                self.assertIsInstance(result, PlanMatch)
                same(self, as_tuple(result), oracle_match(copy.deepcopy(package), observation, obs_cfg))
                seen["scope"] += result.scenario_scope
                seen["changes"] += bool(result.authority_changes)
                seen["primary"] += result.primary is not None
                seen["other_scenario"] += bool(observation.get("applied_scenario_id"))
        self.assertTrue(all(seen.values()), seen)

    def test_primary_and_authority_changes_are_the_step_matches_themselves(self):
        _, _, package, observation = next((n, i, p, o) for n, i, p, o in scenario_observations() if n == "GR-019")
        result = matching.match(package, observation, oengine.observed_config(observation, package))
        self.assertTrue(result.scenario_scope)
        self.assertEqual(len(result.authority_changes), 2)
        for change in result.authority_changes:
            self.assertTrue(any(change is m for m in result.step_matches))
        self.assertIs(result.primary, result.step_matches[0])

    def test_scenario_scope_triggers(self):
        package = fixture_package()
        base = oengine.demo_observation(package)
        source = oengine.package_config(package)
        cases = {
            "one authoritative change": (base, False),
            "migration scenario": ({**base, "applied_scenario_id": MIGRATION, "config": {**source, "warehouse_type": "SERVERLESS"}}, True),
            "another scenario id": ({**base, "applied_scenario_id": "SCN-UNKNOWN"}, True),
            "nothing applied": ({**base, "config": dict(source)}, False),
        }
        for label, (observation, expected) in cases.items():
            with self.subTest(case=label):
                obs_cfg = oengine.observed_config(observation, package)
                self.assertEqual(matching.match(package, observation, obs_cfg).scenario_scope, expected)
                self.assertEqual(oracle_match(package, observation, obs_cfg)[4], expected)

    def test_malformed_input_raises_the_oracle_exception(self):
        observation = oengine.demo_observation(fixture_package())
        obs_cfg = oengine.observed_config(observation, fixture_package())
        cases = [(label, corrupt, observation) for label, corrupt in malformed_packages().items()]
        cases.append(("observation not a mapping", lambda p: None, 5))
        for label, corrupt, obs in cases:
            package = copy.deepcopy(fixture_package())
            corrupt(package)
            with self.subTest(case=label):
                new = outcome(matching.match, copy.deepcopy(package), obs, obs_cfg)
                old = outcome(oracle_match, copy.deepcopy(package), obs, obs_cfg)
                self.assertNotEqual(old[0], "ok", label)
                self.assertEqual(new, old)

    def test_match_is_frozen(self):
        package = fixture_package()
        observation = oengine.demo_observation(package)
        result = matching.match(package, observation, oengine.observed_config(observation, package))
        with self.assertRaises(Exception):
            result.scenario_scope = True


if __name__ == "__main__":
    unittest.main()
