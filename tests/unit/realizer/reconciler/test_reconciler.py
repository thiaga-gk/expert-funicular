"""Realizer reconciler capability (src/realizer/reconciler/reconciler.py): ``run`` and its ``ReconciliationResult``.

For every run of the 41 scenarios, the lifecycle events ``run`` decides, recorded in slot order with the oracle's
per-append duplicate suppression, are exactly the entries the oracle appended after its evidence file; what follows
them is the regeneration request it decided, the validation episode when validation is due, or nothing; and the
downstream regeneration and every RERUN file match. ``run`` never raises, writes and logs nothing, and keeps a failure
as ``halt`` with the oracle's exception.
"""
import copy
import dataclasses
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome, run_sequence, tree  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.reconciliation import ReconciliationResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402
from tests.unit.realizer.reconciler.test_failed_runs import Unprintable, Untestable  # noqa: E402

VALIDATION_SECTION = {"VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED", "COUNTERFACTUAL_RECORDED",
                      "ECONOMIC_MEASUREMENT_RECORDED", "BILLING_CORRECTION_RECORDED", "REGENERATION_REQUESTED"}


def ledger_lines(out):
    files = sorted((out / "realization" / "ledger").glob("*.jsonl"))
    return [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def lifecycle_entries(context, analysis, result):
    """The lifecycle slots of *result* as ledger entries, in the oracle's order (drift, applications, intent, rollback
    requested, rollback observed, supersession)."""
    package, rpkg, observed = context.request.package, context.realization_package_id, analysis.observation
    effective, recorded, revid = observed.effective_at_utc, observed.recorded_at_utc, observed.evidence_id

    def entry(event_type, payload, refs, episode):
        return engine.make_ledger_entry(package=package, rpkg_id=rpkg, event_type=event_type, effective_at_utc=effective,
                                        recorded_at_utc=recorded, payload=payload, evidence_refs=refs, application_episode_id=episode)

    out = []
    if result.drift is not None:
        out.append(entry(rc.EventType.DRIFT_DETECTED, result.drift, [revid], result.drift_episode_id))
    out += [entry(a.event_type, a.payload, [revid], a.application_episode_id) for a in result.applications]
    if result.intent is not None:
        out.append(entry(rc.EventType.INTENT_RECORDED, result.intent, [], result.intent_episode_id))
    if result.rollback_requested is not None:
        out.append(entry(rc.EventType.ROLLBACK_REQUESTED, result.rollback_requested, [revid], result.rollback_episode_id))
    if result.rollback_observed is not None:
        out.append(entry(rc.EventType.ROLLBACK_OBSERVED, result.rollback_observed, [revid], result.rollback_episode_id))
    if result.replacement_package_id:
        for event_type in (rc.EventType.SOURCE_PACKAGE_SUPERSEDED, rc.EventType.REPLACEMENT_PACKAGE_LINKED):
            out.append(entry(event_type, {"replacement_package_id": result.replacement_package_id}, [], None))
    return out


def regeneration_entry(context, analysis, request):
    observed = analysis.observation
    return engine.make_ledger_entry(package=context.request.package, rpkg_id=context.realization_package_id,
                                    event_type=rc.EventType.REGENERATION_REQUESTED, effective_at_utc=observed.effective_at_utc,
                                    recorded_at_utc=observed.recorded_at_utc, payload=request.payload,
                                    application_episode_id=request.application_episode_id)


def as_line(entry):
    return json.loads(rc.canonical_json(entry.to_dict()))


class EveryScenarioRunTests(unittest.TestCase):
    def test_decided_events_are_the_oracle_ledger_entries_for_every_run_of_every_scenario(self):
        seen = {key: 0 for key in ("runs", "drift", "scenario", "independent", "modified", "contrary", "reuse", "rollback",
                                   "supersession", "intent", "terminal", "validation", "upstream", "suppressed")}
        original = reconciler.run
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            package = scenario["package_doc"]
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                oracle_out, modular_out = Path(a), Path(b)
                for index, step in enumerate(scenario["runs"]):
                    observation = step.get("observation")
                    kwargs = {"replay_through": step.get("replay_through")}
                    before = ledger_lines(oracle_out)
                    oengine.realize(copy.deepcopy(package), copy.deepcopy(observation), oracle_out,
                                    policy=None if step.get("policy") is None else oengine.RealizerPolicy.from_mapping(step["policy"]), **kwargs)
                    appended = ledger_lines(oracle_out)[len(before):]
                    captured = []

                    def capturing(context, analysis, governance, _captured=captured):
                        result = original(context, analysis, governance)
                        _captured.append((context, analysis, result))
                        return result

                    with mock.patch.object(engine.reconciler, "run", capturing):
                        engine.realize(copy.deepcopy(package), copy.deepcopy(observation), modular_out,
                                       policy=None if step.get("policy") is None else engine.RealizerPolicy.from_mapping(step["policy"]), **kwargs)
                    ((context, analysis, result),) = captured
                    if appended and appended[0]["event_type"] == "REALIZATION_CREATED":
                        appended = appended[1:]
                    if not observation:
                        self.assertEqual(result, ReconciliationResult())
                        self.assertEqual(appended, [])
                        continue
                    self.assertIsNone(result.halt)
                    seen["runs"] += 1

                    # 1. the lifecycle slots, each suppressed when its RLE id is already on the ledger
                    existing = {line["ledger_entry_id"] for line in before}
                    expected = []
                    for entry in lifecycle_entries(context, analysis, result):
                        if entry.ledger_entry_id in existing:
                            seen["suppressed"] += 1
                            continue
                        existing.add(entry.ledger_entry_id)
                        expected.append(as_line(entry))
                    self.assertEqual(appended[:len(expected)], expected, f"{name} run {index}: lifecycle slots")
                    rest = appended[len(expected):]

                    # 2. then the end of the run: a regeneration request, the validation episode, or nothing
                    if result.terminal_regeneration is not None:
                        self.assertFalse(result.validation_due)
                        regen = regeneration_entry(context, analysis, result.terminal_regeneration)
                        self.assertEqual(rest, [] if regen.ledger_entry_id in existing else [as_line(regen)])
                        self.assert_rerun_file(oracle_out, result.terminal_regeneration)
                        seen["terminal"] += 1
                    elif result.validation_due:
                        self.assertTrue({line["event_type"] for line in rest} <= VALIDATION_SECTION, rest)
                        # (a repeated observation's validation entries may all be suppressed duplicates)
                        if any(line["event_type"] == "VALIDATION_READY" for line in rest):
                            self.assertEqual(rest[0]["event_type"], "VALIDATION_READY")
                        for line in rest:
                            self.assertEqual(line["application_episode_id"], result.active.application_episode_id)
                        upstream = result.upstream_regeneration.get()
                        regenerations = [line for line in rest if line["event_type"] == "REGENERATION_REQUESTED"]
                        if upstream is None:
                            self.assertEqual(regenerations, [])
                        else:
                            regen = regeneration_entry(context, analysis, upstream)
                            self.assertEqual(regenerations, [] if regen.ledger_entry_id in {line["ledger_entry_id"] for line in before} else [as_line(regen)])
                            if regenerations:
                                self.assertEqual(rest[-1], regenerations[0])
                            self.assert_rerun_file(oracle_out, upstream)
                            seen["upstream"] += 1
                        seen["validation"] += 1
                    else:
                        self.assertEqual(rest, [])
                        self.assertIsNone(result.upstream_regeneration)

                    seen["drift"] += result.drift is not None
                    seen["scenario"] += any(a.payload.get("application_scope") == "SCENARIO" for a in result.applications)
                    seen["independent"] += any(a.event_type == rc.EventType.INDEPENDENT_CHANGE_OBSERVED for a in result.applications)
                    seen["modified"] += any(a.event_type == rc.EventType.MODIFIED_APPLICATION_OBSERVED for a in result.applications)
                    seen["contrary"] += bool(result.terminal_regeneration and result.terminal_regeneration.payload["reasons"] == ["CONTRARY_IMPLEMENTATION"])
                    seen["reuse"] += bool(result.active and result.active.is_previous and result.drift is None)
                    seen["rollback"] += result.rollback_observed is not None or result.rollback_requested is not None
                    seen["supersession"] += bool(result.replacement_package_id)
                    seen["intent"] += result.intent is not None
                self.assertEqual(tree(modular_out), tree(oracle_out))
        self.assertGreater(seen["runs"], 40)
        self.assertTrue(all(seen.values()), seen)

    def assert_rerun_file(self, out, request):
        path = out / "realization" / "regeneration" / f"{request.regeneration_request_id}.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(json.dumps(request.payload)))


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def reconcile(self, observation, out, package=None, policy=None):
        context = engine.build_context(RealizationRequest(package or self.pkg, observation, out, policy=policy))
        analysis = engine.analyzer.run(context)
        return reconciler.run(context, analysis, engine.governor.run(context, analysis))

    def test_lineage_only_run_reconciles_nothing(self):
        for observation in (None, {}):
            with self.subTest(observation=observation):
                self.assertEqual(self.reconcile(observation, Path("/nonexistent-out")), ReconciliationResult())

    def test_run_is_pure_it_writes_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            engine.realize(self.pkg, self.base, out)
            before = tree(out)
            later = {**self.base, "observed_at_utc": "2026-10-10T12:00:00Z", "recorded_at_utc": "2026-10-10T12:00:00Z",
                     "config": {**self.base["config"], "warehouse_size": "SMALL"}}
            with self.assertNoLogs("realizer", logging.DEBUG):
                result = self.reconcile(later, out)
            self.assertIsNotNone(result.drift)
            self.assertIsNotNone(result.terminal_regeneration)
            self.assertEqual(tree(out), before)

    def test_result_and_its_parts_are_frozen(self):
        result = self.reconcile(self.base, Path("/nonexistent-out"))
        for value, field in ((result, "halt"), (result.match, "primary"), (result.active, "payload"), (result.applications[0], "payload")):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, field, None)

    def test_run_never_raises_and_halts_with_the_oracle_exception(self):
        policy_nan = engine.RealizerPolicy.from_mapping({"realized_ratio_min": "NaN"})
        bad_plan = copy.deepcopy(self.pkg)
        bad_plan["scenarios"][0]["execution_plan"][0]["step"] = "abc"
        cases = {
            "matching": ({}, bad_plan, None),
            "readiness": ({"elapsed_days": "abc"}, None, None),
            "rollback flag": ({"rollback_requested": Untestable()}, None, None),
            "intent text": ({"intent_state": Unprintable()}, None, None),
            "contrary regeneration": ({"config": {**self.base["config"], "warehouse_size": "X_LARGE"}}, None, policy_nan),
        }
        for label, (patch, package, policy) in cases.items():
            observation = {**self.base, **patch}
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                result = self.reconcile(observation, Path(tmp), package=package, policy=policy)
                self.assertIsNotNone(result.halt)
                self.assertIsInstance(result.halt, Exception)
                mapping = None if policy is None else {"realized_ratio_min": "NaN"}
                (old,), _ = run_sequence(oengine, package or self.pkg, [observation], mapping)
                self.assertEqual((type(result.halt).__name__, str(result.halt)), old)
                self.assertFalse(result.validation_due)
                self.assertIsNone(result.terminal_regeneration)
                self.assertIsNone(result.upstream_regeneration)

    def test_halt_keeps_only_the_events_decided_before_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = self.reconcile({**self.base, "rollback_observed": True, "replacement_package_id": Untestable()}, out)
            self.assertIsInstance(result.halt, ValueError)
            self.assertEqual(len(result.applications), 1)
            self.assertIsNotNone(result.intent)
            self.assertIsNotNone(result.rollback_observed)
            self.assertIsNone(result.replacement_package_id)
            # matching failed: nothing at all was decided
            bad_plan = copy.deepcopy(self.pkg)
            bad_plan["scenarios"][0]["execution_plan"].insert(0, 5)
            result = self.reconcile(self.base, out, package=bad_plan)
            self.assertIsInstance(result.halt, AttributeError)
            self.assertEqual(dataclasses.replace(result, halt=None), ReconciliationResult())

    def test_readiness_is_read_only_when_an_active_application_may_be_validated(self):
        unchanged = {**self.base, "config": oengine.package_config(self.pkg), "elapsed_days": "abc"}
        self.assertIsNone(self.reconcile(unchanged, Path("/nonexistent-out")).halt)
        contrary = {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}, "elapsed_days": "abc"}
        self.assertIsNone(self.reconcile(contrary, Path("/nonexistent-out")).halt)
        self.assertIsInstance(self.reconcile({**self.base, "elapsed_days": "abc"}, Path("/nonexistent-out")).halt, ValueError)

    def test_engine_raises_the_halt_after_recording_the_decided_events(self):
        observation = {**self.base, "rollback_requested": True, "rollback_observed": Untestable()}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = outcome(engine.realize, self.pkg, observation, out)
            self.assertEqual(result, ("ValueError", "observation flag has no truth value"))
            self.assertEqual([line["event_type"] for line in ledger_lines(out)],
                             ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED", "ROLLBACK_REQUESTED"])


if __name__ == "__main__":
    unittest.main()
