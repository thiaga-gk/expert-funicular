"""Realizer facade (src/realizer/realizer.py): the capabilities once each, in order, and nothing else.

``realize_request`` is the pipeline (``build_context``, the lineage entry, then the Analyzer, Governor, Reconciler,
Estimator, Reviewer, Packager and Reporter) and ``realize`` is the positional entry point over it. The facade holds no
domain logic: its functions only call capabilities (checked on the source), the Estimator and the Reviewer gate
themselves, and the facade re-exports nothing but what the CLI needs.
"""
import ast
import copy
import dataclasses
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.helpers import Untestable, canonical, fixture_package, tree  # noqa: E402
from realizer import realizer as facade  # noqa: E402
from realizer.analyzer import analyzer  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import RealizationEstimate, ReviewResult  # noqa: E402
from realizer.contracts.report import RealizationReport  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.estimator import estimator  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.packager import packager  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402
from realizer.reporter import reporter  # noqa: E402
from realizer.reviewer import reviewer  # noqa: E402

STAGES = ("analyzer", "governor", "reconciler", "estimator", "reviewer", "packager", "reporter")
MODULES = {"analyzer": analyzer, "governor": governor, "reconciler": reconciler, "estimator": estimator, "reviewer": reviewer,
           "packager": packager, "reporter": reporter}


class OrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def recorded(self, observation, out):
        """One ``realize_request`` with every stage wrapped: the calls in order, each with its arguments and result."""
        calls = []

        def wrap(label, original):
            def run(*args):
                result = original(*args)
                calls.append((label, args, result))
                return result
            return run

        with mock.patch.object(packager, "begin", wrap("begin", packager.begin)), \
                mock.patch.object(facade, "build_context", wrap("build_context", facade.build_context)):
            patches = [mock.patch.object(MODULES[stage], "run", wrap(stage, MODULES[stage].run)) for stage in STAGES]
            for patch in patches:
                patch.start()
            try:
                report = facade.realize_request(RealizationRequest(copy.deepcopy(self.pkg), copy.deepcopy(observation), out))
            finally:
                for patch in patches:
                    patch.stop()
        return report, calls

    def test_each_stage_is_called_once_in_order_with_the_results_handed_on(self):
        for label, observation in (("applied", self.base), ("lineage only", None)):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                report, calls = self.recorded(observation, Path(tmp))
                self.assertEqual([c[0] for c in calls], ["build_context", "begin", *STAGES])
                by = {label: (args, result) for label, args, result in calls}
                context = by["build_context"][1]
                self.assertIs(by["begin"][0][0], context)
                analysis, governance, reconciliation = by["analyzer"][1], by["governor"][1], by["reconciler"][1]
                estimate, reviewed, packaged = by["estimator"][1], by["reviewer"][1], by["packager"][1]
                self.assertEqual(by["analyzer"][0], (context,))
                self.assertEqual(by["governor"][0], (context, analysis))
                self.assertEqual(by["reconciler"][0], (context, analysis, governance))
                self.assertEqual(by["estimator"][0], (context, analysis, governance, reconciliation))
                self.assertEqual(by["reviewer"][0], (context, analysis, governance, reconciliation, estimate))
                self.assertEqual(by["packager"][0], (context, reviewed))
                self.assertEqual(by["reporter"][0], (context, packaged))
                self.assertIs(by["reporter"][1], report)
                for index in range(1, len(calls)):
                    if calls[index][0] != "begin":
                        self.assertTrue(all(a is b for a, b in zip(calls[index][1][:1], [context])), calls[index][0])

    def test_the_lineage_entry_is_recorded_before_the_analyzer(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, calls = self.recorded(self.base, Path(tmp))
            labels = [c[0] for c in calls]
            self.assertLess(labels.index("begin"), labels.index("analyzer"))

    def test_a_failing_stage_stops_the_run_before_the_stages_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            called = []
            for stage in STAGES[3:]:
                self.assertTrue(callable(MODULES[stage].run))
            with mock.patch.object(analyzer, "run", side_effect=ValueError("boom")), \
                    mock.patch.object(governor, "run", side_effect=lambda *a: called.append("governor")):
                with self.assertRaises(ValueError):
                    facade.realize_request(RealizationRequest(self.pkg, self.base, Path(tmp)))
            self.assertEqual(called, [])


class EntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def test_realize_is_realize_request_returning_the_snapshot(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            snapshot = facade.realize(copy.deepcopy(self.pkg), copy.deepcopy(self.base), Path(a))
            report = facade.realize_request(RealizationRequest(copy.deepcopy(self.pkg), copy.deepcopy(self.base), Path(b)))
            self.assertIsInstance(snapshot, rc.RealizationPackage)
            self.assertIsInstance(report, RealizationReport)
            self.assertEqual(snapshot.to_dict(), report.realization.to_dict())
            self.assertEqual(tree(Path(a)), tree(Path(b)))
            self.assertEqual([p.relative_to(b).parts[1] for p in report.files], ["packages", "snapshots"])

    def test_realize_keeps_its_positional_signature(self):
        self.assertEqual(str(inspect.signature(facade.realize)),
                         "(package: 'Mapping[str, Any]', observation: 'Optional[Mapping[str, Any]]', output_dir: 'Path', *, "
                         "policy: 'Optional[RealizerPolicy]' = None, replay_through: 'Optional[str]' = None) -> 'rc.RealizationPackage'")
        self.assertEqual(list(inspect.signature(facade.realize_request).parameters), ["request"])

    def test_every_scenario_through_the_request_entry_equals_realize(self):
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                for step in scenario["runs"]:
                    policy = step.get("policy")
                    old = facade.realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(a),
                                         replay_through=step.get("replay_through"),
                                         policy=None if policy is None else RealizerPolicy.from_mapping(policy))
                    new = facade.realize_request(RealizationRequest(
                        copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(b),
                        policy=None if policy is None else RealizerPolicy.from_mapping(policy), replay_through=step.get("replay_through")))
                    self.assertEqual(new.realization.snapshot_id, old.snapshot_id, name)
                self.assertEqual(tree(Path(b)), tree(Path(a)), name)


class StageGateTests(unittest.TestCase):
    """The Estimator and the Reviewer return an empty result when there is nothing to do: no validation due, or a
    reconciliation that failed. The facade holds no such condition."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def stages(self, observation):
        context = facade.build_context(RealizationRequest(self.pkg, observation, Path("/nonexistent-out")))
        analysis = analyzer.run(context)
        governance = governor.run(context, analysis)
        return context, analysis, governance, reconciler.run(context, analysis, governance)

    def test_nothing_due_means_empty_results_from_both(self):
        cases = {
            "lineage only": None,
            "nothing applied": {**self.base, "config": package_config(self.pkg)},
            "not ready": {**self.base, "elapsed_days": 1},
            "contrary": {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}},
            "reconciliation halted": {**self.base, "rollback_requested": Untestable()},
        }
        for label, observation in cases.items():
            with self.subTest(case=label):
                stages = self.stages(observation)
                estimate = estimator.run(*stages)
                self.assertEqual(estimate, RealizationEstimate())
                reviewed = reviewer.run(*stages, estimate)
                self.assertEqual(reviewed, ReviewResult(analysis=stages[1], governance=stages[2], reconciliation=stages[3], estimate=estimate))

    def test_a_halted_reconciliation_is_never_estimated_or_reviewed_even_if_validation_looks_due(self):
        stages = self.stages(self.base)
        context, analysis, governance, reconciliation = stages
        self.assertTrue(reconciliation.validation_due)
        halted = dataclasses.replace(reconciliation, halt=ValueError("kept"))
        self.assertEqual(estimator.run(context, analysis, governance, halted), RealizationEstimate())
        reviewed = reviewer.run(context, analysis, governance, halted, RealizationEstimate())
        self.assertIsNone(reviewed.validation)
        self.assertIsNone(reviewed.recognition_status)
        self.assertIsNone(reviewed.halt)

    def test_validation_due_is_estimated_and_reviewed(self):
        stages = self.stages(self.base)
        estimate = estimator.run(*stages)
        self.assertIsNotNone(estimate.measurement)
        reviewed = reviewer.run(*stages, estimate)
        self.assertIsNotNone(reviewed.validation)


class NoDomainLogicTests(unittest.TestCase):
    """The facade only sequences: no SQL, lifecycle, realized-savings formula, ledger fold or rendering lives in it."""

    source = Path(inspect.getsourcefile(facade)).read_text(encoding="utf-8")
    tree_ = ast.parse(source)
    functions = {n.name: n for n in tree_.body if isinstance(n, ast.FunctionDef)}

    def test_its_own_functions_are_the_orchestration_entries(self):
        self.assertEqual(set(self.functions), {"build_context", "realize_request", "realize"})
        self.assertEqual([n.name for n in self.tree_.body if isinstance(n, ast.ClassDef)], [])

    def test_the_pipeline_function_is_a_straight_line_of_stage_calls(self):
        body = [n for n in self.functions["realize_request"].body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        calls = []
        for node in body:
            self.assertIsInstance(node, (ast.Assign, ast.Expr, ast.Return), ast.dump(node))
            value = node.value
            self.assertIsInstance(value, ast.Call)
            func = value.func
            calls.append(func.id if isinstance(func, ast.Name) else f"{func.value.id}.{func.attr}")
        self.assertEqual(calls, ["build_context", "packager.begin", "analyzer.run", "governor.run", "reconciler.run", "estimator.run",
                                 "reviewer.run", "packager.run", "reporter.run"])

    def test_no_control_flow_in_the_pipeline_or_its_entry_points(self):
        forbidden = (ast.If, ast.For, ast.While, ast.Try, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.IfExp, ast.BoolOp)
        for name in ("realize_request", "realize"):  # build_context applies the policy default (``or``)
            for node in ast.walk(self.functions[name]):
                self.assertNotIsInstance(node, forbidden, f"{name}: {ast.dump(node)[:80]}")

    def test_the_facade_calls_only_capability_entry_points_and_request_plumbing(self):
        allowed = {"build_context", "validate_package_shape", "RealizerPolicy", "RunContext", "make_rpkg_id", "RealizationRequest",
                   "realize_request", "analyzer.run", "governor.run", "reconciler.run", "estimator.run", "reviewer.run", "packager.run",
                   "packager.begin", "reporter.run"}
        seen = set()
        for function in self.functions.values():
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    func = node.func
                    seen.add(func.id if isinstance(func, ast.Name) else f"{func.value.id}.{func.attr}")
        self.assertLessEqual(seen, allowed)

    def test_no_sql_lifecycle_savings_ledger_or_rendering_vocabulary(self):
        body = "\n".join(ast.get_source_segment(self.source, f) or "" for f in self.functions.values())
        for word in ("SELECT ", "FROM system", "json.dumps", "write_json", "append_if_absent", "project_snapshot", "make_ledger_entry",
                     "calculate_economics", "realized_savings", "xlsx", "openpyxl", "EventType", "RealizationState"):
            self.assertNotIn(word, body)


class NamespaceTests(unittest.TestCase):
    """The facade re-exports only what the CLI needs; every other name comes from its capability package."""

    def test_the_public_names_are_the_entry_points_the_imports_they_need_and_the_cli_pass_throughs(self):
        names = {n for n, value in vars(facade).items() if not n.startswith("_") and not inspect.ismodule(value)}
        self.assertEqual(names, {
            "Any", "Mapping", "Optional", "Path", "RealizationReport", "RealizationRequest", "RealizerPolicy", "RunContext",
            "annotations", "build_context", "demo_observation", "load_observation", "make_rpkg_id", "realize", "realize_request",
            "validate_package_shape",
        })

    def test_the_cli_pass_throughs_are_the_capability_functions(self):
        from realizer.analyzer.collector import collector
        self.assertIs(facade.demo_observation, collector.demo_observation)
        self.assertIs(facade.load_observation, collector.load_observation)
        self.assertIs(facade.RealizerPolicy, RealizerPolicy)

    def test_removed_compatibility_names_and_helpers_are_gone(self):
        for name in ("review", "main", "parse_args", "calculate_economics", "load_ledger", "legacy_compatibility", "ledger_path", "dt", "json"):
            self.assertFalse(hasattr(facade, name), name)
        with self.assertRaises(AttributeError):
            facade.no_such_name  # noqa: B018


if __name__ == "__main__":
    unittest.main()
