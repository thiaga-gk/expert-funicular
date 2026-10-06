"""Realizer packager capability (src/realizer/packager/packager.py): ``begin``, ``run`` and its ``PackagingResult``.

For every run of the 41 scenarios, the entries ``run`` records, the files it writes and the snapshot it projects are the
oracle's: the whole output tree and the log lines are identical, the result holds the ledger the run left (cut at
``replay_through``) and the evidence and regeneration files written, and the snapshot is the oracle's package body. A
replay through any recorded time reproduces the oracle's snapshot, a repeated observation records nothing, and a billing
restatement is recorded as a correction of the earlier measurement. The mutation checks prove the comparison notices
two events recorded in the wrong order.
"""
import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.packager.test_ledger import capture_logs  # noqa: E402
from tests.unit.realizer.parity import as_line, canonical, engine, events, files, fixture_package, ledger_lines, oengine, outcome, staged_scenario_runs, tree  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import PackagingResult, RealizationEstimate, ReviewResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.packager import ledger, packager  # noqa: E402


class EveryScenarioRunTests(unittest.TestCase):
    def test_result_and_output_are_the_oracles_for_every_run_of_every_scenario(self):
        runs = written = prefixes = 0
        for run in staged_scenario_runs():
            with self.subTest(scenario=run.scenario, run=run.index):
                self.assertEqual(run.modular_tree, run.oracle_tree)
                ((args, result),) = run.calls["packager"]
                context, reviewed = args
                self.assertIsInstance(result, PackagingResult)
                self.assertIs(result.realization.__class__, rc.RealizationPackage)
                # the ledger the run left, as the oracle's file lines (file order before and after the run), cut at the replay time
                lines = run.before + run.appended
                ordered = sorted(lines, key=lambda x: (x["recorded_at_utc"], x["ledger_entry_id"]))
                if run.replay_through:
                    cut = engine.utc_iso(run.replay_through)
                    ordered = [x for x in ordered if x["recorded_at_utc"] <= cut]
                    prefixes += 1
                self.assertEqual([as_line(e) for e in result.entries], ordered)
                # the snapshot is the package body the oracle wrote, under its RPKG and RSNAP names
                rpkg = f"realization/packages/{result.realization.realization_package_id}.json"
                self.assertEqual(json.loads(json.dumps(result.realization.to_dict())), json.loads(run.oracle_tree[rpkg]))
                self.assertIn(f"realization/snapshots/{result.realization.snapshot_id}.json", run.oracle_tree)
                # the files the Packager wrote exist in the oracle's tree, with the oracle's name
                out = context.request.output_dir
                relative = [p.relative_to(out).as_posix() for p in result.written]
                self.assertEqual(len(relative), len(set(relative)))
                for rel in relative:
                    self.assertIsNotNone(run.oracle_tree.get(rel), rel)
                if run.observation:
                    self.assertEqual(relative[0], f"realization/evidence/{reviewed.analysis.observation.evidence_id}.json")
                else:
                    self.assertEqual(relative, [])
                reruns = [rel for rel in relative if rel.startswith("realization/regeneration/")]
                self.assertEqual(len(reruns), len(relative) - (1 if run.observation else 0))
                rec = reviewed.reconciliation
                if rec.terminal_regeneration is not None:
                    self.assertEqual(reruns, [f"realization/regeneration/{rec.terminal_regeneration.regeneration_request_id}.json"])
                written += len(relative)
                runs += 1
        self.assertEqual(runs, sum(len(canonical.load_scenario(name)["runs"]) for name in canonical.scenario_names()))
        self.assertGreater(written, 40)
        self.assertGreater(prefixes, 0)

    def test_log_lines_are_the_oracles_for_every_scenario(self):
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            logs = []
            for module in (engine, oengine):
                with tempfile.TemporaryDirectory() as tmp, capture_logs() as lines:
                    for step in scenario["runs"]:
                        policy = step.get("policy")
                        module.realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(tmp),
                                       replay_through=step.get("replay_through"),
                                       policy=None if policy is None else module.RealizerPolicy.from_mapping(policy))
                logs.append([line for line in lines if "ledger." in line])
            self.assertEqual(logs[0], logs[1], name)
            self.assertTrue(any("ledger.append" in line for line in logs[0]), name)


def run_scenario(module, name, runs=None):
    """Run scenario *name* through *module*'s ``realize`` into a fresh directory; the tree it leaves."""
    scenario = canonical.load_scenario(name)
    with tempfile.TemporaryDirectory() as tmp:
        for step in (scenario["runs"] if runs is None else runs):
            policy = step.get("policy")
            module.realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(tmp),
                           replay_through=step.get("replay_through"), policy=None if policy is None else module.RealizerPolicy.from_mapping(policy))
        return tree(Path(tmp))


class LineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()

    def context(self, out, observation=None, **kw):
        return engine.build_context(RealizationRequest(package=copy.deepcopy(self.pkg), observation=observation, output_dir=out, **kw))

    def test_begin_records_the_lineage_entry_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            context = self.context(out)
            with capture_logs() as lines:
                packager.begin(context)
                packager.begin(context)
            self.assertEqual([line.split(" rle=")[0] for line in lines], ["INFO ledger.append event=REALIZATION_CREATED", "INFO ledger.duplicate_suppressed"])
            self.assertEqual(events(tree(out)), ["REALIZATION_CREATED"])

    def test_begin_fails_on_a_corrupt_ledger_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = self.context(Path(tmp))
            path = ledger.ledger_path(Path(tmp), context.realization_package_id)
            path.parent.mkdir(parents=True)
            path.write_text("not json\n", encoding="utf-8")
            self.assertEqual(outcome(packager.begin, context)[0], "JSONDecodeError")
            self.assertEqual(path.read_text(encoding="utf-8"), "not json\n")

    def test_lineage_only_run_records_nothing_more(self):
        trees = []
        for module in (engine, oengine):
            with tempfile.TemporaryDirectory() as tmp:
                module.realize(copy.deepcopy(self.pkg), None, Path(tmp))
                trees.append(tree(Path(tmp)))
        self.assertEqual(trees[0], trees[1])
        self.assertEqual(events(trees[0]), ["REALIZATION_CREATED"])
        self.assertEqual(files(trees[0], "evidence"), [])
        with tempfile.TemporaryDirectory() as tmp:
            context = self.context(Path(tmp))
            packager.begin(context)
            analysis = engine.analyzer.run(context)
            governance = engine.governor.run(context, analysis)
            reconciliation = engine.reconciler.run(context, analysis, governance)
            reviewed = engine.review(context, analysis, governance, reconciliation)
            result = packager.run(context, reviewed)
            self.assertEqual(result.written, ())
            self.assertEqual([e.event_type for e in result.entries], [rc.EventType.REALIZATION_CREATED])
            self.assertEqual(files(tree(Path(tmp)), "packages"), [], "the Packager writes neither the package nor the snapshot")

    def test_result_is_frozen(self):
        result = PackagingResult(realization=None, entries=())  # type: ignore[arg-type]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.entries = ()  # type: ignore[misc]
        self.assertEqual(result.written, ())
        self.assertNotIn("PackagingResult", rc.__all__)


class ReplayTests(unittest.TestCase):
    def test_an_entry_prefix_through_replay_reproduces_the_oracle_snapshot_and_files(self):
        compared = 0
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            package = scenario["package_doc"]
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                for step in scenario["runs"]:
                    policy = step.get("policy")
                    for module, out in ((engine, a), (oengine, b)):
                        module.realize(copy.deepcopy(package), copy.deepcopy(step.get("observation")), Path(out),
                                       policy=None if policy is None else module.RealizerPolicy.from_mapping(policy))
                cuts = sorted({line["recorded_at_utc"] for line in ledger_lines(Path(b))})
                for cut in cuts + ["1999-01-01T00:00:00Z", "2999-01-01T00:00:00Z"]:
                    results = []
                    for module, out in ((engine, a), (oengine, b)):
                        result = outcome(lambda: module.realize(copy.deepcopy(package), None, Path(out), replay_through=cut).to_dict())
                        results.append(json.loads(json.dumps(result, default=str)))
                    self.assertEqual(results[0], results[1], (name, cut))
                    compared += 1
                self.assertEqual(tree(Path(a)), tree(Path(b)), name)
        self.assertGreater(compared, 150)

    def test_replay_is_cut_after_the_commit(self):
        # a cut that is not a time raises only after the run's events were recorded; a cut before every entry leaves nothing to project
        pkg = fixture_package()
        base = oengine.demo_observation(pkg)
        results = []
        for module in (engine, oengine):
            row = []
            for cut in ("garbage", "1999-01-01T00:00:00Z"):
                with tempfile.TemporaryDirectory() as tmp:
                    row.append((outcome(module.realize, copy.deepcopy(pkg), copy.deepcopy(base), Path(tmp), replay_through=cut), tree(Path(tmp))))
            results.append(row)
        self.assertEqual(results[0], results[1])
        (garbage, garbage_tree), (empty, empty_tree) = results[0]
        self.assertNotEqual(garbage[0], "ok")
        self.assertEqual(empty[:2], ("ValueError", "cannot project empty ledger"))
        for failed in (garbage_tree, empty_tree):
            self.assertIn("ECONOMIC_MEASUREMENT_RECORDED", events(failed))
            self.assertEqual(files(failed, "packages"), [])
            self.assertEqual(files(failed, "snapshots"), [])


