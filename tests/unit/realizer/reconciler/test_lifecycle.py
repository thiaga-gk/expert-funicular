"""Realizer reconciler lifecycle (src/realizer/reconciler/lifecycle.py).

``regeneration_request`` is the RERUN id and payload part of the single-file release's ``request_regeneration`` and
``upstream_regeneration_request`` its downstream-invalidation rule; both return the values stored in
``tests/fixtures/realizer/expectations/`` on valid and malformed input. The engine's ``request_regeneration`` keeps its
signature and writes what the single-file release wrote. The lifecycle scenarios (drift, reuse of the active episode,
rollback, supersession, contrary, scenario scope, migration, modified, pending downstream, not ready) check the
decided events directly and the whole output tree against the stored expectation.
"""
import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import stage_policy, canonical, compact_tree, fixture_package, ledger_lines, migration_scenario_id, outcome, tree  # noqa: E402
from realizer.analyzer.analysis import candidate_steps, package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.reconciliation import ActiveApplication, PlannedApplication, ReconciliationResult, RegenerationRequest  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.packager.packager import request_regeneration  # noqa: E402
from realizer.realizer import realize  # noqa: E402
from realizer.reconciler import lifecycle, reconciler  # noqa: E402

MIGRATION = migration_scenario_id(fixture_package())
REASONS = ("CONFIGURATION_DRIFT", "CONTRARY_IMPLEMENTATION", "UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM")
RECORDED = "2026-10-09T12:00:00Z"
APP = "APP-" + "0123456789AB" * 2   # a well-formed application episode id (the ledger entry validates it)


def rerun_summary(fn, *args, **kwargs):
    """``outcome`` of *fn* with a request reduced to ``(id, application episode, payload as JSON)``."""
    result = outcome(fn, *args, **kwargs)
    if result[0] != "ok" or result[1] is None:
        return result
    request = result[1]
    return "ok", (request.regeneration_request_id, request.application_episode_id, json.loads(json.dumps(request.payload)))


def stored_application_payloads():
    """Every application payload the stored baseline recorded, with its scenario's package and observations."""
    found = []
    for name in canonical.scenario_names():
        scenario = canonical.load_scenario(name)
        for ledger in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
            for line in ledger.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["event_type"] in {"APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED", "INDEPENDENT_CHANGE_OBSERVED"}:
                    observation = next(r["observation"] for r in scenario["runs"] if r.get("observation"))
                    found.append((name, scenario["package_doc"], entry["payload"], observation))
    return found


def reconcile_runs(test, package, observations, policy=None):
    """Run *observations* through the engine into one directory, capturing each run's ``ReconciliationResult`` (and the
    analysis it reconciled), and check the outcomes and the output tree (compact) against the stored expectation."""
    captured = []
    original = reconciler.run

    def capturing(context, analysis, governance):
        result = original(context, analysis, governance)
        captured.append((analysis, result))
        return result

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        outcomes = []
        with mock.patch.object(reconciler, "run", capturing):
            for observation in observations:
                kwargs = {} if policy is None else {"policy": stage_policy(policy)}
                result = outcome(realize, copy.deepcopy(package), copy.deepcopy(observation), out, **kwargs)
                outcomes.append(("ok", result[1].snapshot_id) if result[0] == "ok" else result)
        run_tree = tree(out)
    test.reconcile_checks = getattr(test, "reconcile_checks", 0) + 1
    expect.check(test, f"run{test.reconcile_checks}", (outcomes, compact_tree(run_tree)))
    return captured


