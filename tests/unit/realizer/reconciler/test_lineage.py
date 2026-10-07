"""Reconciler decisions across lineages (src/realizer/reconciler/lineage.py) and the PolicyDiff
(src/realizer/reconciler/policy_diff.py), as pure functions.

The lineages are real Publisher packages (the stored package of ``ge07-profile-balanced-exact`` and the fixtures); where a
rule needs a package the demo fleet does not produce (a successor that is a material improvement, or whose change applied
none of the earlier plan), one field of a real package is changed in memory: these decisions read packages, they never
check them (the loader's gate does, before a run).
"""
import copy
import json
import unittest
from decimal import Decimal

from tests.unit.realizer.helpers import canonical, fixture_package  # noqa: F401  (canonical: src/realizer, never a shadowing copy)
from tests.golden.realizer import e2e
from realizer.contracts import realization as rc
from realizer.contracts.realization import make_rpkg_id
from realizer.governor.policy import RealizerPolicy
from realizer.reconciler import lineage
from realizer.reconciler.policy_diff import changed_settings

BALANCED = json.loads((e2e.case_dir("ge07-profile-balanced-exact") / e2e.PACKAGE_FILE).read_text(encoding="utf-8"))
AGGRESSIVE = json.loads((e2e.case_dir("ge06-profile-aggressive-modified") / e2e.PACKAGE_FILE).read_text(encoding="utf-8"))
CHANGE_AT = "2026-10-08T14:32:00Z"


def claim(conformance="EXACT", value=210, at=CHANGE_AT, episode="APP-" + "A" * 24, recorded="2026-11-07T12:00:00Z"):
    payload = {"authority_eligible": True, "implementation_conformance": conformance, "knob": "auto_stop_minutes", "observed_value": value,
               "source_value": 365, "effective_from_utc": at, "observed_config": {"auto_stop_minutes": value}, "step_role": "SAVINGS",
               "application_scope": "CANDIDATE"}
    return (rc.EventType.APPLICATION_OBSERVED.value if conformance == "EXACT" else rc.EventType.MODIFIED_APPLICATION_OBSERVED.value, payload, episode, recorded)


def view(package, *claims, generated=None, ends=()):
    """A lineage of *claims* the run is about to record; each time in *ends* a drift the run records then."""
    package = copy.deepcopy(package)
    if generated:
        package["provenance"]["generated_at"] = generated
    closers = [(rc.EventType.DRIFT_DETECTED, {"effective_from_utc": end}, None) for end in ends]
    return lineage.lineage_of(make_rpkg_id(package["recommendation_package_id"]), package, (), claims, closers)


def owners_named(events):
    return {e.realization_package_id: e.payload["owner_recommendation_package_id"] for e in events if e.event_type == rc.EventType.APPLICATION_CLAIMED_BY_OTHER_LINEAGE}


class OwnershipTests(unittest.TestCase):
    def test_exact_before_modified_at_the_same_generation_time(self):
        balanced, aggressive = view(BALANCED, claim("EXACT")), view(AGGRESSIVE, claim("MODIFIED"))
        for order in ([balanced, aggressive], [aggressive, balanced]):
            self.assertEqual(owners_named(lineage.ownership(order)), {aggressive.realization_package_id: BALANCED["recommendation_package_id"]})

    def test_the_most_recently_generated_package_before_the_change_wins_over_conformance(self):
        older_exact = view(BALANCED, claim("EXACT"))
        newer_modified = view(AGGRESSIVE, claim("MODIFIED"), generated="2026-10-05T12:00:00Z")
        self.assertEqual(owners_named(lineage.ownership([older_exact, newer_modified])),
                         {older_exact.realization_package_id: AGGRESSIVE["recommendation_package_id"]})

    def test_the_lowest_package_id_breaks_a_full_tie(self):
        first, second = view(BALANCED, claim("EXACT")), view(AGGRESSIVE, claim("EXACT"))
        low, high = sorted([first, second], key=lambda x: x.recommendation_package_id)
        self.assertEqual(owners_named(lineage.ownership([first, second])), {high.realization_package_id: low.recommendation_package_id})

    def test_a_package_generated_after_the_change_never_owns_it(self):
        before = view(BALANCED, claim("MODIFIED"))
        after = view(AGGRESSIVE, claim("EXACT"), generated="2026-10-09T00:00:00Z")
        self.assertEqual(owners_named(lineage.ownership([before, after])), {after.realization_package_id: BALANCED["recommendation_package_id"]})

    def test_different_values_or_disjoint_intervals_are_different_changes(self):
        self.assertEqual(lineage.ownership([view(BALANCED, claim(value=210)), view(AGGRESSIVE, claim(value=15))]), [])
        ended = view(BALANCED, claim(at="2026-10-02T00:00:00Z"), ends=["2026-10-05T00:00:00Z"])
        self.assertEqual(lineage.ownership([ended, view(AGGRESSIVE, claim(at=CHANGE_AT))]), [])

    def test_the_event_names_the_owner_and_takes_its_times_from_the_two_applications(self):
        (event,) = lineage.ownership([view(BALANCED, claim("EXACT", recorded="2026-11-08T00:00:00Z")),
                                      view(AGGRESSIVE, claim("MODIFIED", recorded="2026-11-07T12:00:00Z"))])
        self.assertEqual((event.effective_at_utc, event.recorded_at_utc), (CHANGE_AT, "2026-11-08T00:00:00Z"))
        self.assertEqual(event.payload["owner_realization_package_id"], make_rpkg_id(BALANCED["recommendation_package_id"]))
        self.assertEqual(event.payload["rule"], "MOST_RECENT_PACKAGE_BEFORE_CHANGE")


