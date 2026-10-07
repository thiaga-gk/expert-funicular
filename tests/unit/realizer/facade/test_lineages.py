"""Several lineages of one warehouse in one output directory (src/realizer/reconciler/lineage.py and the Packager's and
Reporter's cross-lineage files), realized from real Publisher packages.

* Two packages from one epoch and one generation time (the end-to-end goldens ``ge06-profile-aggressive-modified`` and
  ``ge07-profile-balanced-exact``, auto-stop 365 to 15 and 365 to 210) and one change (auto-stop 210 on 2026-10-08 14:32):
  the balanced package's step is applied exactly, so its lineage owns the change and claims 1,069.73; the aggressive
  lineage's realization is observable only and names the owner, in either run order, with identical files.
* Daily Publisher runs (the fixtures ``autostop-daily-oct02`` and ``-oct03`` after the golden's 2026-10-01 package): the
  most recent package before the change owns it, the older ones are superseded by it, whatever the run order.
* The package after the change (``successor-after-autostop``) is linked as the successor of the applied lineage, which
  keeps realizing: its expected savings are not a material improvement.
* A canary rollback (``canary-rollback``) applied after its canary (``canary-before-cut``, the epoch the rollback's
  observed change starts from): the earlier lineage is rolled back at the time the rollback was applied.
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple
from unittest import mock

from tests.unit.realizer.helpers import canonical, fixture_package  # noqa: F401  (canonical: src/realizer, never a shadowing copy)
from tests.unit.realizer.lineage_support import AGGRESSIVE, BALANCED, final_files, measured, realize_all, rpkg_file, state_index, stored_run
from tests.golden.realizer import e2e
from realizer.analyzer.collector.collector import demo_observation
from realizer.common.errors import InputError
from realizer.contracts import realization as rc
from realizer.contracts.realization import make_rpkg_id
from realizer.packager.ledger import ledger_path, load_ledger
from realizer.realizer import realize


class ProfileAlternativesTests(unittest.TestCase):
    """Two cost-profile packages of one epoch, generated at the same time, and the change both their steps realize."""

    @classmethod
    def setUpClass(cls):
        cls.aggressive, cls.balanced = stored_run(AGGRESSIVE), stored_run(BALANCED)

    def realized(self, order: Sequence[Tuple[Mapping, Mapping]]) -> Tuple[Dict, Dict, Dict, Dict[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize_all(order, out)
            return (rpkg_file(out, self.balanced[0]), rpkg_file(out, self.aggressive[0]), state_index(out), final_files(out))

    def test_alone_each_package_claims_the_change(self):
        # The defect: each realization package alone reports 1,069.73 claimable, 2,139.46 for one change in a fleet sum.
        for package, observation in (self.aggressive, self.balanced):
            with tempfile.TemporaryDirectory() as tmp:
                realize(package, observation, Path(tmp))
                attribution = rpkg_file(Path(tmp), package)["attribution"]
                self.assertEqual((attribution["attribution_claim_status"], attribution["recommendation_attributed_realized_savings"]),
                                 ("CLAIMABLE", "1069.73"))

    def test_the_exact_application_owns_the_change_in_either_order(self):
        results = {name: self.realized(order) for name, order in (("aggressive first", (self.aggressive, self.balanced)),
                                                                   ("balanced first", (self.balanced, self.aggressive)))}
        for name, (balanced, aggressive, index, _) in results.items():
            with self.subTest(order=name):
                self.assertEqual(balanced["application"]["implementation_conformance"], "EXACT")
                self.assertEqual(balanced["attribution"]["attribution_claim_status"], "CLAIMABLE")
                self.assertEqual(balanced["attribution"]["recommendation_attributed_realized_savings"], "1069.73")
                self.assertEqual(balanced["economics"]["annualized_realized_savings"], "13944.69")
                self.assertNotIn("claimed_by", balanced["attribution"])
                self.assertEqual(aggressive["application"]["implementation_conformance"], "MODIFIED")
                self.assertEqual(aggressive["attribution"]["attribution_claim_status"], "OBSERVABLE_ONLY")
                self.assertEqual(aggressive["attribution"]["attribution_state"], "PARTIAL_ATTRIBUTION")  # an inferred origin
                self.assertEqual(aggressive["attribution"]["recommendation_attributed_realized_savings"], "0.00")
                self.assertEqual(aggressive["attribution"]["hard_disqualifiers"], ["CLAIMED_BY_OTHER_LINEAGE"])
                self.assertEqual(aggressive["attribution"]["claimed_by"], [{
                    "knob": "auto_stop_minutes", "realization_package_id": balanced["realization_package_id"],
                    "recommendation_package_id": balanced["source_recommendation"]["recommendation_package_id"]}])
                # The realization itself is still reported: observable, never claimed.
                self.assertEqual(aggressive["attribution"]["validated_observed_realization"], "1069.73")
                self.assertEqual([o["recommendation_package_id"] for o in index["applied_knob_owners"]],
                                 [balanced["source_recommendation"]["recommendation_package_id"]])
                claimed = sum(Decimal(r["recommendation_attributed_realized_savings"]) for r in index["lineages"])
                self.assertEqual(claimed, Decimal("1069.73"))
        # Not only the decisions: the ledgers, the realization packages and the state index are the same files.
        self.assertEqual(results["aggressive first"][3], results["balanced first"][3])

    def test_the_caller_overlap_count_still_adds_a_disqualifier(self):
        package, observation = self.balanced
        with tempfile.TemporaryDirectory() as tmp:
            realize(package, {**observation, "overlap_count": 1}, Path(tmp))
            attribution = rpkg_file(Path(tmp), package)["attribution"]
        self.assertEqual(attribution["attribution_claim_status"], "OBSERVABLE_ONLY")
        self.assertIn("PORTFOLIO_SCOPE_OVERLAP", attribution["hard_disqualifiers"])


class DailyRunTests(unittest.TestCase):
    """Three daily Publisher packages of one epoch (2026-10-01, -02, -03), each recommending auto-stop 365 to 210."""

    @classmethod
    def setUpClass(cls):
        cls.packages = [stored_run(BALANCED)[0], fixture_package("autostop-daily-oct02"), fixture_package("autostop-daily-oct03")]
        cls.observations = [measured(p) for p in cls.packages]

    def test_never_applied_lineages_are_superseded_by_the_newest_package_of_their_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize_all([(self.packages[0], None)], out)
            self.assertEqual(state_index(out)["active_package_ids"], [self.packages[0]["recommendation_package_id"]])
            realize_all([(self.packages[1], None)], out)
            first = rpkg_file(out, self.packages[0])
            self.assertEqual(first["current_state"]["source_package_disposition"], "SUPERSEDED")
            self.assertEqual(first["source_recommendation"]["superseded_by_package_id"], self.packages[1]["recommendation_package_id"])
            self.assertEqual(first["regeneration"]["status"], "REPLACED")
            realize_all([(self.packages[2], None)], out)
            index = state_index(out)
            superseded = [(e.payload["replacement_package_id"], e.payload["reason"], e.effective_at_utc)
                          for p in self.packages[:2] for e in load_ledger(ledger_path(out, make_rpkg_id(p["recommendation_package_id"])))
                          if e.event_type.value == "SOURCE_PACKAGE_SUPERSEDED"]
        newest = self.packages[2]["recommendation_package_id"]
        self.assertEqual(index["active_package_ids"], [newest])
        self.assertEqual([r["superseded_by_package_id"] for r in index["lineages"]], [newest, newest, None])
        self.assertEqual([r["application_state"] for r in index["lineages"]], ["NOT_APPLIED"] * 3)
        # Recorded as each newer package arrived: the first lineage was superseded by the second, then by the third.
        self.assertEqual(superseded, [
            (self.packages[1]["recommendation_package_id"], "NEWER_PACKAGE_SAME_EPOCH", "2026-10-02T12:00:00Z"),
            (newest, "NEWER_PACKAGE_SAME_EPOCH", "2026-10-03T12:00:00Z"),
            (newest, "NEWER_PACKAGE_SAME_EPOCH", "2026-10-03T12:00:00Z"),
        ])

    def test_the_most_recent_package_before_the_change_owns_it_in_any_order(self):
        runs = list(zip(self.packages, self.observations))
        outcomes = []
        for order in (runs, runs[::-1], [runs[1], runs[2], runs[0]]):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                realize_all(order, out)
                packages = [rpkg_file(out, p) for p in self.packages]
                outcomes.append(([(r["attribution"]["attribution_claim_status"], r["attribution"]["recommendation_attributed_realized_savings"],
                                   [o["recommendation_package_id"] for o in r["attribution"].get("claimed_by") or []],
                                   r["current_state"]["source_package_disposition"], r["source_recommendation"]["superseded_by_package_id"])
                                  for r in packages], state_index(out)))
        newest = self.packages[2]["recommendation_package_id"]
        claim = outcomes[0][0][2][1]
        self.assertEqual(outcomes[0][0], [
            ("OBSERVABLE_ONLY", "0.00", [newest], "SUPERSEDED", newest),
            ("OBSERVABLE_ONLY", "0.00", [newest], "SUPERSEDED", newest),
            ("CLAIMABLE", claim, [], "ACTIVE", None),
        ])
        self.assertGreater(Decimal(claim), 0)
        for decisions, index in outcomes[1:]:
            self.assertEqual(decisions, outcomes[0][0])
            self.assertEqual(index["applied_knob_owners"], outcomes[0][1]["applied_knob_owners"])
            self.assertEqual(index["active_package_ids"], [newest])


class SuccessorTests(unittest.TestCase):
    """The package the Publisher generated after the auto-stop change (2026-10-15: auto-stop 210 is its current value, it
    recommends a size cut) follows the lineage whose step the change applied."""

    def test_the_successor_is_linked_and_the_applied_lineage_keeps_realizing(self):
        balanced, observation = stored_run(BALANCED)
        successor = fixture_package("successor-after-autostop")
        self.assertEqual(successor["observed_change"]["previous_epoch_id"], balanced["warehouse"]["current_epoch"]["epoch_id"])
        for order in ([(balanced, observation), (successor, None)], [(successor, None), (balanced, observation)]):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                realize_all(order, out)
                earlier, later = rpkg_file(out, balanced), rpkg_file(out, successor)
            with self.subTest(first=order[0][0]["recommendation_package_id"]):
                self.assertEqual(earlier["source_recommendation"]["successor_package_id"], successor["recommendation_package_id"])
                # The successor prices nothing for its plan (a 6-day epoch): no material improvement, the lineage stays active.
                self.assertEqual(earlier["current_state"]["source_package_disposition"], "ACTIVE")
                self.assertEqual(earlier["attribution"]["attribution_claim_status"], "CLAIMABLE")
                self.assertEqual(earlier["attribution"]["recommendation_attributed_realized_savings"], "1069.73")
                # The successor started from the applied value: it claims nothing of it.
                self.assertEqual(later["application"]["state"], "NOT_APPLIED")
                self.assertEqual(later["current_state"]["source_package_disposition"], "ACTIVE")


class CanaryRollbackTests(unittest.TestCase):
    """wh-demo-040: the package of 2026-09-20 recommends the canary LARGE to MEDIUM (applied 2026-09-27); the package of
    2026-10-01 finds the canary's p95 guard breached and rolls it back to LARGE (applied 2026-10-03)."""

    @classmethod
    def setUpClass(cls):
        cls.canary, cls.rollback = fixture_package("canary-before-cut"), fixture_package("canary-rollback")
        cut = demo_observation(cls.canary)
        cut.update({"applied_at_utc": "2026-09-27T00:00:00Z", "observed_at_utc": "2026-09-30T00:00:00Z", "recorded_at_utc": "2026-09-30T00:00:00Z",
                    "window_start_utc": "2026-09-27T00:00:00Z", "window_end_utc": "2026-09-30T00:00:00Z", "elapsed_days": 3})
        back = demo_observation(cls.rollback)
        back.update({"applied_at_utc": "2026-10-03T00:00:00Z", "observed_at_utc": "2026-10-09T00:00:00Z", "recorded_at_utc": "2026-10-09T00:00:00Z",
                     "window_start_utc": "2026-10-03T00:00:00Z", "window_end_utc": "2026-10-09T00:00:00Z", "elapsed_days": 6})
        cls.cut, cls.back = cut, back

    def test_the_earlier_lineage_is_rolled_back_when_the_rollback_is_applied(self):
        self.assertEqual(self.rollback["observed_change"]["previous_epoch_id"], self.canary["warehouse"]["current_epoch"]["epoch_id"])
        outcomes = []
        for order in ([(self.canary, self.cut), (self.rollback, self.back)], [(self.rollback, self.back), (self.canary, self.cut)]):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                realize_all(order, out)
                earlier, later = rpkg_file(out, self.canary), rpkg_file(out, self.rollback)
                rollbacks = [e for e in load_ledger(ledger_path(out, earlier["realization_package_id"])) if e.event_type.value == "ROLLBACK_OBSERVED"]
                outcomes.append(final_files(out))
            with self.subTest(first=order[0][0]["recommendation_package_id"]):
                self.assertEqual(earlier["application"]["observed_applied_value"], "MEDIUM")
                self.assertEqual(earlier["application"]["state"], "ROLLED_BACK")
                self.assertEqual(earlier["current_state"]["realization_state"], "ROLLED_BACK")
                self.assertEqual(earlier["application"]["effective_to_utc"], "2026-10-03T00:00:00Z")
                self.assertEqual(earlier["source_recommendation"]["successor_package_id"], self.rollback["recommendation_package_id"])
                self.assertEqual(len(rollbacks), 1)
                self.assertEqual(rollbacks[0].effective_at_utc, "2026-10-03T00:00:00Z")
                self.assertEqual((rollbacks[0].payload["reason"], rollbacks[0].payload["rollback_recommendation_package_id"]),
                                 ("CANARY_ROLLBACK", self.rollback["recommendation_package_id"]))
                self.assertEqual(later["current_state"]["realization_state"], "SAFETY_ACTION_APPLIED")
        self.assertEqual(outcomes[0], outcomes[1])


