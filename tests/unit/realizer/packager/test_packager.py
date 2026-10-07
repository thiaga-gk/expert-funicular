"""Realizer packager capability (src/realizer/packager/packager.py): ``begin``, ``run`` and its ``PackagingResult``.

For every run of the 41 scenarios, the result holds the ledger the run left (cut at ``replay_through``) and the evidence
and regeneration files written, and the snapshot is the package body the Reporter wrote (the output tree itself is the
stored baseline's). The ledger log lines of every scenario, a replay through any recorded time, and the tree a lineage-only
run or a failed replay cut leaves are stored expectations (``tests/fixtures/realizer/expectations/``). A repeated
observation records nothing, and a billing restatement is recorded as a correction of the earlier measurement. The
mutation check proves the comparison notices two events recorded in the wrong order.
"""
import collections
import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.packager.test_ledger import capture_logs  # noqa: E402
from tests.unit.realizer.helpers import stage_policy, as_line, canonical, compact_tree, events, files, fixture_package, ledger_lines, outcome, staged_scenario_runs, tree  # noqa: E402
from realizer.analyzer import analyzer  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.utils import utc_iso  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import PackagingResult  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.estimator import estimator  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.packager import ledger, packager  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402
from realizer.reviewer import reviewer  # noqa: E402


class EveryScenarioRunTests(unittest.TestCase):
    def test_result_and_output_for_every_run_of_every_scenario(self):
        runs = written = prefixes = 0
        for run in staged_scenario_runs():
            with self.subTest(scenario=run.scenario, run=run.index):
                ((args, result),) = run.calls["packager"]
                context, reviewed = args
                self.assertIsInstance(result, PackagingResult)
                self.assertIs(result.realization.__class__, rc.RealizationPackage)
                # the ledger the run left, as the file lines (file order before and after the run), cut at the replay time
                lines = run.before + run.appended
                ordered = sorted(lines, key=lambda x: (x["recorded_at_utc"], x["ledger_entry_id"]))
                if run.replay_through:
                    cut = utc_iso(run.replay_through)
                    ordered = [x for x in ordered if x["recorded_at_utc"] <= cut]
                    prefixes += 1
                self.assertEqual([as_line(e) for e in result.entries], ordered)
                # the snapshot is the package body the Reporter wrote, under its RPKG and RSNAP names
                rpkg = f"realization/packages/{result.realization.realization_package_id}.json"
                self.assertEqual(json.loads(json.dumps(result.realization.to_dict())), json.loads(run.tree[rpkg]))
                self.assertIn(f"realization/snapshots/{result.realization.snapshot_id}.json", run.tree)
                # the files the Packager wrote exist in the output tree
                out = context.request.output_dir
                relative = [p.relative_to(out).as_posix() for p in result.written]
                self.assertEqual(len(relative), len(set(relative)))
                for rel in relative:
                    self.assertIsNotNone(run.tree.get(rel), rel)
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

    def test_log_lines_for_every_scenario(self):
        logs = {}
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            with tempfile.TemporaryDirectory() as tmp, capture_logs() as lines:
                for step in scenario["runs"]:
                    policy = step.get("policy")
                    realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(tmp),
                            replay_through=step.get("replay_through"), policy=None if policy is None else stage_policy(policy))
            logs[name] = [line for line in lines if "ledger." in line]
            self.assertTrue(any("ledger.append" in line for line in logs[name]), name)
        kinds = collections.Counter(line.split(" rle=")[0] for lines in logs.values() for line in lines)
        expect.check(self, "ledger lines", logs, spot={"scenarios": len(logs), "lines": sum(len(v) for v in logs.values()), "kinds": dict(kinds)})


def run_scenario(name):
    """Run scenario *name* through ``realize`` into a fresh directory; the tree it leaves."""
    scenario = canonical.load_scenario(name)
    with tempfile.TemporaryDirectory() as tmp:
        for step in scenario["runs"]:
            policy = step.get("policy")
            realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step.get("observation")), Path(tmp),
                    replay_through=step.get("replay_through"), policy=None if policy is None else stage_policy(policy))
        return tree(Path(tmp))


class LineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()

    def context(self, out, observation=None, **kw):
        return build_context(RealizationRequest(package=copy.deepcopy(self.pkg), observation=observation, output_dir=out, **kw))

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
            self.assertEqual(outcome(packager.begin, context), ("LedgerError", f"ledger {path.name} line 1 is not a valid ledger entry (JSONDecodeError)"))
            self.assertEqual(path.read_text(encoding="utf-8"), "not json\n")

    def test_lineage_only_run_records_nothing_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            realize(copy.deepcopy(self.pkg), None, Path(tmp))
            lineage_tree = tree(Path(tmp))
        self.assertEqual(events(lineage_tree), ["REALIZATION_CREATED"])
        self.assertEqual(files(lineage_tree, "evidence"), [])
        expect.check(self, "tree", compact_tree(lineage_tree))
        with tempfile.TemporaryDirectory() as tmp:
            context = self.context(Path(tmp))
            packager.begin(context)
            analysis = analyzer.run(context)
            governance = governor.run(context, analysis)
            reconciliation = reconciler.run(context, analysis, governance)
            reviewed = reviewer.run(context, analysis, governance, reconciliation, estimator.run(context, analysis, governance, reconciliation))
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
    def test_an_entry_prefix_through_replay_reproduces_the_snapshot_and_files(self):
        replays = []
        trees = {}
        for name in canonical.scenario_names():
            scenario = canonical.load_scenario(name)
            package = scenario["package_doc"]
            with tempfile.TemporaryDirectory() as out:
                for step in scenario["runs"]:
                    policy = step.get("policy")
                    realize(copy.deepcopy(package), copy.deepcopy(step.get("observation")), Path(out),
                            policy=None if policy is None else stage_policy(policy))
                cuts = sorted({line["recorded_at_utc"] for line in ledger_lines(Path(out))})
                for cut in cuts + ["1999-01-01T00:00:00Z", "2999-01-01T00:00:00Z"]:
                    result = outcome(lambda: realize(copy.deepcopy(package), None, Path(out), replay_through=cut).to_dict())
                    replays.append([name, cut, result[0], expect.digest(json.loads(json.dumps(result, default=str)))])
                trees[name] = compact_tree(tree(Path(out)))
        self.assertGreater(len(replays), 150)
        expect.check(self, "replays", replays, spot={"count": len(replays), "outcomes": dict(collections.Counter(r[2] for r in replays)),
                                                     "first": replays[0], "last": replays[-1]})
        expect.check(self, "trees", trees, spot={"scenarios": len(trees), "files": sum(len(t["files"]) for t in trees.values())})

    def test_replay_cut_is_refused_before_the_commit(self):
        # a cut that is not a time, or is before the package was generated (nothing to project), is an input refusal:
        # the run stops in build_context, before the lineage entry or any event is recorded
        pkg = fixture_package()
        base = demo_observation(pkg)
        results = {}
        for label, cut in (("garbage", "garbage"), ("empty", "1999-01-01T00:00:00Z")):
            with tempfile.TemporaryDirectory() as tmp:
                results[label] = (outcome(realize, copy.deepcopy(pkg), copy.deepcopy(base), Path(tmp), replay_through=cut), tree(Path(tmp)))
        for label, (result, refused_tree) in results.items():
            self.assertEqual(result[0], "InputError")
            self.assertEqual(refused_tree, {}, "nothing was recorded or written")
            expect.check(self, label, (result, compact_tree(refused_tree)))
        self.assertIn("not an ISO-8601 time", results["garbage"][0][1])
        self.assertIn("before the package was generated", results["empty"][0][1])


class IdempotencyTests(unittest.TestCase):
    def test_the_same_observation_twice_records_nothing_the_second_time(self):
        pkg = fixture_package()
        base = demo_observation(pkg)
        with tempfile.TemporaryDirectory() as tmp, capture_logs() as lines:
            realize(copy.deepcopy(pkg), copy.deepcopy(base), Path(tmp))
            first = tree(Path(tmp))
            mark = len(lines)
            realize(copy.deepcopy(pkg), copy.deepcopy(base), Path(tmp))
            second = tree(Path(tmp))
            logs = list(lines[mark:])
        self.assertEqual(events(first), events(second))
        self.assertEqual(second, first, "the second run changes nothing: not a ledger line, not a file")
        self.assertTrue(logs)
        self.assertTrue(all("duplicate_suppressed" in line for line in logs), logs)
        expect.check(self, "first run", compact_tree(first))
        expect.check(self, "second run logs", logs)

    def test_billing_restatement_is_a_correction_of_the_earlier_measurement(self):
        run_tree = run_scenario("GR-026")
        lines = [json.loads(line) for rel, text in run_tree.items() if rel.startswith("realization/ledger/") and text for line in text.splitlines()]
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
                expected = run_scenario(name)
                stored = {rel: text for rel, text in canonical_tree(name).items() if rel.startswith("realization/")}
                self.assertEqual({rel: text for rel, text in expected.items() if text is not None}, stored, "the run is the stored baseline")
                with mock.patch.object(ledger, "append_if_absent", self.swapping(first, second)):
                    mutant = run_scenario(name)
                self.assertNotEqual(mutant, expected)
                self.assertEqual(run_scenario(name), expected)


_TREES = {}


def canonical_tree(name):
    """The stored (baseline) output tree of a scenario: relative path -> text."""
    if name not in _TREES:
        root = canonical.EXPECTED / name
        _TREES[name] = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}
    return _TREES[name]


if __name__ == "__main__":
    unittest.main()
