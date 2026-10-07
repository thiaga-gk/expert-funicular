"""Realizer snapshot projection (src/realizer/packager/snapshot.py): ``project_snapshot``, moved whole.

It returns, and raises, what the stored expectations (``tests/fixtures/realizer/expectations/``) hold, for every stored
ledger, every prefix of it (in file order and cut by recorded time, the two ways a run reaches a ledger state), every
ordering of the entries and malformed input. Every snapshot of the compatibility baseline (RSNAP) is reproduced from a
prefix of its ledger, so a replay through a recorded time reconstructs the state the run had then.
"""
import copy
import dataclasses
import json
import random
import tempfile
import unittest
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import canonical, fixture_package, outcome, run_sequence  # noqa: E402
from tests.unit.realizer.packager.test_ledger import stored_ledgers  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.utils import parse_utc  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.packager import ledger, snapshot  # noqa: E402
from realizer.realizer import build_context  # noqa: E402


def normalized(value):
    return json.loads(json.dumps(value.to_dict(), sort_keys=True))


def created_entries(package):
    """The lineage entry of *package* as ``record_creation`` writes it (a fresh output directory)."""
    with tempfile.TemporaryDirectory() as tmp:
        context = build_context(RealizationRequest(package=copy.deepcopy(package), observation=None, output_dir=Path(tmp)))
        ledger.record_creation(context)
        return ledger.load_ledger(ledger.ledger_path(Path(tmp), context.realization_package_id))


class SourceScopeTests(unittest.TestCase):
    """The snapshot's ``source_recommendation`` and the lineage entry record the package's run scope, cost profile name
    and observed change (null when the package has none)."""

    def test_a_scoped_package_with_an_observed_change(self):
        package = fixture_package("scope-migration-only")
        self.assertTrue(package["run_scope"] and package["observed_change"])
        expected = {"run_scope": package["run_scope"], "cost_profile": "balanced", "observed_change": package["observed_change"]}
        entries = created_entries(package)
        self.assertEqual(len(entries), 1)
        self.assertEqual({k: entries[0].payload.get(k, "missing") for k in expected}, expected)
        source = snapshot.project_snapshot(package, entries).source_recommendation
        self.assertEqual({k: source.get(k, "missing") for k in expected}, expected)

    def test_a_full_scope_package_records_null(self):
        package = fixture_package()
        self.assertIsNone(package["run_scope"])
        self.assertIsNone(package["observed_change"])
        entries = created_entries(package)
        source = snapshot.project_snapshot(package, entries).source_recommendation
        for record in (entries[0].payload, source):
            self.assertEqual({k: record.get(k, "missing") for k in ("run_scope", "observed_change")}, {"run_scope": None, "observed_change": None})
            self.assertEqual(record.get("cost_profile", "missing"), package["cost_profile"]["name"])

    def test_a_package_without_the_blocks_records_null(self):
        entries = created_entries(fixture_package())   # the loader's gate refuses a package without them; the projection must not
        package = copy.deepcopy(fixture_package())
        for key in ("run_scope", "cost_profile", "observed_change"):
            package.pop(key)
        self.assertEqual(ledger.source_scope(package), {"run_scope": None, "cost_profile": None, "observed_change": None})
        source = snapshot.project_snapshot(package, entries).source_recommendation
        self.assertEqual({k: source.get(k, "missing") for k in ("run_scope", "cost_profile", "observed_change")},
                         {"run_scope": None, "cost_profile": None, "observed_change": None})

    def test_the_recorded_blocks_are_copies(self):
        package = fixture_package("scope-migration-only")
        source = snapshot.project_snapshot(package, created_entries(package)).source_recommendation
        source["run_scope"]["scenarios"].clear()
        self.assertTrue(package["run_scope"]["scenarios"])


class ScopedPlanProgressTests(unittest.TestCase):
    """Plan progress reads the package's own plan, which a run scope has already cut to the in-scope steps (and a canary
    rollback kept outside it): applying every step the plan holds completes it."""

    def progress(self, package, config):
        observation = {**demo_observation(package), "config": config}
        outcomes, run_tree = run_sequence(package, [observation], None)
        self.assertEqual(outcomes[0][0], "ok", outcomes)
        snaps = [json.loads(v) for k, v in run_tree.items() if k.startswith("realization/packages/") and isinstance(v, str)]
        self.assertEqual(len(snaps), 1)
        return snaps[0]["application"]["plan_progress"]

    def test_a_one_step_scoped_plan_completes_when_its_step_is_applied(self):
        for name in ("autostop-scoped-pro", "autostop-scoped-never-stop", "autostop-scoped-serverless"):
            package = fixture_package(name)
            step = next(s for s in package["scenarios"][0]["execution_plan"] if s["step_type"] == "CANDIDATE")
            config = {**package_config(package), step["knob"]: step["recommended_value"]}
            with self.subTest(name):
                self.assertEqual(self.progress(package, config), {"state": "COMPLETED", "authoritative_actionable_steps": 1,
                                                                  "applied_steps": 1, "pending_reevaluation_steps": 0})


