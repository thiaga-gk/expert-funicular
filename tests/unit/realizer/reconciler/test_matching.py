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
from tests.unit.realizer.helpers import canonical, fixture_package, migration_scenario_id, outcome  # noqa: E402
from realizer.analyzer.analysis import candidate_steps, observed_config, package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.reconciliation import PlanMatch  # noqa: E402
from realizer.reconciler import matching  # noqa: E402

MIGRATION = migration_scenario_id(fixture_package())
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
    return [fixture, fixture_package("two-ready-steps")]


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
        size, auto_stop = (s for s in candidate_steps(package) if s["step"] in (1, 2))
        obs_cfg = observed_config({"config": {size["knob"]: size["recommended_value"], auto_stop["knob"]: auto_stop["recommended_value"]}}, package)
        results, primary = matching.choose_application(package, obs_cfg)
        self.assertIs(primary, results[0])
        self.assertEqual([r["implementation_conformance"] for r in results], ["EXACT", "EXACT", "NOT_APPLIED"])   # size and auto-stop applied, max clusters not
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


class NormalizedComparisonTests(unittest.TestCase):
    """R05: package values and observed values are compared in one spelling, and auto-stop 0 means never."""

    def conformance(self, knob, source, recommended, observed):
        step = {"knob": knob, "current_value": source, "recommended_value": recommended}
        conf, relation = matching.compare_step(step, {knob: observed})
        return None if conf is None else conf.value, relation

    def test_sizes_match_in_the_publishers_spelling(self):
        for observed in ("MEDIUM", "medium", " medium ", "Medium"):
            with self.subTest(observed=observed):
                config = observed_config({"config": {"warehouse_size": observed}}, fixture_package())
                self.assertEqual(config["warehouse_size"], "MEDIUM")
                self.assertEqual(self.conformance("warehouse_size", "LARGE", "medium", config["warehouse_size"]), ("EXACT", None))
        # a recommended value written "medium" and a source written "large" are still the MEDIUM and LARGE steps
        self.assertEqual(self.conformance("warehouse_size", "large", "medium", "MEDIUM"), ("EXACT", None))
        self.assertEqual(self.conformance("warehouse_size", " large ", "medium", "LARGE"), ("NOT_APPLIED", None))

    def test_hyphenated_sizes_are_the_underscored_ones(self):
        package = fixture_package()
        for observed, expected in (("X-Large", "X_LARGE"), ("x-large", "X_LARGE"), ("2X-Small", "2X_SMALL"), (" 2x-large ", "2X_LARGE")):
            with self.subTest(observed=observed):
                self.assertEqual(observed_config({"config": {"warehouse_size": observed}}, package)["warehouse_size"], expected)
        self.assertEqual(self.conformance("warehouse_size", "LARGE", "X-Large", "X_LARGE"), ("EXACT", None))
        self.assertEqual(self.conformance("warehouse_size", "Large", "MEDIUM", "X_LARGE"), ("CONTRARY", None))
        self.assertEqual(self.conformance("warehouse_size", "X-Large", "SMALL", "MEDIUM"), ("MODIFIED", "LESS_AGGRESSIVE"))

    def test_observed_x_large_is_contrary_to_a_recommended_cut_end_to_end(self):
        package = fixture_package()
        observation = demo_observation(package)
        observation["config"]["warehouse_size"] = "X-Large"
        results, primary = matching.choose_application(package, observed_config(observation, package), scenario_id=package["recommended_plan"]["scenario_id"])
        size = next(r for r in results if r["knob"] == "warehouse_size")
        self.assertEqual((size["implementation_conformance"], size["observed_value"]), ("CONTRARY", "X_LARGE"))

    def test_type_and_channel_are_upper_cased(self):
        config = observed_config({"config": {"warehouse_type": " serverless ", "warehouse_channel": "preview"}}, fixture_package())
        self.assertEqual((config["warehouse_type"], config["warehouse_channel"]), ("SERVERLESS", "PREVIEW"))
        self.assertEqual(self.conformance("warehouse_type", "pro", "serverless", "SERVERLESS"), ("EXACT", None))
        self.assertEqual(self.conformance("warehouse_channel", "current", "preview", "PREVIEW"), ("EXACT", None))
        self.assertEqual(self.conformance("warehouse_type", "classic", "serverless", "PRO"), ("UNRELATED", None))

    def test_auto_stop_zero_is_never_stop_and_longer_than_any_value(self):
        self.assertEqual(matching.NEVER_STOP_MINUTES, 10 ** 9)
        # an observed 0 against a recommended cut is the opposite direction
        self.assertEqual(self.conformance("auto_stop_minutes", 60, 15, 0), ("CONTRARY", None))
        self.assertEqual(self.conformance("auto_stop_minutes", 35, 10, 0), ("CONTRARY", None))
        # a source of 0 (never stops): any finite recommendation is a cut, and a finite observation in between is the same direction
        self.assertEqual(self.conformance("auto_stop_minutes", 0, 35, 60), ("MODIFIED", "LESS_AGGRESSIVE"))
        self.assertEqual(self.conformance("auto_stop_minutes", 0, 35, 10), ("MODIFIED", "MORE_AGGRESSIVE"))
        self.assertEqual(self.conformance("auto_stop_minutes", 0, 35, 35), ("EXACT", None))
        self.assertEqual(self.conformance("auto_stop_minutes", 0, 35, 0), ("NOT_APPLIED", None))
        # a recommendation of 0 (stop never) is a lengthening: a shorter observation is the opposite direction
        self.assertEqual(self.conformance("auto_stop_minutes", 15, 0, 10), ("CONTRARY", None))
        self.assertEqual(self.conformance("auto_stop_minutes", 15, 0, 30), ("MODIFIED", "LESS_AGGRESSIVE"))
        # nonzero values are unchanged
        self.assertEqual(self.conformance("auto_stop_minutes", 120, 30, 60), ("MODIFIED", "LESS_AGGRESSIVE"))
        self.assertEqual(self.conformance("auto_stop_minutes", 120, 30, 10), ("MODIFIED", "MORE_AGGRESSIVE"))
        self.assertEqual(self.conformance("auto_stop_minutes", 120, 30, 200), ("CONTRARY", None))

    def test_auto_stop_zero_observed_end_to_end_is_contrary(self):
        package = fixture_package()
        observation = demo_observation(package)
        observation["config"] = {**package_config(package), "auto_stop_minutes": 0}
        results, _ = matching.choose_application(package, observed_config(observation, package), scenario_id=package["recommended_plan"]["scenario_id"])
        step = next(r for r in results if r["knob"] == "auto_stop_minutes")
        self.assertEqual(step["implementation_conformance"], "CONTRARY")

    def test_a_size_outside_the_registry_is_unrelated(self):
        self.assertEqual(self.conformance("warehouse_size", "LARGE", "MEDIUM", "GIGANTIC"), ("UNRELATED", None))
        self.assertEqual(self.conformance("warehouse_size", "LARGE", "MEDIUM", "SMALL"), ("MODIFIED", "MORE_AGGRESSIVE"))


