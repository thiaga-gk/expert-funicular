"""Realizer reconciler capability (src/realizer/reconciler/reconciler.py): ``run`` and its ``ReconciliationResult``.

For every run of the 41 scenarios, the lifecycle events ``run`` decides, recorded in slot order with the per-append
duplicate suppression, are exactly the entries the engine appended after its evidence file (which the stored baseline
ledger holds, see ``helpers.staged_scenario_runs``); what follows them is the regeneration request it decided, the
validation episode when validation is due, or nothing; and the downstream regeneration and every RERUN file match.
``run`` never raises, writes and logs nothing, and keeps a failure as ``halt`` with the exception stored in
``tests/fixtures/realizer/expectations/``.
"""
import contextlib
import copy
import dataclasses
import json
import logging
import tempfile
import unittest
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import (  # noqa: E402
    stage_policy,    Unprintable, Untestable, as_line, fixture_package, ledger_lines, observation_gate_open, outcome, package_gate_open, staged_scenario_runs, tree,
)
from realizer.analyzer import analyzer  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.reconciliation import ReconciliationResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.packager.ledger import make_ledger_entry  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402

VALIDATION_SECTION = {"VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED", "COUNTERFACTUAL_RECORDED",
                      "ECONOMIC_MEASUREMENT_RECORDED", "BILLING_CORRECTION_RECORDED", "REGENERATION_REQUESTED"}


def lifecycle_entries(context, analysis, result):
    """The lifecycle slots of *result* as ledger entries, in the order the engine records them (drift, applications,
    intent, rollback requested, rollback observed, supersession)."""
    package, rpkg, observed = context.request.package, context.realization_package_id, analysis.observation
    effective, recorded, revid = observed.effective_at_utc, observed.recorded_at_utc, observed.evidence_id

    def entry(event_type, payload, refs, episode):
        return make_ledger_entry(package=package, rpkg_id=rpkg, event_type=event_type, effective_at_utc=effective,
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
    return make_ledger_entry(package=context.request.package, rpkg_id=context.realization_package_id,
                                    event_type=rc.EventType.REGENERATION_REQUESTED, effective_at_utc=observed.effective_at_utc,
                                    recorded_at_utc=observed.recorded_at_utc, payload=request.payload,
                                    application_episode_id=request.application_episode_id)


class EveryScenarioRunTests(unittest.TestCase):
    def test_decided_events_are_the_ledger_entries_for_every_run_of_every_scenario(self):
        seen = {key: 0 for key in ("runs", "drift", "scenario", "independent", "modified", "contrary", "reuse", "rollback",
                                   "supersession", "intent", "terminal", "validation", "upstream", "suppressed")}
        for run in staged_scenario_runs():
            name, index, observation, before, appended = run.scenario, run.index, run.observation, run.before, run.appended
            with self.subTest(scenario=name, run=index):
                ((args, result),) = run.calls["reconciler"]
                context, analysis, _ = args
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
                    self.assert_rerun_file(run.tree, result.terminal_regeneration)
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
                        self.assert_rerun_file(run.tree, upstream)
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
        self.assertGreater(seen["runs"], 40)
        self.assertTrue(all(seen.values()), seen)

    def assert_rerun_file(self, run_tree, request):
        text = run_tree[f"realization/regeneration/{request.regeneration_request_id}.json"]
        self.assertEqual(json.loads(text), json.loads(json.dumps(request.payload)))


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def reconcile(self, observation, out, package=None, policy=None, gate=True):
        # a package passed in is a deliberately malformed one, which the loader's gate would refuse before any stage ran;
        # so would the observation gate for ``gate=False`` (an observation the Reconciler must survive, see tests/unit/realizer/config)
        with package_gate_open() if package is not None else contextlib.nullcontext(), contextlib.nullcontext() if gate else observation_gate_open():
            context = build_context(RealizationRequest(package or self.pkg, observation, out, policy=policy))
        analysis = analyzer.run(context)
        return reconciler.run(context, analysis, governor.run(context, analysis))

    def test_lineage_only_run_reconciles_nothing(self):
        for observation in (None, {}):
            with self.subTest(observation=observation):
                self.assertEqual(self.reconcile(observation, Path("/nonexistent-out")), ReconciliationResult())

    def test_run_is_pure_it_writes_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(self.pkg, self.base, out)
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

    def test_run_never_raises_and_halts_with_the_stored_exception(self):
        policy_nan = stage_policy({"realized_ratio_min": "NaN"})
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
                result = self.reconcile(observation, Path(tmp), package=package, policy=policy, gate=False)
                self.assertIsNotNone(result.halt)
                self.assertIsInstance(result.halt, Exception)
                expect.check(self, label, (type(result.halt).__name__, str(result.halt)))
                self.assertFalse(result.validation_due)
                self.assertIsNone(result.terminal_regeneration)
                self.assertIsNone(result.upstream_regeneration)

    def test_halt_keeps_only_the_events_decided_before_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = self.reconcile({**self.base, "rollback_observed": True, "replacement_package_id": Untestable()}, out, gate=False)
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
        unchanged = {**self.base, "config": package_config(self.pkg), "elapsed_days": "abc"}
        self.assertIsNone(self.reconcile(unchanged, Path("/nonexistent-out"), gate=False).halt)
        contrary = {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}, "elapsed_days": "abc"}
        self.assertIsNone(self.reconcile(contrary, Path("/nonexistent-out"), gate=False).halt)
        self.assertIsInstance(self.reconcile({**self.base, "elapsed_days": "abc"}, Path("/nonexistent-out"), gate=False).halt, ValueError)

    def test_engine_raises_the_halt_after_recording_the_decided_events(self):
        observation = {**self.base, "rollback_requested": True, "rollback_observed": Untestable()}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with observation_gate_open():
                result = outcome(realize, self.pkg, observation, out)
            self.assertEqual(result, ("ValueError", "observation flag has no truth value"))
            self.assertEqual([line["event_type"] for line in ledger_lines(out)],
                             ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED", "ROLLBACK_REQUESTED"])


if __name__ == "__main__":
    unittest.main()