class ProjectionTests(unittest.TestCase):
    def test_every_ledger_prefix_projects_and_every_stored_snapshot_is_reproduced(self):
        projections = []
        checked = 0
        for name, package, path, _ in stored_ledgers():
            entries = ledger.load_ledger(path)
            by_file_order = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            ids = {entry["ledger_entry_id"]: i for i, entry in enumerate(by_file_order)}
            by_file = sorted(entries, key=lambda e: ids[e.ledger_entry_id])
            reproduced = {}
            cuts = sorted({e.recorded_at_utc for e in entries}, key=parse_utc)
            prefixes = [("file prefix", k, by_file[:k]) for k in range(1, len(entries) + 1)]
            prefixes += [("recorded through", cut, [e for e in entries if parse_utc(e.recorded_at_utc) <= parse_utc(cut)]) for cut in cuts]
            for kind, cut, prefix in prefixes:
                result = snapshot.project_snapshot(package, prefix)
                reproduced[result.snapshot_id] = normalized(result)
                projections.append([name, path.stem, kind, cut, result.snapshot_id, expect.digest(normalized(result))])
            stored = sorted((canonical.EXPECTED / name / "realization" / "snapshots").glob("*.json"))
            self.assertTrue(stored, name)
            for file in stored:
                self.assertIn(file.stem, reproduced, f"{name}: a stored snapshot that no ledger prefix reproduces")
                self.assertEqual(reproduced[file.stem], json.loads(file.read_text(encoding="utf-8")), name)
                checked += 1
        self.assertGreater(len(projections), 400)
        self.assertGreater(checked, 40)
        expect.check(self, "projections", projections, spot={"count": len(projections), "distinct snapshots": len({p[4] for p in projections}),
                                                              "first": projections[0], "last": projections[-1]})

    def test_entry_order_and_duplicates(self):
        rng = random.Random(7)
        doubled_digests = {}
        for name, package, path, _ in stored_ledgers():
            entries = ledger.load_ledger(path)
            expected = normalized(snapshot.project_snapshot(package, entries))
            # entries recorded at different times may come in any order; at equal time the given (file) order is kept
            groups = {}
            for e in entries:
                groups.setdefault(e.recorded_at_utc, []).append(e)
            keys = list(groups)
            rng.shuffle(keys)
            shuffled = [e for k in keys for e in groups[k]]
            self.assertEqual(normalized(snapshot.project_snapshot(package, shuffled)), expected, name)
            doubled_digests[f"{name}/{path.stem}"] = expect.digest(normalized(snapshot.project_snapshot(package, entries + entries)))
        expect.check(self, "doubled ledgers", doubled_digests, spot={"count": len(doubled_digests), "first": next(iter(doubled_digests.items()))})

    def test_malformed_input_raises(self):
        package = fixture_package()
        name, _, path, _ = next(stored_ledgers())
        entries = ledger.load_ledger(path)
        cases = {
            "empty ledger": (package, []),
            "entries None": (package, None),
            "entry None": (package, [None]),
            "entry a mapping": (package, [{}]),
            "package without id": ({k: v for k, v in package.items() if k != "recommendation_package_id"}, entries),
            "package without warehouse": ({k: v for k, v in package.items() if k != "warehouse"}, entries),
            "package warehouse not a mapping": ({**package, "warehouse": 5}, entries),
            "package plan not a mapping": ({**package, "recommended_plan": 5}, entries),
            "package without scenarios": ({k: v for k, v in package.items() if k != "execution_plan"}, entries),
            "package a list": ([1], entries),
        }
        for label, (pkg, given) in cases.items():
            with self.subTest(case=label):
                expect.check(self, label, outcome(lambda: normalized(snapshot.project_snapshot(copy.deepcopy(pkg), given))))
        self.assertEqual(outcome(snapshot.project_snapshot, package, [])[0], "ValueError")
        self.assertEqual(outcome(snapshot.project_snapshot, package, [])[1], "cannot project empty ledger")

    def test_a_tampered_entry_changes_the_snapshot(self):
        _, package, path, _ = next((s for s in stored_ledgers() if s[0] == "GR-024"))
        entries = ledger.load_ledger(path)
        base = normalized(snapshot.project_snapshot(package, entries))
        self.assertNotEqual(normalized(snapshot.project_snapshot(package, entries[:-1])), base)


class ApplicationAuthorityTests(unittest.TestCase):
    """Only an application of a plan step is the snapshot's application; a contrary one invalidates the realization."""

    @staticmethod
    def stored(scenario):
        _, package, path, _ = next(s for s in stored_ledgers() if s[0] == scenario)
        return package, ledger.load_ledger(path)

    def test_an_independent_change_is_never_the_application(self):
        package, entries = self.stored("GR-003")   # an exact, confirmed application that realized
        self.assertEqual(snapshot.project_snapshot(package, entries).application["state"], "APPLIED")
        relabeled = [dataclasses.replace(e, event_type=rc.EventType.INDEPENDENT_CHANGE_OBSERVED) if e.event_type == rc.EventType.APPLICATION_OBSERVED else e
                     for e in entries]
        self.assertNotEqual(relabeled, entries)
        result = snapshot.project_snapshot(package, relabeled)
        self.assertEqual(result.application["state"], "NOT_APPLIED")

    def test_a_contrary_application_is_invalidated(self):
        package, entries = self.stored("GR-006")
        result = snapshot.project_snapshot(package, entries)
        self.assertEqual(result.application["implementation_conformance"], "CONTRARY")
        self.assertEqual(result.application["state"], "APPLIED")
        self.assertEqual(result.current_state["realization_state"], "INVALIDATED")


if __name__ == "__main__":
    unittest.main()
