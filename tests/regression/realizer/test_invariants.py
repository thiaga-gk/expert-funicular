"""The ledger invariant checker (``invariants.py``) itself: real states hold every invariant, and a hand-corrupted copy of
them breaks the invariant the corruption targets, one test per invariant.

The states are real runs into one directory: wh-demo-039's daily packages and its aggressive package with the change
measured against each (supersession across lineages, claims by another lineage), wh-demo-040's canary and its rollback
(a successor, a rollback by a later package, an interval handed over), and the stored scenarios GR-022 (a replacement
the caller names) and GR-026 (a billing correction).
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical, invariants  # noqa: E402
from tests.unit.realizer.helpers import fixture_package  # noqa: E402
from tests.unit.realizer.lineage_support import AGGRESSIVE, BALANCED, measured, realize_all, stored_run  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.realization import make_rpkg_id  # noqa: E402
from realizer.realizer import realize  # noqa: E402


def _state(runs) -> Dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        realize_all(runs, Path(tmp))
        return invariants.read_state(Path(tmp))


def _stored(name: str) -> Dict[str, str]:
    scenario = canonical.load_scenario(name)
    with tempfile.TemporaryDirectory() as tmp:
        for step in scenario["runs"]:
            realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step["observation"]), Path(tmp))
        return invariants.read_state(Path(tmp))


def rpkg_of(package: Mapping[str, Any]) -> str:
    return make_rpkg_id(package["recommendation_package_id"])


def entries(files: Mapping[str, str], rpkg: str) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in invariants.ledgers(files)[rpkg].splitlines() if line.strip()]


def with_entries(files: Mapping[str, str], rpkg: str, change: Callable[[List[Dict[str, Any]]], None]) -> Dict[str, str]:
    """A copy of *files* whose ledger *rpkg* has its parsed entries changed in place by *change*."""
    lines = entries(files, rpkg)
    change(lines)
    out = dict(files)
    out[f"{invariants.LEDGER}{rpkg}.jsonl"] = "".join(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n" for e in lines)
    return out


def first(lines: List[Dict[str, Any]], event_type: str) -> Dict[str, Any]:
    return next(e for e in lines if e["event_type"] == event_type)


class InvariantCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        daily = [stored_run(BALANCED)[0], fixture_package("autostop-daily-oct02"), fixture_package("autostop-daily-oct03")]
        aggressive = stored_run(AGGRESSIVE)
        cls.daily = daily
        cls.aggressive = aggressive[0]
        cls.fleet = _state([(p, measured(p)) for p in daily] + [aggressive])
        canary, rollback = fixture_package("canary-before-cut"), fixture_package("canary-rollback")
        cut, back = demo_observation(canary), demo_observation(rollback)
        cut.update({"applied_at_utc": "2026-09-27T00:00:00Z", "observed_at_utc": "2026-09-30T00:00:00Z", "recorded_at_utc": "2026-09-30T00:00:00Z",
                    "window_start_utc": "2026-09-27T00:00:00Z", "window_end_utc": "2026-09-30T00:00:00Z", "elapsed_days": 3})
        back.update({"applied_at_utc": "2026-10-03T00:00:00Z", "observed_at_utc": "2026-10-09T00:00:00Z", "recorded_at_utc": "2026-10-09T00:00:00Z",
                     "window_start_utc": "2026-10-03T00:00:00Z", "window_end_utc": "2026-10-09T00:00:00Z", "elapsed_days": 6})
        cls.canary, cls.rollback = canary, rollback
        cls.canary_state = _state([(canary, cut), (rollback, back)])
        cls.replaced = _stored("GR-022")
        cls.corrected = _stored("GR-026")

    def broken(self, files: Mapping[str, str], invariant: int, **kwargs: Any) -> List[str]:
        """The violations of *files*, asserting one of them is of *invariant*."""
        found = invariants.violations(files, **kwargs)
        self.assertTrue(any(p.startswith(f"({invariant}) ") for p in found), found)
        return found

    def test_real_states_hold_every_invariant(self):
        for label, files in (("daily and aggressive", self.fleet), ("canary and rollback", self.canary_state),
                             ("GR-022", self.replaced), ("GR-026", self.corrected)):
            with self.subTest(state=label):
                self.assertEqual(invariants.violations(files), [])
        # The states hold what the corruptions below break.
        kinds = {e["event_type"] for f in (self.fleet, self.canary_state) for r in invariants.ledgers(f) for e in entries(f, r)}
        self.assertTrue({"SUCCESSOR_PACKAGE_LINKED", "APPLICATION_CLAIMED_BY_OTHER_LINEAGE", "ROLLBACK_OBSERVED", "SOURCE_PACKAGE_SUPERSEDED"} <= kinds)

    def test_1_a_ledger_rewritten_by_a_run_is_not_append_only(self):
        before = invariants.ledgers(self.fleet)
        # another lineage's ledger (not the run's own) loses an entry, or has one rewritten in place
        other = rpkg_of(self.daily[0])
        dropped = with_entries(self.fleet, other, lambda lines: lines.pop(1))
        self.broken(dropped, 1, before=before)
        rewritten = dict(self.fleet)
        rel = f"{invariants.LEDGER}{other}.jsonl"
        rewritten[rel] = rewritten[rel].replace('"run_scope":', '"run_scope_x":', 1)
        self.assertNotEqual(rewritten[rel], self.fleet[rel])
        self.broken(rewritten, 1, before=before)
        gone = {k: v for k, v in self.fleet.items() if k != rel}
        self.broken(gone, 1, before=before)
        # appending is fine
        self.assertEqual(invariants.violations(self.fleet, before={r: t[: t.index("\n") + 1] for r, t in before.items()}), [])

    def test_2_a_repeated_entry_id_or_a_second_warehouse(self):
        rpkg = rpkg_of(self.daily[2])
        self.broken(with_entries(self.fleet, rpkg, lambda lines: lines.append(dict(lines[-1]))), 2)

        def other_warehouse(lines):
            lines[-1]["warehouse_id"] = "wh-demo-999"
        self.broken(with_entries(self.fleet, rpkg, other_warehouse), 2)

    def test_3_an_entry_of_another_lineage_or_a_misnamed_ledger(self):
        rpkg = rpkg_of(self.daily[2])

        def foreign(lines):
            lines[-1]["recommendation_package_id"] = self.daily[1]["recommendation_package_id"]
        self.broken(with_entries(self.fleet, rpkg, foreign), 3)
        renamed = dict(self.fleet)
        renamed[f"{invariants.LEDGER}RPKG-{'0' * 24}.jsonl"] = renamed.pop(f"{invariants.LEDGER}{rpkg}.jsonl")
        self.broken(renamed, 3)

    def test_4_a_reference_to_a_later_or_unknown_entry(self):
        rpkg = rpkg_of(self.daily[2])

        def forward(lines):
            lines[1]["caused_by_entry_id"] = lines[-1]["ledger_entry_id"]
        self.broken(with_entries(self.fleet, rpkg, forward), 4)
        correction = first(entries(self.corrected, next(iter(invariants.ledgers(self.corrected)))), "BILLING_CORRECTION_RECORDED")
        self.assertIsNotNone(correction["corrects_entry_id"])

        def unknown(lines):
            first(lines, "BILLING_CORRECTION_RECORDED")["corrects_entry_id"] = "RLE-" + "0" * 24
        self.broken(with_entries(self.corrected, next(iter(invariants.ledgers(self.corrected))), unknown), 4)

    def test_5_a_cross_lineage_id_naming_no_lineage(self):
        stranger = "PKG-" + "B" * 24
        cases = [
            (self.canary_state, rpkg_of(self.canary), "SUCCESSOR_PACKAGE_LINKED", "successor_package_id"),
            (self.canary_state, rpkg_of(self.canary), "ROLLBACK_OBSERVED", "rollback_recommendation_package_id"),
            (self.fleet, rpkg_of(self.aggressive), "APPLICATION_CLAIMED_BY_OTHER_LINEAGE", "owner_recommendation_package_id"),
            (self.fleet, rpkg_of(self.daily[0]), "SOURCE_PACKAGE_SUPERSEDED", "replacement_package_id"),
        ]
        for files, rpkg, kind, key in cases:
            with self.subTest(event=kind):
                def change(lines, kind=kind, key=key):
                    first(lines, kind)["payload"][key] = stranger
                self.broken(with_entries(files, rpkg, change), 5)
        # an owner's RPKG id that is not the owner package's
        def owner_rpkg(lines):
            first(lines, "APPLICATION_CLAIMED_BY_OTHER_LINEAGE")["payload"]["owner_realization_package_id"] = rpkg_of(self.daily[0])
        self.broken(with_entries(self.fleet, rpkg_of(self.aggressive), owner_rpkg), 5)
        # A replacement the caller named (GR-022's literal id) is checked for its form only.
        rpkg = next(iter(invariants.ledgers(self.replaced)))
        self.assertEqual(first(entries(self.replaced, rpkg), "SOURCE_PACKAGE_SUPERSEDED")["payload"], {"replacement_package_id": "PKG-" + "A" * 24})
        for bad in ("PKG-xyz", None, first(entries(self.replaced, rpkg), "REALIZATION_CREATED")["recommendation_package_id"]):
            with self.subTest(replacement=bad):
                def named(lines, bad=bad):
                    first(lines, "SOURCE_PACKAGE_SUPERSEDED")["payload"]["replacement_package_id"] = bad
                self.broken(with_entries(self.replaced, rpkg, named), 5)

    def test_6_a_written_package_the_ledger_does_not_replay_to(self):
        rel = f"{invariants.PACKAGES}{rpkg_of(self.daily[1])}.json"
        edited = json.loads(self.fleet[rel])
        edited["attribution"]["attribution_claim_status"] = "CLAIMABLE"
        self.broken({**self.fleet, rel: json.dumps(edited)}, 6)
        edited = json.loads(self.fleet[rel])
        edited["snapshot_id"] = "RSNAP-" + "0" * 24
        self.broken({**self.fleet, rel: json.dumps(edited)}, 6)
        self.broken({k: v for k, v in self.fleet.items() if k != rel}, 6)
        # an entry dropped from the end of the ledger after the package was written
        self.broken(with_entries(self.fleet, rpkg_of(self.daily[1]), lambda lines: lines.pop()), 6)

    def test_6_replay_honours_the_cut_of_the_run(self):
        scenario = canonical.load_scenario("replay-through-generation")
        cut = [s.get("replay_through") for s in scenario["runs"] if s.get("replay_through")]
        self.assertTrue(cut)
        with tempfile.TemporaryDirectory() as tmp:
            check = invariants.RunChecker("replay-through-generation")
            for step in scenario["runs"]:
                realize(copy.deepcopy(scenario["package_doc"]), copy.deepcopy(step["observation"]), Path(tmp), replay_through=step.get("replay_through"))
                check(scenario["package_doc"], Path(tmp), replay_through=step.get("replay_through"))
            files = invariants.read_state(Path(tmp))
        # Without the cut the whole ledger does not replay to the package the cut run wrote.
        self.broken(files, 6)

    def test_7_overlapping_owned_intervals(self):
        # Without its claim by the owner, the aggressive lineage owns the same change over the same interval.
        def unclaimed(lines):
            lines[:] = [e for e in lines if e["event_type"] != "APPLICATION_CLAIMED_BY_OTHER_LINEAGE"]
        self.broken(with_entries(self.fleet, rpkg_of(self.aggressive), unclaimed), 7)

        # The rollback applied before the canary's interval ended: [09-27, 10-03) and [10-02, ...) intersect.
        def earlier(lines):
            first(lines, "APPLICATION_OBSERVED")["payload"]["effective_from_utc"] = "2026-10-02T00:00:00Z"
        self.broken(with_entries(self.canary_state, rpkg_of(self.rollback), earlier), 7)

        # An interval that ends before it starts.
        def reversed_(lines):
            first(lines, "ROLLBACK_OBSERVED")["payload"]["effective_from_utc"] = "2026-09-01T00:00:00Z"
        self.broken(with_entries(self.canary_state, rpkg_of(self.canary), reversed_), 7)

        # Half-open: the canary's interval ends exactly where the rollback's starts, which is no overlap.
        intervals = [i for r in invariants.ledgers(self.canary_state)
                     for i in invariants.owned_intervals(r, entries(self.canary_state, r), self.rollback if r == rpkg_of(self.rollback) else self.canary)]
        self.assertEqual(sorted((str(i[3]), str(i[4])) for i in intervals),
                         [("2026-09-27 00:00:00+00:00", "2026-10-03 00:00:00+00:00"), ("2026-10-03 00:00:00+00:00", "None")])


if __name__ == "__main__":
    unittest.main()
