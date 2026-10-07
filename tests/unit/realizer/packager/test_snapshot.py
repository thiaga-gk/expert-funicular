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
import unittest

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import canonical, fixture_package, outcome  # noqa: E402
from tests.unit.realizer.packager.test_ledger import stored_ledgers  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.packager import ledger, snapshot  # noqa: E402


def normalized(value):
    return json.loads(json.dumps(value.to_dict(), sort_keys=True))


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
            cuts = sorted({e.recorded_at_utc for e in entries})
            prefixes = [("file prefix", k, by_file[:k]) for k in range(1, len(entries) + 1)]
            prefixes += [("recorded through", cut, [e for e in entries if e.recorded_at_utc <= cut]) for cut in cuts]
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
            order = list(range(len(entries)))
            rng.shuffle(order)
            shuffled = [entries[i] for i in order]
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
