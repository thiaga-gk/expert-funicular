"""Realizer reporter capability (src/realizer/reporter/): ``run`` and ``legacy_compatibility``.

``run`` writes the realization package and then the snapshot, the same canonical JSON in both, and nothing else: for every
run of the 41 scenarios the files, their bytes and the report it returns are the oracle's, and a write that fails leaves
the output tree the oracle left. ``legacy_compatibility`` is moved verbatim (source text and signature identical to the
oracle's) and gives the oracle's projection for every snapshot of every scenario and for every state the mapping knows
or does not know. The Reporter decides nothing: it logs nothing and changes neither the snapshot nor the ledger.
"""
import contextlib
import copy
import dataclasses
import inspect
import io
import json
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import canonical, engine, events, files, fixture_package, oengine, outcome, tree  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.report import RealizationReport  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.reporter import reporter  # noqa: E402


def packaged_run(package, observation, out, replay_through=None):
    """The modular stages up to the Packager, the way the facade runs them: ``(context, packaged)``."""
    context = engine.build_context(RealizationRequest(package, observation, out, replay_through=replay_through))
    engine.packager.begin(context)
    analysis = engine.analyzer.run(context)
    governance = engine.governor.run(context, analysis)
    reconciliation = engine.reconciler.run(context, analysis, governance)
    estimate = engine.estimator.run(context, analysis, governance, reconciliation)
    reviewed = engine.reviewer.run(context, analysis, governance, reconciliation, estimate)
    return context, engine.packager.run(context, reviewed)