class EpisodeEndTests(unittest.TestCase):
    """A claim ends when its own episode closes: a rollback of a later plan step alone leaves the earlier step's claim
    open (plan progress), a drift ends every episode still open."""

    def entries(self, *rows):
        package = copy.deepcopy(BALANCED)
        rpkg = make_rpkg_id(package["recommendation_package_id"])
        from realizer.packager.ledger import make_ledger_entry
        return [make_ledger_entry(package=package, rpkg_id=rpkg, event_type=kind, effective_at_utc=at, recorded_at_utc=at,
                                  payload=payload, application_episode_id=episode) for kind, payload, episode, at in rows]

    def test_a_rollback_ends_only_the_episodes_it_lists(self):
        step1, step2 = "APP-" + "1" * 24, "APP-" + "2" * 24
        rows = self.entries(
            (rc.EventType.APPLICATION_OBSERVED, {"authority_eligible": True, "step": 1}, step1, "2026-10-08T00:00:00Z"),
            (rc.EventType.APPLICATION_OBSERVED, {"authority_eligible": True, "step": 2}, step2, "2026-10-22T09:00:00Z"),
            (rc.EventType.ROLLBACK_OBSERVED, {"effective_from_utc": "2026-10-29T09:00:00Z", "rolled_back_episode_ids": [step2]}, step2, "2026-10-29T09:00:00Z"))
        ends = lineage.episode_ends(rows)
        # Before: any drift or rollback ended every claim of the lineage, so the open auto-stop claim ended too.
        self.assertEqual(sorted(ends), [step2])
        self.assertEqual(ends[step2].isoformat(), "2026-10-29T09:00:00+00:00")

    def test_a_planned_drift_ends_every_open_episode_at_its_effective_time(self):
        step1, step2 = "APP-" + "1" * 24, "APP-" + "2" * 24
        rows = self.entries(
            (rc.EventType.APPLICATION_OBSERVED, {"authority_eligible": True, "step": 1}, step1, "2026-10-08T00:00:00Z"),
            (rc.EventType.APPLICATION_OBSERVED, {"authority_eligible": True, "step": 2}, step2, "2026-10-22T09:00:00Z"))
        ends = lineage.episode_ends(rows, [(rc.EventType.DRIFT_DETECTED, {"effective_from_utc": "2026-10-29T09:00:00Z"}, step2)])
        self.assertEqual({e: t.isoformat() for e, t in ends.items()}, {step1: "2026-10-29T09:00:00+00:00", step2: "2026-10-29T09:00:00+00:00"})


