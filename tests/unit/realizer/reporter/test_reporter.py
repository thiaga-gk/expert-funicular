"""Realizer reporter capability (src/realizer/reporter/): ``run`` and ``legacy_compatibility``.

``run`` writes the realization package and then the snapshot, the same canonical JSON in both, and nothing else: for every
run of the 41 scenarios it writes those two files and returns them in its report (the bytes of every output tree are
checked against the stored baseline in ``tests/regression/realizer``), and a write that fails leaves the output tree
stored in ``tests/fixtures/realizer/expectations/``. ``legacy_compatibility`` gives the stored projection for every
snapshot of every scenario and for every state the mapping knows or does not know. The Reporter decides nothing: it logs
nothing and changes neither the snapshot nor the ledger.
"""
import ast
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

from tests import realizer_expect as expect  # noqa: E402
from tests.unit.realizer.helpers import stage_policy, canonical, compact_tree, events, files, fixture_package, outcome, tree  # noqa: E402
from realizer.analyzer import analyzer  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.report import RealizationReport  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.estimator import estimator  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.packager import packager  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402
from realizer.reporter import reporter  # noqa: E402
from realizer.reviewer import reviewer  # noqa: E402


def packaged_run(package, observation, out, replay_through=None):
    """The modular stages up to the Packager, the way the facade runs them: ``(context, packaged)``."""
    context = build_context(RealizationRequest(package, observation, out, replay_through=replay_through))
    packager.begin(context)
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    reconciliation = reconciler.run(context, analysis, governance)
    estimate = estimator.run(context, analysis, governance, reconciliation)
    reviewed = reviewer.run(context, analysis, governance, reconciliation, estimate)
    return context, packager.run(context, reviewed)


class EveryScenarioRunTests(unittest.TestCase):
    def test_files_and_report_for_every_run_of_every_scenario(self):
        runs = 0
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            package = scenario["package_doc"]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                for index, step in enumerate(scenario["runs"]):
                    with self.subTest(scenario=name, run=index):
                        observation, cut, policy = step.get("observation"), step.get("replay_through"), step.get("policy")
                        request = RealizationRequest(copy.deepcopy(package), copy.deepcopy(observation), out, replay_through=cut,
                                                     policy=None if policy is None else stage_policy(policy))
                        context = build_context(request)
                        packager.begin(context)
                        analysis = analyzer.run(context)
                        governance = governor.run(context, analysis)
                        reconciliation = reconciler.run(context, analysis, governance)
                        estimate = estimator.run(context, analysis, governance, reconciliation)
                        packaged = packager.run(context, reviewer.run(context, analysis, governance, reconciliation, estimate))
                        report = reporter.run(context, packaged)
                        rpkg = context.realization_package_id
                        # one lineage per scenario: its package and snapshot, then the warehouse's state index
                        state = out / "realization" / "state" / f"{package['warehouse']['workspace_id']}__{package['warehouse']['warehouse_id']}.json"
                        self.assertEqual(report.files, (out / "realization" / "packages" / f"{rpkg}.json",
                                                        out / "realization" / "snapshots" / f"{report.realization.snapshot_id}.json", state))
                        for path in report.files[:2]:
                            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report.realization.to_dict())
                        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), packaged.state_index)
                        self.assertEqual([r["snapshot_id"] for r in packaged.state_index["lineages"]], [report.realization.snapshot_id])
                        self.assertIs(report.realization, packaged.realization)
                        runs += 1
        self.assertGreaterEqual(runs, 53)


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

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
            self.assertEqual([p.parent.name for p, _ in order], ["packages", "snapshots", "state"])
            self.assertEqual(order[0][1], order[1][1])
            self.assertEqual(order[2][1], packaged.state_index)
            written = {k for k in set(after) - set(before) if after[k] is not None}
            self.assertEqual(written, {f"realization/packages/{context.realization_package_id}.json",
                                       f"realization/snapshots/{report.realization.snapshot_id}.json",
                                       f"realization/state/{self.pkg['warehouse']['workspace_id']}__{self.pkg['warehouse']['warehouse_id']}.json"})
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
        with tempfile.TemporaryDirectory() as tmp:
            context, packaged = packaged_run(copy.deepcopy(self.pkg), None, Path(tmp))
            report = reporter.run(context, packaged)
            self.assertEqual([p.parent.name for p in report.files], ["packages", "snapshots", "state"])
            expect.check(self, "tree", compact_tree(tree(Path(tmp))))


