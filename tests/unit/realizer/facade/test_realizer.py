"""Realizer facade (src/realizer/realizer.py): the capabilities once each, in order, and nothing else.

``realize_request`` is the pipeline (``build_context``, the lineage entry, then the Analyzer, Governor, Reconciler,
Estimator, Reviewer, Packager and Reporter) and ``realize`` is the single-file entry point over it. The facade holds no
domain logic: its functions only call capabilities (checked on the source), the Estimator and the Reviewer gate
themselves, and every name the single-file engine had is still importable from it.
"""
import ast
import copy
import dataclasses
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import Untestable, canonical, engine, fixture_package, oengine, tree  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import RealizationEstimate, ReviewResult  # noqa: E402
from realizer.contracts.report import RealizationReport  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402

# Every name ``realizer.realizer`` had before the reporter and the CLI left it (dumped from the module namespace).
ENGINE_NAMES = (
    "ANALYZER_GATEWAY_VERSION", "ATTRIBUTION_VERSION", "Any", "CONFIG_KNOBS", "Decimal", "Dict",
    "ESTIMATOR_GATEWAY_VERSION", "EXCLUSIVITY_VERSION", "GovernanceResult", "LOGGER", "List",
    "MODELER_GATEWAY_VERSION", "Mapping", "Optional", "POLICY_VERSION", "PRODUCT_VERSION", "PROJECTOR_VERSION",
    "Path", "REALIZER_VERSION", "RealizationAnalysis", "RealizationEstimate", "RealizationRequest", "RealizerPolicy",
    "ReconciliationResult", "RegenerationRequest", "ReviewResult", "RunContext", "SIZE_ORDER", "Sequence", "Tuple",
    "_directional_relation", "analyzer", "annotations", "append_if_absent", "append_regeneration", "argparse",
    "attribution_result", "authoritative_cumulative_realization", "authority_eligible", "build_context",
    "calculate_economics", "candidate_by_id", "candidate_steps", "choose_application", "compare_step", "config_hash",
    "copy", "counterfactual_eligible", "decimal", "demo_observation", "dt", "economic_quality_result", "estimator",
    "expected_annual_for_exact_step", "governor", "json", "latest_event", "ledger_business_key", "ledger_checkpoint",
    "ledger_path", "legacy_compatibility", "load_json", "load_ledger", "load_observation", "load_package",
    "load_policy_mapping", "main", "make_app_id", "make_cf_id", "make_evidence_id", "make_ledger_entry",
    "make_measurement_id", "make_rpkg_id", "make_val_id", "money", "normalize_scalar", "observed_config", "os",
    "package_config", "packager", "parse_args", "photon_bool_from_package", "policy_to_dict", "project_snapshot",
    "ratio_string", "rc", "readiness", "realize", "recommended_scenario", "reconciler", "regeneration_request",
    "request_regeneration", "review", "reviewer", "savings_improvement_material", "scenario_by_id", "setup_logging",
    "utc_iso", "validate_package_shape", "validate_performance", "write_json",
)
STAGES = ("analyzer", "governor", "reconciler", "estimator", "reviewer", "packager", "reporter")


class OrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def recorded(self, observation, out):
        """One ``realize_request`` with every stage wrapped: the calls in order, each with its arguments and result."""
        calls = []

        def wrap(label, original):
            def run(*args):
                result = original(*args)
                calls.append((label, args, result))
                return result
            return run

        with mock.patch.object(engine.packager, "begin", wrap("begin", engine.packager.begin)), \
                mock.patch.object(engine, "build_context", wrap("build_context", engine.build_context)):
            patches = [mock.patch.object(getattr(engine, stage), "run", wrap(stage, getattr(engine, stage).run)) for stage in STAGES]
            for patch in patches:
                patch.start()
            try:
                report = engine.realize_request(RealizationRequest(copy.deepcopy(self.pkg), copy.deepcopy(observation), out))
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
                module = getattr(engine, stage)
                self.assertTrue(callable(module.run))
            with mock.patch.object(engine.analyzer, "run", side_effect=ValueError("boom")), \
                    mock.patch.object(engine.governor, "run", side_effect=lambda *a: called.append("governor")):
                with self.assertRaises(ValueError):
                    engine.realize_request(RealizationRequest(self.pkg, self.base, Path(tmp)))
            self.assertEqual(called, [])


class EntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def test_realize_is_realize_request_returning_the_snapshot(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            snapshot = engine.realize(copy.deepcopy(self.pkg), copy.deepcopy(self.base), Path(a))
            report = engine.realize_request(RealizationRequest(copy.deepcopy(self.pkg), copy.deepcopy(self.base), Path(b)))
            self.assertIsInstance(snapshot, rc.RealizationPackage)
            self.assertIsInstance(report, RealizationReport)
            self.assertEqual(snapshot.to_dict(), report.realization.to_dict())
            self.assertEqual(tree(Path(a)), tree(Path(b)))
            self.assertEqual([p.relative_to(b).parts[1] for p in report.files], ["packages", "snapshots"])

    def test_realize_keeps_the_oracle_signature(self):
        self.assertEqual(str(inspect.signature(engine.realize)).replace("rc.RealizationPackage", "RealizationPackage"),
                         str(inspect.signature(oengine.realize)).replace("rc.RealizationPackage", "RealizationPackage"))
        self.assertEqual(list(inspect.signature(engine.realize_request).parameters), ["request"])

    def test_every_scenario_through_the_request_entry_equals_the_oracle_files(self):
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                for step in scenario["runs"]:
                    policy = step.get("policy")
                    old = oengine.realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(a),
                                          replay_through=step.get("replay_through"),
                                          policy=None if policy is None else oengine.RealizerPolicy.from_mapping(policy))
                    new = engine.realize_request(RealizationRequest(
                        copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(b),
                        policy=None if policy is None else engine.RealizerPolicy.from_mapping(policy), replay_through=step.get("replay_through")))
                    self.assertEqual(new.realization.snapshot_id, old.snapshot_id, name)
                self.assertEqual(tree(Path(b)), tree(Path(a)), name)