class SupersessionTests(unittest.TestCase):
    def successor(self, *, savings_factor=None, auto_stop=None):
        package = copy.deepcopy(fixture_package("successor-after-autostop"))
        if savings_factor is not None:
            base = lineage.expected_savings(BALANCED) * Decimal(str(savings_factor))
            scenario = next(s for s in package["scenarios"] if s["scenario_id"] == package["recommended_plan"]["scenario_id"])
            scenario["portfolio_economics"]["savings_365d"] = {"total": {"base": float(base)}}
        if auto_stop is not None:
            package["current_config"]["auto_stop_minutes"] = auto_stop
        return view(package)

    def reasons(self, events):
        return [(e.realization_package_id, e.payload["reason"]) for e in events if e.event_type == rc.EventType.SOURCE_PACKAGE_SUPERSEDED]

    def test_a_successor_is_a_replacement_only_when_it_is_a_material_improvement(self):
        earlier = view(BALANCED)
        policy = RealizerPolicy()
        self.assertEqual(lineage.expected_savings(BALANCED), Decimal("10476.47"))
        self.assertEqual(self.reasons(lineage.supersession([earlier, self.successor()], policy)), [])  # unpriced
        self.assertEqual(self.reasons(lineage.supersession([earlier, self.successor(savings_factor="1.04")], policy)), [])
        self.assertEqual(self.reasons(lineage.supersession([earlier, self.successor(savings_factor="1.06")], policy)),
                         [(earlier.realization_package_id, "MATERIAL_IMPROVEMENT")])

    def test_a_successor_whose_change_applied_none_of_the_plan_replaces_it(self):
        earlier = view(BALANCED)
        self.assertEqual(self.reasons(lineage.supersession([earlier, self.successor(auto_stop=365)], RealizerPolicy())),
                         [(earlier.realization_package_id, "CONFIGURATION_CHANGED")])

    def test_packages_generated_together_never_replace_each_other(self):
        self.assertEqual(lineage.supersession([view(BALANCED), view(AGGRESSIVE)], RealizerPolicy()), [])

    def test_the_newest_later_package_of_the_epoch_is_the_replacement(self):
        oct1, oct2, oct3 = view(BALANCED), view(fixture_package("autostop-daily-oct02")), view(fixture_package("autostop-daily-oct03"))
        events = [e for e in lineage.supersession([oct3, oct1, oct2], RealizerPolicy()) if e.event_type == rc.EventType.REPLACEMENT_PACKAGE_LINKED]
        self.assertEqual({e.realization_package_id: (e.payload["replacement_package_id"], e.recorded_at_utc) for e in events}, {
            oct1.realization_package_id: (oct3.recommendation_package_id, "2026-10-03T12:00:00Z"),
            oct2.realization_package_id: (oct3.recommendation_package_id, "2026-10-03T12:00:00Z"),
        })


class SuccessionAndRollbackTests(unittest.TestCase):
    def test_the_first_successor_is_linked(self):
        earlier, successor = view(BALANCED), view(fixture_package("successor-after-autostop"))
        (event,) = lineage.succession([successor, earlier])
        self.assertEqual((event.realization_package_id, event.payload["successor_package_id"], event.effective_at_utc),
                         (earlier.realization_package_id, successor.recommendation_package_id, "2026-10-15T12:00:00Z"))

    def test_a_rollback_needs_the_earlier_application_and_its_source_value(self):
        canary, rollback = fixture_package("canary-before-cut"), fixture_package("canary-rollback")
        cut = ("APPLICATION_OBSERVED", {"authority_eligible": True, "implementation_conformance": "EXACT", "knob": "warehouse_size",
               "observed_value": "MEDIUM", "source_value": "LARGE", "effective_from_utc": "2026-09-27T00:00:00Z", "observed_config": {},
               "step_role": "SAVINGS"}, "APP-" + "C" * 24, "2026-09-30T00:00:00Z")
        back = ("APPLICATION_OBSERVED", {"authority_eligible": True, "implementation_conformance": "EXACT", "knob": "warehouse_size",
                "observed_value": "LARGE", "source_value": "MEDIUM", "effective_from_utc": "2026-10-03T00:00:00Z",
                "observed_config": {"warehouse_size": "LARGE"}, "step_role": "SAFETY_ROLLBACK"}, "APP-" + "D" * 24, "2026-10-09T00:00:00Z")
        (event,) = lineage.rollbacks([view(canary, cut), view(rollback, back)])
        self.assertEqual((event.event_type, event.application_episode_id, event.effective_at_utc, event.recorded_at_utc),
                         (rc.EventType.ROLLBACK_OBSERVED, "APP-" + "C" * 24, "2026-10-03T00:00:00Z", "2026-10-09T00:00:00Z"))
        self.assertEqual(lineage.rollbacks([view(canary), view(rollback, back)]), [])  # the canary was never observed applied
        late = (cut[0], {**cut[1], "effective_from_utc": "2026-10-04T00:00:00Z"}, cut[2], cut[3])
        self.assertEqual(lineage.rollbacks([view(canary, late), view(rollback, back)]), [])  # applied after the rollback


class PolicyDiffTests(unittest.TestCase):
    def test_changed_settings_name_each_field_or_nothing_without_the_previous_policy(self):
        self.assertEqual(changed_settings({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}),
                         [{"field": "b", "previous": "2", "current": "3"}, {"field": "c", "previous": None, "current": "4"}])
        self.assertIsNone(changed_settings(None, {"a": "1"}))


if __name__ == "__main__":
    unittest.main()