class OtherLedgerErrorTests(unittest.TestCase):
    """Only another run's lock leaves a cross-lineage event for a later run; a ledger that cannot be written fails."""

    def test_a_locked_ledger_is_skipped_and_any_other_ledger_error_raises(self):
        from realizer.common.errors import LedgerError, LedgerLockedError
        from realizer.contracts.lineage import LineageEvent, LineageRecord
        from realizer.packager import lineage as packager_lineage
        own, other = e2e.stored_package(BALANCED), e2e.stored_package(AGGRESSIVE)
        own_rpkg, other_rpkg = make_rpkg_id(own["recommendation_package_id"]), make_rpkg_id(other["recommendation_package_id"])
        event = LineageEvent(realization_package_id=other_rpkg, event_type=rc.EventType.SUCCESSOR_PACKAGE_LINKED,
                             effective_at_utc="2026-10-01T12:00:00Z", recorded_at_utc="2026-10-01T12:00:00Z",
                             payload={"successor_package_id": own["recommendation_package_id"]}, application_episode_id=None)
        record = LineageRecord(realization_package_id=other_rpkg, recommendation_package_id=other["recommendation_package_id"], package=other, entries=())
        for error, raises in ((LedgerLockedError("ledger x is locked by another run"), False), (LedgerError("ledger x line 3 is not a valid ledger entry"), True)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                def locked(path, error=error):
                    raise error
                with mock.patch.object(packager_lineage, "ledger_lock", locked):
                    if raises:
                        with self.assertRaises(LedgerError):
                            packager_lineage.record_events(Path(tmp), own_rpkg, own, [event], [record])
                    else:
                        self.assertEqual(packager_lineage.record_events(Path(tmp), own_rpkg, own, [event], [record]), set())


class ReplacementGateTests(unittest.TestCase):
    def test_a_package_cannot_replace_itself(self):
        package, observation = stored_run(BALANCED)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InputError, "replacement_package_id .* is the package itself"):
                realize(package, {**observation, "replacement_package_id": package["recommendation_package_id"]}, Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])  # refused before anything is written


if __name__ == "__main__":
    unittest.main()