class FailedWriteTests(unittest.TestCase):
    """A write that fails leaves the stored tree: the ledger, the evidence and the regeneration files stay, and the
    package file stays when only the snapshot cannot be written."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def failing(self, blocked):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            blocker = out / "realization" / blocked
            blocker.parent.mkdir(parents=True)
            blocker.write_text("in the way\n", encoding="utf-8")
            result = outcome(realize, copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
            kind, message = result
            return (kind, str(message).replace(str(out), "<out>")), tree(out)

    def test_a_package_or_snapshot_that_cannot_be_written_fails_like_the_single_file_release(self):
        for blocked in ("packages", "snapshots"):
            with self.subTest(blocked=blocked):
                failure, left = self.failing(blocked)
                self.assertNotEqual(failure[0], "ok")
                expect.check(self, blocked, (failure, compact_tree(left)))
        # the snapshot directory in the way: the package file is already written (package first), the ledger is complete
        _, snapshot_blocked = self.failing("snapshots")
        self.assertEqual(len(files(snapshot_blocked, "packages")), 1)
        _, package_blocked = self.failing("packages")
        self.assertEqual(files(package_blocked, "packages"), [])
        self.assertIn("VALIDATION_COMPLETED", events(package_blocked))


class SchemaGateTests(unittest.TestCase):
    """Every realization package and snapshot is checked against the committed schema before the Reporter writes it; an
    invalid document is a defect (``jsonschema.ValidationError``) and no file of it is written."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    INVALID_STATES = {
        "unknown realization state": {"realization_state": "SOMETHING_NEW"},
        "unknown validation state": {"validation_state": "MAYBE"},
        "unknown maturity state": {"maturity_state": 7},
        "unknown disposition": {"source_package_disposition": None},
    }

    def test_an_invalid_current_state_is_refused_before_any_file_is_written(self):
        import jsonschema
        for label, patch in self.INVALID_STATES.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                context, packaged = packaged_run(copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
                bad = dataclasses.replace(packaged.realization, current_state={**packaged.realization.current_state, **patch})
                with self.assertRaises(jsonschema.ValidationError):
                    reporter.run(context, dataclasses.replace(packaged, realization=bad))
                after = tree(out)
                self.assertEqual(files(after, "packages") + files(after, "snapshots"), [], "the schema gate runs before the first write")
                self.assertIn("VALIDATION_COMPLETED", events(after), "the ledger is the Packager's and is complete")

    def test_a_missing_or_extra_current_state_key_is_refused(self):
        import jsonschema
        with tempfile.TemporaryDirectory() as tmp:
            context, packaged = packaged_run(copy.deepcopy(self.pkg), copy.deepcopy(self.base), Path(tmp))
            state = dict(packaged.realization.current_state)
            for label, changed in (("missing key", {k: v for k, v in state.items() if k != "maturity_state"}), ("extra key", {**state, "extra": 1})):
                with self.subTest(case=label), self.assertRaises(jsonschema.ValidationError):
                    reporter.run(context, dataclasses.replace(packaged, realization=dataclasses.replace(packaged.realization, current_state=changed)))
            self.assertEqual(files(tree(Path(tmp)), "packages"), [])

    def test_the_facade_run_stops_at_a_snapshot_the_projector_got_wrong(self):
        import jsonschema
        from realizer.packager import packager as packager_module
        original = packager_module.project_snapshot

        def wrong(package, entries):
            snapshot = original(package, entries)
            return dataclasses.replace(snapshot, current_state={**snapshot.current_state, "realization_state": "NOT_A_STATE"})

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(packager_module, "project_snapshot", wrong), self.assertRaises(jsonschema.ValidationError):
            realize(copy.deepcopy(self.pkg), copy.deepcopy(self.base), Path(tmp))
        after = tree(Path(tmp)) if Path(tmp).exists() else {}
        self.assertEqual(files(after, "packages") + files(after, "snapshots"), [])

    def test_a_valid_snapshot_passes_and_the_two_files_are_the_validated_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            context, packaged = packaged_run(copy.deepcopy(self.pkg), copy.deepcopy(self.base), out)
            from realizer.contracts.realization_schema import validate_realization_package
            self.assertIsNone(validate_realization_package(packaged.realization.to_dict()))
            report = reporter.run(context, packaged)
            self.assertEqual(len(report.files), 3)  # the package, the snapshot and the state index
            for path in report.files[:2]:
                self.assertIsNone(validate_realization_package(json.loads(Path(path).read_text(encoding="utf-8"))))

    def test_every_stored_package_and_snapshot_of_the_baseline_validates(self):
        from realizer.contracts.realization_schema import validate_realization_package
        checked = 0
        for path in sorted(canonical.EXPECTED.glob("*/realization/*/*.json")):
            if path.parent.name in ("packages", "snapshots"):
                validate_realization_package(json.loads(path.read_text(encoding="utf-8")))
                checked += 1
        self.assertGreater(checked, 60)


class ProvenanceTests(unittest.TestCase):
    def test_product_version_is_the_publisher_release_in_the_package_and_in_every_ledger_entry(self):
        from publisher.config.versions import PUBLISHER_VERSION
        pkg = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(copy.deepcopy(pkg), demo_observation(pkg, modified=True), Path(tmp))
            written = json.loads(next((Path(tmp) / "realization" / "packages").glob("*.json")).read_text(encoding="utf-8"))
            lines = [json.loads(line) for path in (Path(tmp) / "realization" / "ledger").glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(snapshot.provenance["product_version"], PUBLISHER_VERSION)
        self.assertEqual(written["provenance"]["product_version"], PUBLISHER_VERSION)
        self.assertGreater(len(lines), 3)
        for line in lines:
            self.assertEqual(line["provenance"]["product_version"], PUBLISHER_VERSION, line["event_type"])

    def test_the_stored_baseline_carries_that_version(self):
        from publisher.config.versions import PUBLISHER_VERSION
        versions = set()
        for path in sorted(canonical.EXPECTED.glob("*/realization/packages/*.json")):
            versions.add(json.loads(path.read_text(encoding="utf-8"))["provenance"]["product_version"])
        for path in sorted(canonical.EXPECTED.glob("*/realization/ledger/*.jsonl")):
            versions.update(json.loads(line)["provenance"]["product_version"] for line in path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(versions, {PUBLISHER_VERSION})


class LegacyCompatibilityTests(unittest.TestCase):
    def test_every_snapshot_of_every_scenario_projects_as_stored(self):
        seen = set()
        projections = {}
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            with tempfile.TemporaryDirectory() as tmp:
                for index, step in enumerate(scenario["runs"]):
                    policy = step.get("policy")
                    snapshot = realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(tmp),
                                       replay_through=step.get("replay_through"), policy=None if policy is None else stage_policy(policy))
                    with self.subTest(scenario=name, run=index):
                        projection = reporter.legacy_compatibility(snapshot)
                        projections[f"{name}/{index}"] = projection
                        seen.add((snapshot.current_state["realization_state"], projection["status"], tuple(projection["reasons"])))
        states = {state for state, _, _ in seen}
        for state in ("NOT_APPLIED", "APPLIED", "REALIZED", "UNDER_REALIZED", "VALIDATION_FAILED", "ROLLED_BACK", "MONITORING", "DRIFTED", "BLOCKED", "INVALIDATED"):
            self.assertIn(state, states)
        self.assertGreaterEqual({status for _, status, _ in seen}, {"LOSSLESS", "LOSSY"})
        expect.check(self, "projections", projections, spot={"runs": len(projections), "distinct": sorted([state, status, list(reasons)] for state, status, reasons in seen)})

    def snapshot(self, realization_state, **application):
        return types.SimpleNamespace(current_state={"realization_state": realization_state}, application=application)

    def test_every_state_actor_and_conformance(self):
        states = ["NOT_APPLIED", "APPLIED", "VALIDATING", "REALIZED", "UNDER_REALIZED", "VALIDATION_FAILED", "ROLLED_BACK", "MONITORING",
                  "INVALIDATED", "SOMETHING_NEW", "", "applied"]
        applications = [{}, {"state": "APPLIED"}, {"state": "APPLIED", "actor": "ops@example.com"}, {"state": "APPLIED", "actor": ""},
                        {"implementation_conformance": "MODIFIED"}, {"implementation_conformance": "EXACT", "state": "APPLIED", "actor": None},
                        {"state": "APPLIED", "implementation_conformance": "MODIFIED"}]
        results = {}
        for state in states:
            for application in applications:
                with self.subTest(state=state, application=application):
                    results[f"{state!r} {json.dumps(application, sort_keys=True)}"] = outcome(reporter.legacy_compatibility, self.snapshot(state, **application))
        self.assertEqual(len(results), len(states) * len(applications))
        expect.check(self, "projections", results, spot={"cases": len(results), "statuses": sorted({r[1]["status"] for r in results.values() if r[0] == "ok"})})

    def test_the_unknown_actor_is_not_fabricated(self):
        result = reporter.legacy_compatibility(self.snapshot("APPLIED", state="APPLIED"))
        self.assertEqual(result, {"status": "LOSSY", "legacy_realization_status": "APPLIED", "reasons": ["LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN"]})
        self.assertEqual(reporter.legacy_compatibility(self.snapshot("MONITORING"))["reasons"], ["MONITORING_COLLAPSES_TO_LEGACY_REALIZED"])
        self.assertEqual(reporter.legacy_compatibility(self.snapshot("OTHER")),
                         {"status": "UNREPRESENTABLE", "legacy_realization_status": None, "reasons": ["REALIZER_STATE_OTHER_HAS_NO_LEGACY_MAPPING"]})

    def test_an_invalidated_realization_collapses_to_the_legacy_applied_status(self):
        result = reporter.legacy_compatibility(self.snapshot("INVALIDATED", state="APPLIED", actor="ops@example.com"))
        self.assertEqual(result, {"status": "LOSSY", "legacy_realization_status": "APPLIED", "reasons": ["INVALIDATED_COLLAPSES_TO_LEGACY_APPLIED"]})
        unknown_actor = reporter.legacy_compatibility(self.snapshot("INVALIDATED", state="APPLIED"))
        self.assertEqual(unknown_actor["reasons"], ["INVALIDATED_COLLAPSES_TO_LEGACY_APPLIED", "LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN"])

    def test_malformed_snapshots_raise_the_single_file_exception(self):
        cases = {"no state": types.SimpleNamespace(current_state={}, application={}),
                 "no application": types.SimpleNamespace(current_state={"realization_state": "APPLIED"}, application=None),
                 "empty object": types.SimpleNamespace(), "none": None}
        for label, bad in cases.items():
            with self.subTest(bad=label):
                kind, message = outcome(reporter.legacy_compatibility, bad)
                self.assertNotEqual(kind, "ok")
                expect.check(self, label, (kind, message.replace("types.SimpleNamespace", "SimpleNamespace")))  # the class name reads differently before Python 3.13

    def test_a_state_without_a_legacy_mapping_does_not_read_the_application(self):
        """The single-file release read ``application["actor"]`` into a local it never used, before the state check, so a
        snapshot with an unmapped state and no application mapping raised there. The local is gone; every snapshot the
        engine produces has an application mapping, and the projection of each is unchanged (the scenario and grid tests above)."""
        snapshot = types.SimpleNamespace(current_state={"realization_state": "OTHER"}, application=None)
        self.assertEqual(reporter.legacy_compatibility(snapshot)["status"], "UNREPRESENTABLE")

    def test_is_pure(self):
        snap = self.snapshot("APPLIED", state="APPLIED")
        before = copy.deepcopy(snap)
        printed = io.StringIO()
        with self.assertNoLogs("realizer", logging.DEBUG), contextlib.redirect_stdout(printed):
            reporter.legacy_compatibility(snap)
        self.assertEqual((snap, printed.getvalue()), (before, ""))


class ReporterShapeTests(unittest.TestCase):
    def test_the_workbook_renderer_is_its_own_module_and_imports_no_publisher_code(self):
        """The realization workbook renderer (``workbook.py``) follows the Publisher workbook's rules with its own code:
        the products share no workbook module. The projection it renders is ``contracts/report.py``."""
        folder = Path(reporter.__file__).parent
        self.assertEqual(sorted(p.name for p in folder.glob("*.py")), ["__init__.py", "reporter.py", "workbook.py"])
        imported = set()
        for node in ast.walk(ast.parse((folder / "workbook.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse([name for name in imported if name.split(".")[0] == "publisher"])

    def test_run_and_legacy_compatibility_are_the_public_names(self):
        self.assertEqual(list(inspect.signature(reporter.run).parameters), ["context", "packaged"])


if __name__ == "__main__":
    unittest.main()