class EveryScenarioRunTests(unittest.TestCase):
    def test_files_and_report_are_the_oracle_files_for_every_run_of_every_scenario(self):
        runs = 0
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            package = scenario["package_doc"]
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                oracle_out, modular_out = Path(a), Path(b)
                for index, step in enumerate(scenario["runs"]):
                    with self.subTest(scenario=name, run=index):
                        observation, cut, policy = step.get("observation"), step.get("replay_through"), step.get("policy")
                        oengine.realize(copy.deepcopy(package), copy.deepcopy(observation), oracle_out, replay_through=cut,
                                        policy=None if policy is None else oengine.RealizerPolicy.from_mapping(policy))
                        request = RealizationRequest(copy.deepcopy(package), copy.deepcopy(observation), modular_out, replay_through=cut,
                                                     policy=None if policy is None else engine.RealizerPolicy.from_mapping(policy))
                        context = engine.build_context(request)
                        engine.packager.begin(context)
                        analysis = engine.analyzer.run(context)
                        governance = engine.governor.run(context, analysis)
                        reconciliation = engine.reconciler.run(context, analysis, governance)
                        estimate = engine.estimator.run(context, analysis, governance, reconciliation)
                        packaged = engine.packager.run(context, engine.reviewer.run(context, analysis, governance, reconciliation, estimate))
                        report = reporter.run(context, packaged)
                        self.assertEqual(tree(modular_out), tree(oracle_out))
                        rpkg = context.realization_package_id
                        self.assertEqual(report.files, (modular_out / "realization" / "packages" / f"{rpkg}.json",
                                                        modular_out / "realization" / "snapshots" / f"{report.realization.snapshot_id}.json"))
                        for path in report.files:
                            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report.realization.to_dict())
                        self.assertIs(report.realization, packaged.realization)
                        runs += 1
        self.assertGreaterEqual(runs, 53)


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def test_writes_the_package_then_the_snapshot_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            context, packaged = packaged_run(self.pkg, self.base, out)
            before = tree(out)
            order = []
            real = reporter.write_json
            with mock.patch.object(reporter, "write_json", lambda path, value: (order.append((path, value)), real(path, value))[1]):
                report = reporter.run(context, packaged)
            after = tree(out)
            self.assertEqual([p for p, _ in order], list(report.files))
            self.assertEqual([p.parent.name for p, _ in order], ["packages", "snapshots"])
            self.assertEqual(order[0][1], order[1][1])
            written = {k for k in set(after) - set(before) if after[k] is not None}
            self.assertEqual(written, {f"realization/packages/{context.realization_package_id}.json",
                                       f"realization/snapshots/{report.realization.snapshot_id}.json"})
            self.assertEqual({k: v for k, v in after.items() if k in before}, before, "nothing the run wrote before is touched")

    def test_is_idempotent_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, packaged = packaged_run(self.pkg, self.base, Path(tmp))
            reporter.run(context, packaged)
            first = tree(Path(tmp))
            with self.assertNoLogs("realizer", logging.DEBUG):
                report = reporter.run(context, packaged)
            self.assertEqual(tree(Path(tmp)), first)
            self.assertEqual(events(first), events(tree(Path(tmp))))
            self.assertEqual(len(files(first, "packages")), 1)
            self.assertEqual(len(files(first, "snapshots")), 1)
            self.assertEqual(report.realization.to_dict(), packaged.realization.to_dict())

    def test_report_is_a_frozen_internal_hand_off(self):
        report = RealizationReport(realization=None, files=())  # type: ignore[arg-type]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.files = ()  # type: ignore[misc]
        self.assertEqual([f.name for f in dataclasses.fields(RealizationReport)], ["realization", "files"])
        self.assertNotIn("RealizationReport", getattr(rc, "__all__", ()))

    def test_lineage_only_run_is_reported_like_any_other(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            oengine.realize(copy.deepcopy(self.pkg), None, Path(a))
            context, packaged = packaged_run(copy.deepcopy(self.pkg), None, Path(b))
            report = reporter.run(context, packaged)
            self.assertEqual(tree(Path(a)), tree(Path(b)))
            self.assertEqual(len(report.files), 2)


class FailedWriteTests(unittest.TestCase):
    """A write that fails leaves the oracle's tree: the ledger, the evidence and the regeneration files stay, and the
    package file stays when only the snapshot cannot be written."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def failing(self, module, blocked):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            blocker = out / "realization" / blocked
            blocker.parent.mkdir(parents=True)
            blocker.write_text("in the way\n", encoding="utf-8")
            result = outcome(module.realize, copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
            kind, message = result
            return (kind, str(message).replace(str(out), "<out>")), tree(out)

    def test_a_package_or_snapshot_that_cannot_be_written_fails_like_the_oracle(self):
        for blocked in ("packages", "snapshots"):
            with self.subTest(blocked=blocked):
                new, old = self.failing(engine, blocked), self.failing(oengine, blocked)
                self.assertEqual(new, old)
                self.assertNotEqual(new[0][0], "ok")
        # the snapshot directory in the way: the package file is already written (package first), the ledger is complete
        _, snapshot_blocked = self.failing(oengine, "snapshots")
        self.assertEqual(len(files(snapshot_blocked, "packages")), 1)
        _, package_blocked = self.failing(oengine, "packages")
        self.assertEqual(files(package_blocked, "packages"), [])
        self.assertIn("VALIDATION_COMPLETED", events(package_blocked))


class LegacyCompatibilityTests(unittest.TestCase):
    def test_moved_verbatim_and_still_reachable_from_the_engine(self):
        self.assertIs(engine.legacy_compatibility, reporter.legacy_compatibility)
        self.assertEqual(inspect.getsource(reporter.legacy_compatibility), inspect.getsource(oengine.legacy_compatibility))
        self.assertEqual(str(inspect.signature(reporter.legacy_compatibility)), str(inspect.signature(oengine.legacy_compatibility)))

    def test_every_snapshot_of_every_scenario_projects_like_the_oracle(self):
        seen = set()
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                for index, step in enumerate(scenario["runs"]):
                    policy = step.get("policy")
                    new = engine.realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(a),
                                         replay_through=step.get("replay_through"), policy=None if policy is None else engine.RealizerPolicy.from_mapping(policy))
                    old = oengine.realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(b),
                                          replay_through=step.get("replay_through"), policy=None if policy is None else oengine.RealizerPolicy.from_mapping(policy))
                    with self.subTest(scenario=name, run=index):
                        projection = reporter.legacy_compatibility(new)
                        self.assertEqual(projection, oengine.legacy_compatibility(old))
                        seen.add((new.current_state["realization_state"], projection["status"], tuple(projection["reasons"])))
        states = {state for state, _, _ in seen}
        for state in ("NOT_APPLIED", "APPLIED", "REALIZED", "UNDER_REALIZED", "VALIDATION_FAILED", "ROLLED_BACK", "MONITORING", "DRIFTED", "BLOCKED"):
            self.assertIn(state, states)
        self.assertGreaterEqual({status for _, status, _ in seen}, {"LOSSLESS", "LOSSY"})

    def snapshot(self, realization_state, **application):
        return types.SimpleNamespace(current_state={"realization_state": realization_state}, application=application)

    def test_every_state_actor_and_conformance(self):
        states = ["NOT_APPLIED", "APPLIED", "VALIDATING", "REALIZED", "UNDER_REALIZED", "VALIDATION_FAILED", "ROLLED_BACK", "MONITORING",
                  "SOMETHING_NEW", "", "applied"]
        applications = [{}, {"state": "APPLIED"}, {"state": "APPLIED", "actor": "ops@example.com"}, {"state": "APPLIED", "actor": ""},
                        {"implementation_conformance": "MODIFIED"}, {"implementation_conformance": "EXACT", "state": "APPLIED", "actor": None},
                        {"state": "APPLIED", "implementation_conformance": "MODIFIED"}]
        for state in states:
            for application in applications:
                with self.subTest(state=state, application=application):
                    self.assertEqual(outcome(reporter.legacy_compatibility, self.snapshot(state, **application)),
                                     outcome(oengine.legacy_compatibility, self.snapshot(state, **application)))

    def test_the_unknown_actor_is_not_fabricated(self):
        result = reporter.legacy_compatibility(self.snapshot("APPLIED", state="APPLIED"))
        self.assertEqual(result, {"status": "LOSSY", "legacy_realization_status": "APPLIED", "reasons": ["LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN"]})
        self.assertEqual(reporter.legacy_compatibility(self.snapshot("MONITORING"))["reasons"], ["MONITORING_COLLAPSES_TO_LEGACY_REALIZED"])
        self.assertEqual(reporter.legacy_compatibility(self.snapshot("OTHER")),
                         {"status": "UNREPRESENTABLE", "legacy_realization_status": None, "reasons": ["REALIZER_STATE_OTHER_HAS_NO_LEGACY_MAPPING"]})

    def test_malformed_snapshots_raise_the_oracle_exception(self):
        for bad in (types.SimpleNamespace(current_state={}, application={}), types.SimpleNamespace(current_state={"realization_state": "APPLIED"}, application=None),
                    types.SimpleNamespace(), None):
            with self.subTest(bad=bad):
                self.assertEqual(outcome(reporter.legacy_compatibility, bad), outcome(oengine.legacy_compatibility, bad))
                self.assertNotEqual(outcome(reporter.legacy_compatibility, bad)[0], "ok")

    def test_is_pure(self):
        snap = self.snapshot("APPLIED", state="APPLIED")
        before = copy.deepcopy(snap)
        printed = io.StringIO()
        with self.assertNoLogs("realizer", logging.DEBUG), contextlib.redirect_stdout(printed):
            reporter.legacy_compatibility(snap)
        self.assertEqual((snap, printed.getvalue()), (before, ""))


class ReporterShapeTests(unittest.TestCase):
    def test_no_workbook_stub(self):
        """The single-file ``.xlsx`` renderer needs ``artifact_tool`` and the retired Publisher's workbook reporter; no
        module is created for it until it has code (design 4.3). The projection it renders is ``contracts/report.py``."""
        folder = Path(reporter.__file__).parent
        self.assertEqual(sorted(p.name for p in folder.glob("*.py")), ["__init__.py", "reporter.py"])

    def test_run_and_legacy_compatibility_are_the_public_names(self):
        self.assertEqual(list(inspect.signature(reporter.run).parameters), ["context", "packaged"])


if __name__ == "__main__":
    unittest.main()