class StageGateTests(unittest.TestCase):
    """The Estimator and the Reviewer return an empty result when there is nothing to do: no validation due, or a
    reconciliation that failed. The facade holds no such condition."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def stages(self, observation):
        context = engine.build_context(RealizationRequest(self.pkg, observation, Path("/nonexistent-out")))
        analysis = engine.analyzer.run(context)
        governance = engine.governor.run(context, analysis)
        return context, analysis, governance, engine.reconciler.run(context, analysis, governance)

    def test_nothing_due_means_empty_results_from_both(self):
        cases = {
            "lineage only": None,
            "nothing applied": {**self.base, "config": oengine.package_config(self.pkg)},
            "not ready": {**self.base, "elapsed_days": 1},
            "contrary": {**self.base, "config": {**self.base["config"], "warehouse_size": "X_LARGE"}},
            "reconciliation halted": {**self.base, "rollback_requested": Untestable()},
        }
        for label, observation in cases.items():
            with self.subTest(case=label):
                stages = self.stages(observation)
                estimate = engine.estimator.run(*stages)
                self.assertEqual(estimate, RealizationEstimate())
                reviewed = engine.reviewer.run(*stages, estimate)
                self.assertEqual(reviewed, ReviewResult(analysis=stages[1], governance=stages[2], reconciliation=stages[3], estimate=estimate))
                self.assertEqual(engine.review(*stages), reviewed)

    def test_a_halted_reconciliation_is_never_estimated_or_reviewed_even_if_validation_looks_due(self):
        stages = self.stages(self.base)
        context, analysis, governance, reconciliation = stages
        self.assertTrue(reconciliation.validation_due)
        halted = dataclasses.replace(reconciliation, halt=ValueError("kept"))
        self.assertEqual(engine.estimator.run(context, analysis, governance, halted), RealizationEstimate())
        reviewed = engine.reviewer.run(context, analysis, governance, halted, RealizationEstimate())
        self.assertIsNone(reviewed.validation)
        self.assertIsNone(reviewed.recognition_status)
        self.assertIsNone(reviewed.halt)

    def test_validation_due_is_estimated_and_reviewed(self):
        stages = self.stages(self.base)
        estimate = engine.estimator.run(*stages)
        self.assertIsNotNone(estimate.measurement)
        reviewed = engine.reviewer.run(*stages, estimate)
        self.assertIsNotNone(reviewed.validation)
        self.assertEqual(engine.review(*stages), reviewed)


class NoDomainLogicTests(unittest.TestCase):
    """The facade only sequences: no SQL, lifecycle, realized-savings formula, ledger fold or rendering lives in it."""

    source = Path(inspect.getsourcefile(engine)).read_text(encoding="utf-8")
    tree_ = ast.parse(source)
    functions = {n.name: n for n in tree_.body if isinstance(n, ast.FunctionDef)}

    def test_its_own_functions_are_the_orchestration_and_compatibility_entries(self):
        self.assertEqual(set(self.functions), {"build_context", "realize_request", "realize", "review", "__getattr__"})
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
        for name in ("realize_request", "realize", "review"):  # build_context applies the policy default (``or``)
            for node in ast.walk(self.functions[name]):
                self.assertNotIsInstance(node, forbidden, f"{name}: {ast.dump(node)[:80]}")

    def test_the_facade_calls_only_capability_entry_points_and_request_plumbing(self):
        allowed = {"build_context", "validate_package_shape", "RealizerPolicy", "RunContext", "make_rpkg_id", "RealizationRequest",
                   "realize_request", "analyzer.run", "governor.run", "reconciler.run", "estimator.run", "reviewer.run", "packager.run",
                   "packager.begin", "reporter.run"}
        seen = set()
        for name, function in self.functions.items():
            if name == "__getattr__":
                continue
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


class ReexportTests(unittest.TestCase):
    def test_every_name_the_engine_had_still_resolves(self):
        self.assertEqual(len(ENGINE_NAMES), len(set(ENGINE_NAMES)))
        missing = [n for n in ENGINE_NAMES if not hasattr(engine, n)]
        self.assertEqual(missing, [])

    def test_the_names_keep_their_values_and_homes(self):
        from realizer.cli import main as cli_main, realize as cli_realize
        from realizer.reporter import reporter
        self.assertIs(engine.legacy_compatibility, reporter.legacy_compatibility)
        self.assertIs(engine.parse_args, cli_realize.parse_args)
        self.assertIs(engine.main, cli_main.main)
        self.assertIs(engine.write_json, __import__("realizer.common.utils", fromlist=["write_json"]).write_json)
        self.assertEqual(engine.REALIZER_VERSION, oengine.REALIZER_VERSION)
        self.assertEqual(engine.PRODUCT_VERSION, oengine.PRODUCT_VERSION)

    def test_a_name_that_never_existed_is_still_an_error(self):
        with self.assertRaises(AttributeError):
            engine.no_such_name  # noqa: B018
        self.assertFalse(hasattr(engine, "no_such_name"))

    def test_the_public_contract_surface_is_unchanged(self):
        self.assertNotIn("RealizationReport", rc.__all__)
        for name in oengine.rc.__all__:
            self.assertIn(name, rc.__all__)


if __name__ == "__main__":
    unittest.main()