class AppliedBeforePackageTests(unittest.TestCase):
    """R03: a change applied before the package was generated is never an application of it."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.generated = cls.pkg["provenance"]["generated_at"]
        cls.base = demo_observation(cls.pkg)

    def test_the_predicate(self):
        self.assertEqual(self.generated, self.base["applied_at_utc"], "the demo applies the change at the generation instant")
        self.assertFalse(matching.applied_before_package(self.pkg, self.base))
        before = {**self.base, "applied_at_utc": "2026-09-29T12:00:00Z"}
        self.assertTrue(matching.applied_before_package(self.pkg, before))
        self.assertTrue(matching.applied_before_package(self.pkg, {**self.base, "applied_at_utc": "2026-10-01T11:59:59Z"}))
        self.assertTrue(matching.applied_before_package(self.pkg, {**self.base, "applied_at_utc": "2026-10-01T13:59:59+02:00"}))
        self.assertFalse(matching.applied_before_package(self.pkg, {**self.base, "applied_at_utc": "2026-10-01T12:00:01Z"}))
        # no applied time, or no generation time: nothing to compare
        self.assertFalse(matching.applied_before_package(self.pkg, {**self.base, "applied_at_utc": None}))
        self.assertFalse(matching.applied_before_package({**self.pkg, "provenance": {}}, before))

    def test_every_changed_step_is_an_independent_change_and_nothing_carries_authority(self):
        observation = {**self.base, "applied_at_utc": "2026-09-29T12:00:00Z"}   # two days before generated_at
        result = matching.match(self.pkg, observation, observed_config(observation, self.pkg))
        self.assertIsNone(result.primary)
        self.assertEqual(result.authority_changes, ())
        self.assertFalse(result.scenario_scope)
        self.assertTrue(result.step_matches)
        for item in result.step_matches:
            self.assertFalse(item["authority_eligible"])
            self.assertEqual(item["independent_reason"], "APPLIED_BEFORE_PACKAGE")
        size = next(item for item in result.step_matches if item["knob"] == "warehouse_size")
        self.assertEqual(size["implementation_conformance"], "EXACT", "the match itself is still described")

    def test_an_application_at_or_after_generation_keeps_its_authority(self):
        result = matching.match(self.pkg, self.base, observed_config(self.base, self.pkg))
        self.assertIsNotNone(result.primary)
        self.assertTrue(result.authority_changes)
        self.assertFalse(any("independent_reason" in item for item in result.step_matches))


if __name__ == "__main__":
    unittest.main()