class RegenerationRequestTests(unittest.TestCase):
    def test_every_stored_application_payload_and_reason(self):
        payloads = stored_application_payloads()
        self.assertGreater(len(payloads), 20)
        policy = RealizerPolicy()
        results = []
        for name, package, app_payload, observation in payloads:
            for reason in REASONS:
                results.append([name, reason, rerun_summary(lifecycle.regeneration_request, package, app_payload, observation, policy, reason=reason)])
        self.assertTrue(all(r[2][0] == "ok" for r in results))
        expect.check(self, "requests", results, spot={"count": len(results), "scenarios": sorted({r[0] for r in results}),
                                                       "ids": sorted({r[2][1][0] for r in results})[:3]})

    def test_fallback_to_the_observed_configuration_and_other_policies(self):
        package = fixture_package()
        observation = demo_observation(package)
        variants = [{}, {"observed_config": None, "invalidates": None}, {"observed_config": {}, "scenario_id": "S", "step": 2},
                    {"observed_config": {"warehouse_size": "small"}, "invalidates": ["a", "b"], "application_episode_id": APP}]
        results = []
        for app_payload, mapping in itertools.product(variants, (None, {"minimum_days": 1}, {"realized_ratio_min": "0.9"})):
            results.append([app_payload, mapping, rerun_summary(lifecycle.regeneration_request, package, app_payload, observation,
                                                                stage_policy(mapping), reason="R")])
        self.assertEqual(len(results), 12)
        expect.check(self, "requests", results, spot={"count": len(results), "ids": sorted({r[2][1][0] for r in results})})

    def test_malformed_input_raises_and_returns_nothing(self):
        package = fixture_package()
        observation = demo_observation(package)
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
                new = rerun_summary(lifecycle.regeneration_request, pkg, app_payload, obs, stage_policy(mapping), reason="R")
                self.assertNotEqual(new[0], "ok")
                expect.check(self, label, new)

    def test_engine_request_regeneration_writes_the_stored_files(self):
        package = fixture_package()
        observation = demo_observation(package)
        for index, (app_payload, mapping) in enumerate((({}, None), ({"observed_config": {"warehouse_size": "SMALL"}, "application_episode_id": APP}, None),
                                                        ({}, {"realized_ratio_min": "NaN"}), ({"observed_config": 5}, None))):
            with self.subTest(app_payload=app_payload, policy=mapping), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                result = outcome(request_regeneration, copy.deepcopy(package), dict(app_payload), observation, out,
                                 stage_policy(mapping), RECORDED, RECORDED, reason="R")
                expect.check(self, f"case{index}", (result, compact_tree(tree(out))))


class UpstreamRegenerationTests(unittest.TestCase):
    def test_rule_over_its_inputs(self):
        package = fixture_package()
        observation = demo_observation(package)
        results = []
        for invalidates, conformance, elapsed, remeasure, mapping in itertools.product(
            ([], ["min_clusters"], None), ("EXACT", "MODIFIED", "CONTRARY", None), (None, 0, 6, 7, 8, "8", "abc", 7.9),
            (None, 3, 10, "abc", 0), (None, {"remeasure_days_after_invalidating_change": 3}, {"remeasure_days_after_invalidating_change": "x"}),
        ):
            app_payload = {"invalidates": invalidates, "implementation_conformance": conformance, "remeasure_days": remeasure,
                           "observed_config": {"warehouse_size": "MEDIUM"}, "application_episode_id": APP, "scenario_id": "S", "step": 1}
            obs = {**observation, "elapsed_days": elapsed}
            results.append(rerun_summary(lifecycle.upstream_regeneration_request, package, app_payload, obs, stage_policy(mapping)))
        self.assertGreater(len(results), 1000)
        kinds = sorted({r[0] for r in results})
        self.assertGreater(len(kinds), 1)
        expect.check(self, "results", results, spot={"count": len(results), "kinds": kinds, "requested": sum(1 for r in results if r[0] == "ok" and r[1] is not None),
                                                      "ids": len({r[1][0] for r in results if r[0] == "ok" and r[1] is not None})})


