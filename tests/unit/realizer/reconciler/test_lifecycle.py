"""Realizer reconciler lifecycle (src/realizer/reconciler/lifecycle.py) against the frozen oracle.

``regeneration_request`` is the RERUN id and payload part of the oracle's ``request_regeneration`` and
``upstream_regeneration_request`` its downstream-invalidation rule; both equal the oracle on valid and malformed input.
The engine's ``request_regeneration`` keeps its signature and writes what the oracle wrote. The lifecycle scenarios
(drift, reuse of the active episode, rollback, supersession, contrary, scenario scope, migration, modified, pending
downstream, not ready) check the decided events directly and the whole output tree against the oracle.
"""
import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome, run_sequence, tree  # noqa: E402
from realizer.contracts.reconciliation import ActiveApplication, PlannedApplication, ReconciliationResult, RegenerationRequest  # noqa: E402
from realizer.reconciler import lifecycle, reconciler  # noqa: E402

MIGRATION = "SCN-1DFDCB17E0622D63C6758AB4"
REASONS = ("CONFIGURATION_DRIFT", "CONTRARY_IMPLEMENTATION", "UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM")
RECORDED = "2026-10-09T12:00:00Z"
APP = "APP-" + "0123456789AB" * 2   # a well-formed application episode id (the oracle's ledger entry validates it)