class IdempotencyTests(unittest.TestCase):
    def test_the_same_observation_twice_records_nothing_the_second_time(self):
        pkg = fixture_package()
        base = oengine.demo_observation(pkg)
        trees, logs = [], []
        for module in (engine, oengine):
            with tempfile.TemporaryDirectory() as tmp, capture_logs() as lines:
                module.realize(copy.deepcopy(pkg), copy.deepcopy(base), Path(tmp))
                first = tree(Path(tmp))
                mark = len(lines)
                module.realize(copy.deepcopy(pkg), copy.deepcopy(base), Path(tmp))
                second = tree(Path(tmp))
                trees.append((first, second))
                logs.append(list(lines[mark:]))
        self.assertEqual(trees[0], trees[1])
        self.assertEqual(logs[0], logs[1])
        first, second = trees[0]
        self.assertEqual(events(first), events(second))
        self.assertEqual(second, first, "the second run changes nothing: not a ledger line, not a file")
        self.assertTrue(logs[0])
        self.assertTrue(all("duplicate_suppressed" in line for line in logs[0]), logs[0])

    def test_billing_restatement_is_a_correction_of_the_earlier_measurement(self):
        ledgers = [run_scenario(module, "GR-026") for module in (engine, oengine)]
        self.assertEqual(ledgers[0], ledgers[1])
        lines = [json.loads(line) for rel, text in ledgers[0].items() if rel.startswith("realization/ledger/") and text for line in text.splitlines()]
        measurement = next(x for x in lines if x["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED")
        corrections = [x for x in lines if x["event_type"] == "BILLING_CORRECTION_RECORDED"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["corrects_entry_id"], measurement["ledger_entry_id"])
        self.assertEqual(sum(x["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED" for x in lines), 2, "the restatement is a second measurement")


class SlotOrderMutationTests(unittest.TestCase):
    """Mutation checks: recording two adjacent events in the other order is caught by the ledger file comparison (the
    snapshot cannot notice, it sorts by recorded time)."""

    PAIRS = (
        ("SOURCE_PACKAGE_SUPERSEDED", "REPLACEMENT_PACKAGE_LINKED"),
        ("APPLICATION_OBSERVED", "INTENT_RECORDED"),
        ("DRIFT_DETECTED", "ROLLBACK_OBSERVED"),
        ("VALIDATION_READY", "VALIDATION_STARTED"),
        ("VALIDATION_STARTED", "VALIDATION_COMPLETED"),
        ("VALIDATION_COMPLETED", "COUNTERFACTUAL_RECORDED"),
        ("COUNTERFACTUAL_RECORDED", "ECONOMIC_MEASUREMENT_RECORDED"),
        ("ECONOMIC_MEASUREMENT_RECORDED", "BILLING_CORRECTION_RECORDED"),
        ("ECONOMIC_MEASUREMENT_RECORDED", "REGENERATION_REQUESTED"),
    )

    @staticmethod
    def swapping(first, second):
        original = ledger.append_if_absent
        held = []

        def append(path, entry):
            if entry.event_type.value == first and not held:
                held.append((path, entry))
                return True
            result = original(path, entry)
            if entry.event_type.value == second and held:
                original(*held.pop())
            return result

        return append

    @staticmethod
    def scenario_with(first, second):
        for name in canonical.scenario_names():
            for text in (t for rel, t in canonical_tree(name).items() if rel.startswith("realization/ledger/")):
                kinds = [json.loads(line)["event_type"] for line in text.splitlines() if line.strip()]
                if any(a == first and b == second for a, b in zip(kinds, kinds[1:])):
                    return name
        return None

    def test_every_adjacent_pair_of_slots_is_pinned(self):
        for first, second in self.PAIRS:
            with self.subTest(first=first, second=second):
                name = self.scenario_with(first, second)
                self.assertIsNotNone(name, "no scenario records the pair")
                expected = run_scenario(oengine, name)
                self.assertEqual(run_scenario(engine, name), expected)
                with mock.patch.object(ledger, "append_if_absent", self.swapping(first, second)):
                    mutant = run_scenario(engine, name)
                self.assertNotEqual(mutant, expected)
                self.assertEqual(run_scenario(engine, name), expected)


_TREES = {}


def canonical_tree(name):
    """The stored (baseline) output tree of a scenario: relative path -> text."""
    if name not in _TREES:
        root = canonical.EXPECTED / name
        _TREES[name] = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}
    return _TREES[name]


if __name__ == "__main__":
    unittest.main()