class RegenerationEvidenceTests(unittest.TestCase):
    """A regeneration request names the observed-configuration evidence it was made from, the observation window and the
    knobs whose actual value differs from the package's current configuration."""

    def test_the_request_states_its_evidence_window_and_changed_knobs(self):
        package = fixture_package()
        observation = {**demo_observation(package), "window_start_utc": "2026-10-02T12:00:00Z", "window_end_utc": "2026-10-09T12:00:00Z"}
        actual = {**package_config(package), "warehouse_size": "MEDIUM", "auto_stop_minutes": 5, "enable_photon": False}
        app_payload = {"observed_config": actual, "invalidates": ["max_clusters"], "application_episode_id": APP}
        payload = lifecycle.regeneration_request(package, app_payload, observation, RealizerPolicy(), reason="R", evidence_id="REVID-" + "AB" * 12).payload
        self.assertEqual(payload["evidence_id"], "REVID-" + "AB" * 12)
        self.assertEqual(payload["observation_window"], {"start_utc": "2026-10-02T12:00:00Z", "end_utc": "2026-10-09T12:00:00Z"})
        self.assertEqual(payload["changed_knobs"], ["warehouse_size", "auto_stop_minutes"])   # decision knobs only, never Photon
        self.assertEqual(payload["impacted_knobs"], ["max_clusters"])                          # what the change invalidates

    def test_the_window_falls_back_to_the_observed_time_and_a_missing_reference_is_null(self):
        package = fixture_package()
        observation = {k: v for k, v in demo_observation(package).items() if k not in {"window_start_utc", "window_end_utc"}}
        request = lifecycle.regeneration_request(package, {"observed_config": package_config(package)}, observation, RealizerPolicy(), reason="R")
        self.assertIsNone(request.payload["evidence_id"])
        self.assertEqual(request.payload["observation_window"], {"start_utc": observation["observed_at_utc"], "end_utc": observation["observed_at_utc"]})
        self.assertEqual(request.payload["changed_knobs"], [])

    def test_the_evidence_does_not_change_the_request_id(self):
        package = fixture_package()
        observation = demo_observation(package)
        app_payload = {"observed_config": {**package_config(package), "warehouse_size": "MEDIUM"}}
        ids = {lifecycle.regeneration_request(package, app_payload, observation, RealizerPolicy(), reason="R", **kwargs).regeneration_request_id
               for kwargs in ({}, {"evidence_id": "REVID-" + "01" * 12})}
        self.assertEqual(len(ids), 1)

    def test_asking_again_on_a_later_run_is_the_same_request(self):
        package = fixture_package()
        base = demo_observation(package)
        later = {**copy.deepcopy(base), "observed_at_utc": "2026-10-12T12:00:00Z", "recorded_at_utc": "2026-10-12T12:00:00Z",
                 "window_start_utc": "2026-10-05T12:00:00Z", "window_end_utc": "2026-10-12T12:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            first_evidence = None
            for observation in (base, later):
                realize(copy.deepcopy(package), copy.deepcopy(observation), out)
                files_ = sorted((out / "realization" / "regeneration").glob("RERUN-*.json"))
                self.assertEqual(len(files_), 1)
                evidence = json.loads(files_[0].read_text(encoding="utf-8"))["evidence_id"]
                first_evidence = first_evidence or evidence
                self.assertEqual(evidence, first_evidence)   # the first request's file stays
            lines = [json.loads(line) for p in (out / "realization" / "ledger").glob("*.jsonl") for line in p.read_text(encoding="utf-8").splitlines()]
        (entry,) = [e for e in lines if e["event_type"] == "REGENERATION_REQUESTED"]
        self.assertEqual(entry["evidence_refs"], [first_evidence])
        self.assertEqual(entry["payload"]["evidence_id"], first_evidence)

    def realize_all(self, package, observations):
        """Run *observations*; returns each run's reconciliation, the RERUN payloads written and the evidence ids written."""
        captured = []
        original = reconciler.run

        def capturing(context, analysis, governance):
            result = original(context, analysis, governance)
            captured.append((analysis, result))
            return result

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(reconciler, "run", capturing):
            out = Path(tmp)
            for observation in observations:
                realize(copy.deepcopy(package), copy.deepcopy(observation), out)
            reruns = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "realization" / "regeneration").glob("RERUN-*.json"))]
            evidence = sorted(p.stem for p in (out / "realization" / "evidence").glob("REVID-*.json"))
        return captured, reruns, evidence

    def test_every_regeneration_reason_names_the_runs_evidence(self):
        base_package = fixture_package()
        base = demo_observation(base_package)
        drift_package, drift_runs = (lambda sc: (sc["package_doc"], [r["observation"] for r in sc["runs"]]))(canonical.load_scenario("GR-024"))
        contrary_package, contrary_runs = (lambda sc: (sc["package_doc"], [r["observation"] for r in sc["runs"]]))(canonical.load_scenario("GR-006"))
        cases = {"UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM": (base_package, [base]), "CONFIGURATION_DRIFT": (drift_package, drift_runs),
                 "CONTRARY_IMPLEMENTATION": (contrary_package, contrary_runs)}
        for reason, (package, observations) in cases.items():
            with self.subTest(reason):
                captured, reruns, evidence = self.realize_all(package, observations)
                (rerun,) = [r for r in reruns if r["reasons"] == [reason]]
                analysis = captured[-1][0].observation
                self.assertEqual(rerun["evidence_id"], analysis.evidence_id)
                self.assertIn(rerun["evidence_id"], evidence)
                raw = analysis.raw
                self.assertEqual(rerun["observation_window"], {"start_utc": raw.get("window_start_utc") or analysis.observed_at_utc,
                                                               "end_utc": raw.get("window_end_utc") or analysis.observed_at_utc})
                source = package_config(package)
                self.assertEqual(rerun["changed_knobs"], [k for k in lifecycle.DECISION_KNOBS if rerun["actual_config"][k] != source[k]])
                # the drift scenario reverts to the package's own configuration: nothing differs from it
                self.assertEqual(bool(rerun["changed_knobs"]), reason != "CONFIGURATION_DRIFT")


class LifecycleScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)
        cls.source = package_config(cls.pkg)
        # the plan's pending downstream step (max_clusters): applying it opens no episode of its own
        cls.pending = next(s for s in candidate_steps(cls.pkg) if s["knob"] == "max_clusters")

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
        both = {**self.base, "config": {**self.base["config"], "max_clusters": self.pending["recommended_value"]}}
        first, second = [r for _, r in reconcile_runs(self, self.pkg, [both, self.later(10, config=both["config"])])]
        self.assertEqual([a.event_type.value for a in first.applications], ["APPLICATION_OBSERVED", "INDEPENDENT_CHANGE_OBSERVED"])
        self.assertTrue(second.active.is_previous)
        self.assertEqual([(a.event_type.value, a.payload["step"]) for a in second.applications], [("INDEPENDENT_CHANGE_OBSERVED", self.pending["step"])])

    def test_reuse_rule_applies_only_while_the_active_episode_is_the_previous_one(self):
        # A plan that repeats a step number (a second eligible step 1 on the warehouse type, copied from the migration
        # alternative's step so that the package stays valid) and an observation older than the recorded application: no
        # drift and no reuse, so the size step opens a new episode, and the repeated step 1 that follows is recorded,
        # because the active episode is no longer the previous application.
        package = copy.deepcopy(self.pkg)
        plan = next(s for s in package["scenarios"] if s["scenario_id"] == package["recommended_plan"]["scenario_id"])["execution_plan"]
        migration_step = next(s for s in next(s for s in package["scenarios"] if s["scenario_id"] == MIGRATION)["execution_plan"] if s.get("knob") == "warehouse_type")
        plan.insert(1, {**copy.deepcopy(migration_step), "step": 1, "invalidates": []})
        earlier = {**self.base, "observed_at_utc": "2026-10-05T12:00:00Z", "recorded_at_utc": "2026-10-05T12:00:00Z",
                   "window_start_utc": "2026-09-28T12:00:00Z", "window_end_utc": "2026-10-05T12:00:00Z",
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
        drifted = self.later(10, config={**self.base["config"], "warehouse_size": "SMALL", "max_clusters": self.pending["recommended_value"]})
        _, second = [r for _, r in reconcile_runs(self, self.pkg, [self.base, drifted])]
        self.assertIsNotNone(second.drift)
        self.assertEqual([(a.event_type.value, a.payload["step"]) for a in second.applications], [("INDEPENDENT_CHANGE_OBSERVED", self.pending["step"])])

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
        steps = [s for s in candidate_steps(package) if s["step"] in (1, 2)]
        self.assertEqual(app.payload["invalidates"], sorted({knob for s in steps for knob in s["invalidates"]}))
        self.assertEqual(app.payload["invalidates"], ["max_clusters", "min_clusters"])   # the size step's; the auto-stop step invalidates nothing
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
        pending = next(s for s in candidate_steps(package) if s["knob"] == "max_clusters")
        self.assertEqual((app.event_type.value, app.payload["authority_eligible"], app.payload["step"]), ("INDEPENDENT_CHANGE_OBSERVED", False, pending["step"]))
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


class IndependentChangeTests(unittest.TestCase):
    """A step is an application only when the observed value relates to it and was applied after the package existed:
    otherwise it is an independent change (R03, R08), and a contrary application invalidates the plan."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def run_once(self, observation):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            snapshot = realize(copy.deepcopy(self.pkg), copy.deepcopy(observation), out)
            return snapshot, ledger_lines(out)

    @staticmethod
    def kinds(entries):
        return [entry["event_type"] for entry in entries]

    def test_a_change_applied_two_days_before_the_package_is_never_an_application(self):
        observation = {**self.base, "applied_at_utc": "2026-09-29T12:00:00Z"}   # generated_at is 2026-10-01T12:00:00Z
        snapshot, entries = self.run_once(observation)
        changes = [e for e in entries if e["event_type"] == "INDEPENDENT_CHANGE_OBSERVED"]
        self.assertTrue(changes)
        self.assertEqual({e["payload"]["independent_reason"] for e in changes}, {"APPLIED_BEFORE_PACKAGE"})
        self.assertFalse(any(e["event_type"] in {"APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED"} for e in entries))
        self.assertEqual(snapshot.current_state["realization_state"], "NOT_APPLIED")
        self.assertEqual(snapshot.application["state"], "NOT_APPLIED")
        # nothing is validated or measured, so nothing can be claimed
        self.assertFalse(any(e["event_type"] in {"VALIDATION_READY", "ECONOMIC_MEASUREMENT_RECORDED", "COUNTERFACTUAL_RECORDED"} for e in entries))
        self.assertFalse(snapshot.economics.get("available", False))
        self.assertNotEqual(snapshot.attribution.get("attribution_claim_status"), "CLAIMABLE")
        self.assertIn(snapshot.attribution.get("recommendation_attributed_realized_savings", "0.00"), ("0.00", None))

    def test_the_same_change_applied_at_generation_is_an_application(self):
        snapshot, entries = self.run_once(self.base)
        self.assertIn("APPLICATION_OBSERVED", self.kinds(entries))
        self.assertEqual(snapshot.application["state"], "APPLIED")

    def test_an_unrelated_value_is_an_independent_change_not_an_application(self):
        observation = {**self.base, "config": {**self.base["config"], "warehouse_size": "GIGANTIC"}}   # a size the registry does not know
        snapshot, entries = self.run_once(observation)
        change = next(e for e in entries if e["event_type"] == "INDEPENDENT_CHANGE_OBSERVED")
        self.assertEqual(change["payload"]["independent_reason"], "UNRELATED_VALUE")
        self.assertEqual(change["payload"]["implementation_conformance"], "UNRELATED")
        self.assertEqual(change["payload"]["knob"], "warehouse_size")
        self.assertFalse(any(e["event_type"] in {"APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED"} for e in entries))
        self.assertEqual(snapshot.current_state["realization_state"], "NOT_APPLIED")
        self.assertEqual(snapshot.application["state"], "NOT_APPLIED")
        self.assertNotIn("VALIDATION_READY", self.kinds(entries))

    def test_an_independent_reason_is_recorded_only_for_independent_changes(self):
        _, entries = self.run_once(self.base)
        for entry in entries:
            if entry["event_type"] != "INDEPENDENT_CHANGE_OBSERVED":
                self.assertNotIn("independent_reason", entry["payload"], entry["event_type"])

    def test_a_contrary_application_invalidates_the_realization(self):
        observation = {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}}
        snapshot, entries = self.run_once(observation)
        self.assertEqual(snapshot.application["implementation_conformance"], "CONTRARY")
        self.assertEqual(snapshot.application["state"], "APPLIED")
        self.assertEqual(snapshot.current_state["realization_state"], "INVALIDATED")
        self.assertIn("REGENERATION_REQUESTED", self.kinds(entries))
        self.assertFalse(snapshot.economics.get("available", False))


if __name__ == "__main__":
    unittest.main()