def oracle_rerun(package, app_payload, observation, policy, reason):
    """The oracle's ``request_regeneration`` outcome as ``(id, application episode, payload)`` read back from what it
    wrote, or its exception."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        result = outcome(oengine.request_regeneration, copy.deepcopy(package), copy.deepcopy(app_payload), copy.deepcopy(observation),
                         out, policy, RECORDED, RECORDED, reason=reason)
        if result[0] != "ok":
            return result
        (rerun,) = (out / "realization" / "regeneration").glob("*.json")
        (ledger,) = (out / "realization" / "ledger").glob("*.jsonl")
        entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        return "ok", (rerun.stem, entry["application_episode_id"], json.loads(rerun.read_text(encoding="utf-8")))


def modular_rerun(fn, *args, **kwargs):
    result = outcome(fn, *args, **kwargs)
    if result[0] != "ok" or result[1] is None:
        return result
    request = result[1]
    return "ok", (request.regeneration_request_id, request.application_episode_id, json.loads(json.dumps(request.payload)))


def oracle_upstream(package, app_payload, observation, policy):
    """The oracle's downstream-invalidation rule (end of ``process_observation``) as an outcome, its request read back
    from what ``request_regeneration`` wrote."""
    try:
        if app_payload.get("invalidates") or app_payload.get("implementation_conformance") == oengine.rc.ImplementationConformance.MODIFIED.value:
            days = int(observation.get("elapsed_days", 0) or 0)
            required_days = int(app_payload.get("remeasure_days") or policy.remeasure_days_after_invalidating_change)
            if days >= required_days:
                return oracle_rerun(package, app_payload, observation, policy, "UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM")
        return "ok", None
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)


def stored_application_payloads():
    """Every application payload the stored baseline recorded, with its scenario's package and observations."""
    found = []
    for name in canonical.scenario_names():
        scenario = canonical.load_scenario(name)
        for ledger in (canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl"):
            for line in ledger.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["event_type"] in {"APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED", "INDEPENDENT_CHANGE_OBSERVED"}:
                    observation = next(r["observation"] for r in scenario["runs"] if r.get("observation"))
                    found.append((name, scenario["package_doc"], entry["payload"], observation))
    return found


def reconcile_runs(test, package, observations, policy=None):
    """Run *observations* through the modular engine into one directory, capturing each run's ``ReconciliationResult``
    (and the analysis it reconciled), and check the output tree equals the oracle's."""
    captured = []
    original = reconciler.run

    def capturing(context, analysis, governance):
        result = original(context, analysis, governance)
        captured.append((analysis, result))
        return result

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        outcomes = []
        with mock.patch.object(engine.reconciler, "run", capturing):
            for observation in observations:
                kwargs = {} if policy is None else {"policy": engine.RealizerPolicy.from_mapping(policy)}
                result = outcome(engine.realize, copy.deepcopy(package), copy.deepcopy(observation), out, **kwargs)
                outcomes.append(("ok", result[1].snapshot_id) if result[0] == "ok" else result)
        new_tree = tree(out)
    test.assertEqual((outcomes, new_tree), run_sequence(oengine, package, observations, policy), "same outcomes and tree as the oracle")
    return captured


class RegenerationRequestTests(unittest.TestCase):
    def test_every_stored_application_payload_and_reason_equals_the_oracle(self):
        payloads = stored_application_payloads()
        self.assertGreater(len(payloads), 20)
        policy, opolicy = engine.RealizerPolicy(), oengine.RealizerPolicy()
        for name, package, app_payload, observation in payloads:
            for reason in REASONS:
                with self.subTest(scenario=name, reason=reason):
                    new = modular_rerun(lifecycle.regeneration_request, package, app_payload, observation, policy, reason=reason)
                    self.assertEqual(new, oracle_rerun(package, app_payload, observation, opolicy, reason))

    def test_fallback_to_the_observed_configuration_and_other_policies(self):
        package = fixture_package()
        observation = oengine.demo_observation(package)
        variants = [{}, {"observed_config": None, "invalidates": None}, {"observed_config": {}, "scenario_id": "S", "step": 2},
                    {"observed_config": {"warehouse_size": "small"}, "invalidates": ["a", "b"], "application_episode_id": APP}]
        for app_payload, mapping in itertools.product(variants, (None, {"minimum_days": 1}, {"realized_ratio_min": "0.9"})):
            with self.subTest(app_payload=app_payload, policy=mapping):
                new = modular_rerun(lifecycle.regeneration_request, package, app_payload, observation, engine.RealizerPolicy.from_mapping(mapping), reason="R")
                self.assertEqual(new, oracle_rerun(package, app_payload, observation, oengine.RealizerPolicy.from_mapping(mapping), "R"))

    def test_malformed_input_raises_the_oracle_exception_and_writes_nothing(self):
        package = fixture_package()
        observation = oengine.demo_observation(package)
        cases = {
            "unhashable policy": (package, {}, observation, {"realized_ratio_min": "NaN"}),
            "configuration not a mapping": (package, {"observed_config": 5}, observation, None),
            "fallback configuration not a mapping": (package, {}, {**observation, "config": 5}, None),
            "package without id": ({k: v for k, v in package.items() if k != "recommendation_package_id"}, {}, observation, None),
            "package without configuration": ({k: v for k, v in package.items() if k != "current_config"}, {}, observation, None),
            "payload not a mapping": (package, 5, observation, None),
        }
        for label, (pkg, app_payload, obs, mapping) in cases.items():
            with self.subTest(case=label):
                old = oracle_rerun(pkg, app_payload, obs, oengine.RealizerPolicy.from_mapping(mapping), "R")
                self.assertNotEqual(old[0], "ok")
                new = modular_rerun(lifecycle.regeneration_request, pkg, app_payload, obs, engine.RealizerPolicy.from_mapping(mapping), reason="R")
                self.assertEqual(new, old)

    def test_engine_request_regeneration_writes_what_the_oracle_wrote(self):
        package = fixture_package()
        observation = oengine.demo_observation(package)
        for app_payload, mapping in (({}, None), ({"observed_config": {"warehouse_size": "SMALL"}, "application_episode_id": APP}, None),
                                     ({}, {"realized_ratio_min": "NaN"}), ({"observed_config": 5}, None)):
            with self.subTest(app_payload=app_payload, policy=mapping):
                trees = []
                for module in (engine, oengine):
                    with tempfile.TemporaryDirectory() as tmp:
                        out = Path(tmp)
                        result = outcome(module.request_regeneration, copy.deepcopy(package), dict(app_payload), observation, out,
                                         module.RealizerPolicy.from_mapping(mapping), RECORDED, RECORDED, reason="R")
                        trees.append((result, tree(out)))
                self.assertEqual(trees[0], trees[1])


class UpstreamRegenerationTests(unittest.TestCase):
    def test_rule_equals_the_oracle_over_its_inputs(self):
        package = fixture_package()
        observation = oengine.demo_observation(package)
        count = 0
        for invalidates, conformance, elapsed, remeasure, mapping in itertools.product(
            ([], ["min_clusters"], None), ("EXACT", "MODIFIED", "CONTRARY", None), (None, 0, 6, 7, 8, "8", "abc", 7.9),
            (None, 3, 10, "abc", 0), (None, {"remeasure_days_after_invalidating_change": 3}, {"remeasure_days_after_invalidating_change": "x"}),
        ):
            app_payload = {"invalidates": invalidates, "implementation_conformance": conformance, "remeasure_days": remeasure,
                           "observed_config": {"warehouse_size": "MEDIUM"}, "application_episode_id": APP, "scenario_id": "S", "step": 1}
            obs = {**observation, "elapsed_days": elapsed}
            new = modular_rerun(lifecycle.upstream_regeneration_request, package, app_payload, obs, engine.RealizerPolicy.from_mapping(mapping))
            old = oracle_upstream(package, app_payload, obs, oengine.RealizerPolicy.from_mapping(mapping))
            self.assertEqual(new, old, (invalidates, conformance, elapsed, remeasure, mapping))
            count += 1
        self.assertGreater(count, 1000)


class LifecycleScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)
        cls.source = oengine.package_config(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def scenario(self, name):
        scenario = canonical.load_scenario(name)
        return scenario["package_doc"], [r["observation"] for r in scenario["runs"]]

    def test_exact_application_opens_an_episode_and_validation_is_due(self):
        (first,) = [r for _, r in reconcile_runs(self, self.pkg, [self.base])]
        self.assertIsNone(first.drift)
        (app,) = first.applications
        self.assertIsInstance(app, PlannedApplication)
        self.assertEqual((app.event_type.value, app.payload["implementation_conformance"], app.payload["step"]), ("APPLICATION_OBSERVED", "EXACT", 1))
        self.assertEqual(first.active, ActiveApplication(app.application_episode_id, app.payload, is_previous=False))
        self.assertEqual(first.intent_episode_id, app.application_episode_id)
        self.assertEqual(first.intent, {"intent_state": "ACCEPTED", "actor": "demo-user", "reference": None})
        self.assertTrue(first.validation_due)
        self.assertIsNone(first.terminal_regeneration)
        upstream = first.upstream_regeneration.get()   # the size step invalidates downstream steps, 8 days >= 7
        self.assertIsInstance(upstream, RegenerationRequest)
        self.assertEqual(upstream.payload["reasons"], ["UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM"])
        self.assertEqual(upstream.application_episode_id, app.application_episode_id)

    def test_same_configuration_again_reuses_the_active_episode(self):
        first, second = [r for _, r in reconcile_runs(self, self.pkg, [self.base, self.later(10)])]
        self.assertEqual(second.applications, ())
        self.assertTrue(second.active.is_previous)
        self.assertEqual(second.active.application_episode_id, first.active.application_episode_id)
        self.assertIsNone(second.drift)
        self.assertTrue(second.validation_due)

    def test_reused_episode_still_records_other_changes(self):
        both = {**self.base, "config": {**self.base["config"], "max_clusters": 3}}
        first, second = [r for _, r in reconcile_runs(self, self.pkg, [both, self.later(10, config=both["config"])])]
        self.assertEqual([a.event_type.value for a in first.applications], ["APPLICATION_OBSERVED", "INDEPENDENT_CHANGE_OBSERVED"])
        self.assertTrue(second.active.is_previous)
        self.assertEqual([(a.event_type.value, a.payload["step"]) for a in second.applications], [("INDEPENDENT_CHANGE_OBSERVED", 2)])

    def test_reuse_rule_applies_only_while_the_active_episode_is_the_previous_one(self):
        # A plan that repeats a step number (a second eligible step 1 on the warehouse type) and an observation older
        # than the recorded application: no drift and no reuse, so the size step opens a new episode, and the repeated
        # step 1 that follows is recorded, because the active episode is no longer the previous application.
        package = copy.deepcopy(self.pkg)
        plan = next(s for s in package["scenarios"] if s["scenario_id"] == package["recommended_plan"]["scenario_id"])["execution_plan"]
        plan.insert(1, {"step": 1, "step_type": "CANDIDATE", "knob": "warehouse_type", "current_value": "PRO",
                        "recommended_value": "SERVERLESS", "execution_state": "READY_FOR_CANARY", "invalidates": []})
        earlier = {**self.base, "observed_at_utc": "2026-10-05T12:00:00Z", "recorded_at_utc": "2026-10-05T12:00:00Z",
                   "config": {**self.base["config"], "warehouse_type": "CLASSIC"}}
        (a1, first), (a2, second) = reconcile_runs(self, package, [self.base, earlier])
        self.assertIsNotNone(a2.history.previous_application)
        self.assertIsNone(second.drift)
        self.assertEqual([(a.payload["knob"], a.payload["implementation_conformance"]) for a in second.applications],
                         [("warehouse_size", "EXACT"), ("warehouse_type", "UNRELATED")])
        self.assertFalse(second.active.is_previous)

    def test_drift_ends_with_a_regeneration_on_the_previous_episode(self):
        package, observations = self.scenario("GR-024")
        (a1, first), (a2, second) = reconcile_runs(self, package, observations)
        previous = a2.history.previous_application
        self.assertEqual(second.drift["previous_observed_config_hash"], previous.payload["observed_config_hash"])
        self.assertEqual(second.drift["observed_config_hash"], a2.observation.observed_config_hash)
        self.assertEqual((second.drift["state"], second.drift["required_action"]), ("DRIFTED", "REGENERATE"))
        self.assertEqual(second.drift_episode_id, previous.application_episode_id)
        self.assertEqual(second.active, ActiveApplication(previous.application_episode_id, previous.payload, is_previous=True))
        self.assertEqual(second.terminal_regeneration.payload["reasons"], ["CONFIGURATION_DRIFT"])
        self.assertEqual(second.terminal_regeneration.payload["actual_config"], a2.observation.observed_config)
        self.assertEqual(second.terminal_regeneration.application_episode_id, previous.application_episode_id)
        self.assertFalse(second.validation_due)
        self.assertIsNone(second.upstream_regeneration)

    def test_drift_skips_authoritative_steps_but_records_independent_ones(self):
        drifted = self.later(10, config={**self.base["config"], "warehouse_size": "SMALL", "max_clusters": 3})
        _, second = [r for _, r in reconcile_runs(self, self.pkg, [self.base, drifted])]
        self.assertIsNotNone(second.drift)
        self.assertEqual([(a.event_type.value, a.payload["step"]) for a in second.applications], [("INDEPENDENT_CHANGE_OBSERVED", 2)])

    def test_rollback_attaches_to_the_active_or_previous_episode(self):
        package, observations = self.scenario("GR-025")
        _, second = [r for _, r in reconcile_runs(self, package, observations)]
        self.assertEqual(second.rollback_observed, {"observed_config": second.drift["observed_config"], "reason": None})
        self.assertIsNone(second.rollback_requested)
        self.assertEqual(second.rollback_episode_id, second.drift_episode_id)
        # requested on an application this run opens
        (only,) = [r for _, r in reconcile_runs(self, self.pkg, [{**self.base, "rollback_requested": True, "rollback_reason": "slow"}])]
        self.assertEqual(only.rollback_requested, {"reason": "slow"})
        self.assertEqual(only.rollback_episode_id, only.active.application_episode_id)
        # nothing applied, no history: no episode
        (none,) = [r for _, r in reconcile_runs(self, self.pkg, [{**self.base, "config": dict(self.source), "rollback_observed": True}])]
        self.assertIsNone(none.active)
        self.assertIsNone(none.rollback_episode_id)
        self.assertIsNotNone(none.rollback_observed)

    def test_supersession_records_the_replacement(self):
        package, observations = self.scenario("GR-022")
        _, second = [r for _, r in reconcile_runs(self, package, observations)]
        self.assertEqual(second.replacement_package_id, "PKG-AAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertTrue(second.active.is_previous)
        self.assertTrue(second.validation_due)

    def test_contrary_application_ends_with_a_regeneration(self):
        package, observations = self.scenario("GR-006")
        ((_, only),) = reconcile_runs(self, package, observations)
        (app,) = only.applications
        self.assertEqual((app.event_type.value, app.payload["implementation_conformance"]), ("APPLICATION_OBSERVED", "CONTRARY"))
        self.assertEqual(only.terminal_regeneration.payload["reasons"], ["CONTRARY_IMPLEMENTATION"])
        self.assertEqual(only.terminal_regeneration.application_episode_id, app.application_episode_id)
        self.assertFalse(only.validation_due)

    def test_several_authoritative_changes_are_one_scenario_application(self):
        package, observations = self.scenario("GR-019")
        ((_, only),) = reconcile_runs(self, package, observations)
        (app,) = only.applications
        self.assertEqual(app.payload["application_scope"], "SCENARIO")
        self.assertEqual(app.payload["applied_step_refs"], [{"scenario_id": app.payload["scenario_id"], "step": 1, "conformance": "EXACT"},
                                                            {"scenario_id": app.payload["scenario_id"], "step": 2, "conformance": "EXACT"}])
        self.assertEqual(app.payload["invalidates"], ["min_clusters"])
        self.assertEqual(only.active.application_episode_id, app.application_episode_id)
        self.assertTrue(only.validation_due)

    def test_migration_scenario_is_a_scenario_application(self):
        package, observations = self.scenario("GR-021")
        ((_, only),) = reconcile_runs(self, package, observations)
        self.assertTrue(only.match.scenario_scope)
        self.assertEqual(only.match.selected_scenario_id, MIGRATION)
        (app,) = only.applications
        self.assertEqual((app.payload["application_scope"], app.payload["scenario_id"]), ("SCENARIO", MIGRATION))
        self.assertEqual(app.payload["invalidates"], sorted(["warehouse_size", "max_clusters", "min_clusters", "auto_stop_minutes"]))

    def test_modified_application_requests_regeneration_after_measurement(self):
        package, observations = self.scenario("GR-005")
        ((_, only),) = reconcile_runs(self, package, observations)
        (app,) = only.applications
        self.assertEqual((app.event_type.value, app.payload["modification_relation"]), ("MODIFIED_APPLICATION_OBSERVED", "MORE_AGGRESSIVE"))
        self.assertTrue(only.validation_due)
        self.assertEqual(only.upstream_regeneration.get().payload["reasons"], ["UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM"])

    def test_pending_downstream_change_is_independent_and_opens_no_episode(self):
        package, observations = self.scenario("GR-020")
        ((_, only),) = reconcile_runs(self, package, observations)
        (app,) = only.applications
        self.assertEqual((app.event_type.value, app.payload["authority_eligible"], app.payload["step"]), ("INDEPENDENT_CHANGE_OBSERVED", False, 2))
        self.assertIsNone(only.active)
        self.assertIsNone(only.intent_episode_id)
        self.assertFalse(only.validation_due)
        self.assertIsNone(only.terminal_regeneration)
        self.assertIsNone(only.upstream_regeneration)

    def test_not_ready_evidence_stops_before_validation(self):
        ((_, only),) = reconcile_runs(self, self.pkg, [{**self.base, "elapsed_days": 1}])
        self.assertIsNotNone(only.active)
        self.assertFalse(only.validation_due)
        self.assertIsNone(only.upstream_regeneration)

    def test_result_is_frozen(self):
        ((_, only),) = reconcile_runs(self, self.pkg, [self.base])
        self.assertIsInstance(only, ReconciliationResult)
        with self.assertRaises(Exception):
            only.validation_due = False


if __name__ == "__main__":
    unittest.main()
