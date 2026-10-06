"""Realizer snapshot projection (src/realizer/packager/snapshot.py): ``project_snapshot``, moved whole.

It returns, and raises, what the frozen oracle's copy did, for every stored ledger, every prefix of it (in file order
and cut by recorded time, the two ways a run reaches a ledger state), every ordering of the entries and malformed input.
Every snapshot of the compatibility baseline (RSNAP) is reproduced from a prefix of its ledger, so a replay through a
recorded time reconstructs the state the run had then.
"""
import copy
import inspect
import json
import random
import unittest

from tests.unit.realizer.parity import canonical, engine, fixture_package, oengine, outcome  # noqa: E402
from tests.unit.realizer.packager.test_ledger import stored_ledgers  # noqa: E402
from realizer.packager import ledger, snapshot  # noqa: E402


def normalized(value):
    return json.loads(json.dumps(value.to_dict(), sort_keys=True))


class MovedProjectionTests(unittest.TestCase):
    def test_engine_reexport_source_and_signature(self):
        self.assertIs(engine.project_snapshot, snapshot.project_snapshot)
        self.assertEqual(inspect.getsource(snapshot.project_snapshot), inspect.getsource(oengine.project_snapshot))
        self.assertEqual(str(inspect.signature(snapshot.project_snapshot)), str(inspect.signature(oengine.project_snapshot)))

    def test_every_ledger_prefix_projects_like_the_oracle_and_every_stored_snapshot_is_reproduced(self):
        projections = checked = 0
        for name, package, path, _ in stored_ledgers():
            new, old = ledger.load_ledger(path), oengine.load_ledger(path)
            by_file_order = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            ids = {entry["ledger_entry_id"]: i for i, entry in enumerate(by_file_order)}
            new_by_file = sorted(new, key=lambda e: ids[e.ledger_entry_id])
            old_by_file = sorted(old, key=lambda e: ids[e.ledger_entry_id])
            reproduced = {}
            cuts = sorted({e.recorded_at_utc for e in new})
            prefixes = [(new_by_file[:k], old_by_file[:k]) for k in range(1, len(new) + 1)]
            prefixes += [([e for e in new if e.recorded_at_utc <= cut], [e for e in old if e.recorded_at_utc <= cut]) for cut in cuts]
            for new_entries, old_entries in prefixes:
                result = snapshot.project_snapshot(package, new_entries)
                self.assertEqual(normalized(result), normalized(oengine.project_snapshot(package, old_entries)), name)
                reproduced[result.snapshot_id] = normalized(result)
                projections += 1
            stored = sorted((canonical.EXPECTED / name / "realization" / "snapshots").glob("*.json"))
            self.assertTrue(stored, name)
            for file in stored:
                self.assertIn(file.stem, reproduced, f"{name}: a stored snapshot that no ledger prefix reproduces")
                self.assertEqual(reproduced[file.stem], json.loads(file.read_text(encoding="utf-8")), name)
                checked += 1
        self.assertGreater(projections, 400)
        self.assertGreater(checked, 40)

    def test_entry_order_and_duplicates(self):
        rng = random.Random(7)
        for name, package, path, _ in stored_ledgers():
            new, old = ledger.load_ledger(path), oengine.load_ledger(path)
            expected = normalized(snapshot.project_snapshot(package, new))
            order = list(range(len(new)))
            rng.shuffle(order)
            shuffled = [new[i] for i in order]
            self.assertEqual(normalized(snapshot.project_snapshot(package, shuffled)), expected, name)
            doubled = new + new
            self.assertEqual(normalized(snapshot.project_snapshot(package, doubled)), normalized(oengine.project_snapshot(package, old + old)), name)

    def test_malformed_input_raises_the_oracle_exception(self):
        package = fixture_package()
        name, _, path, _ = next(stored_ledgers())
        new, old = ledger.load_ledger(path), oengine.load_ledger(path)
        cases = {
            "empty ledger": (package, [], []),
            "entries None": (package, None, None),
            "entry None": (package, [None], [None]),
            "entry a mapping": (package, [{}], [{}]),
            "package without id": ({k: v for k, v in package.items() if k != "recommendation_package_id"}, new, old),
            "package without warehouse": ({k: v for k, v in package.items() if k != "warehouse"}, new, old),
            "package warehouse not a mapping": ({**package, "warehouse": 5}, new, old),
            "package plan not a mapping": ({**package, "recommended_plan": 5}, new, old),
            "package without scenarios": ({k: v for k, v in package.items() if k != "execution_plan"}, new, old),
            "package a list": ([1], new, old),
        }
        for label, (pkg, new_entries, old_entries) in cases.items():
            with self.subTest(case=label):
                expected = outcome(lambda: normalized(oengine.project_snapshot(copy.deepcopy(pkg), old_entries)))
                self.assertEqual(outcome(lambda: normalized(snapshot.project_snapshot(copy.deepcopy(pkg), new_entries))), expected)
        self.assertEqual(outcome(snapshot.project_snapshot, package, [])[0], "ValueError")
        self.assertEqual(outcome(snapshot.project_snapshot, package, [])[1], "cannot project empty ledger")

    def test_a_tampered_entry_changes_the_snapshot(self):
        _, package, path, _ = next((s for s in stored_ledgers() if s[0] == "GR-024"))
        entries = ledger.load_ledger(path)
        base = normalized(snapshot.project_snapshot(package, entries))
        self.assertNotEqual(normalized(snapshot.project_snapshot(package, entries[:-1])), base)


if __name__ == "__main__":
    unittest.main()
