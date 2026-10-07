"""Failed runs through the extracted Packager: the exception and the half-written ledger the single-file release left.

The capabilities decide before the Packager records anything, so a failure one of them kept (``halt``) must still
surface after exactly the ledger entries and files the single-file engine had written when it raised. This file walks
the commit in order and fails the run at each position: before the lineage entry (a corrupt ledger), right after it (the
Analyzer), after the evidence file (matching), after each lifecycle entry, after the lifecycle (readiness), after each
validation payload, the counterfactual, the measurement and the correction (the Estimator, the Reviewer, the entry
itself) and the downstream regeneration, and, after the whole commit, a replay cut that is not a time or leaves nothing
to project. Each case runs the engine into a fresh directory and compares the outcomes and the output tree (the ledger
events in append order, the directories and the digest of every file) with the stored expectation
(``tests/fixtures/realizer/expectations/``); the last event it leaves is pinned. The mutation checks prove the comparison
would notice a Packager that raised a failure before recording the events decided ahead of it.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, Unprintable, Untestable, compact_tree, correction, events, files, fixture_package, outcome, run_sequence, tree, without_remeasure_window  # noqa: E402
from realizer.analyzer.analysis import package_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.packager import ledger  # noqa: E402
from realizer.realizer import build_context, realize  # noqa: E402

CREATED = ["REALIZATION_CREATED"]
LIFECYCLE = CREATED + ["APPLICATION_OBSERVED", "INTENT_RECORDED"]
VALIDATED = LIFECYCLE + ["VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED"]


class CommitPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def compare(self, label, observations, policy=None, package=None):
        """Run *observations* into one directory and check the outcomes and the output tree against the stored expectation."""
        # a package passed in is a deliberately malformed one: the loader's gate would refuse it before any stage ran; the
        # observation gate is open too (it refuses these observations before anything is written, see tests/unit/realizer/config)
        outcomes, tree_ = run_sequence(self.pkg if package is None else package, observations, policy, gate=package is None, observation_gate=False)
        expect.check(self, label, (outcomes, compact_tree(tree_)))
        return outcomes, tree_

    def assert_halts_after(self, label, observations, last, kind, **kwargs):
        outcomes, tree_ = self.compare(label, observations, **kwargs)
        self.assertEqual(outcomes[-1][0], kind, outcomes)
        self.assertEqual(events(tree_)[-1], last, events(tree_))
        return tree_

    def test_corrupt_ledger_fails_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            context = build_context(RealizationRequest(package=copy.deepcopy(self.pkg), observation=None, output_dir=out))
            path = ledger.ledger_path(out, context.realization_package_id)
            path.parent.mkdir(parents=True)
            path.write_text("not json\n", encoding="utf-8")
            result, tree_ = outcome(realize, copy.deepcopy(self.pkg), copy.deepcopy(self.base), out), tree(out)
        self.assertEqual(result[0], "LedgerError")
        self.assertEqual(files(tree_, "evidence"), [])
        expect.check(self, "result", (result, tree_))  # the ledger holds the corrupt text, which compact_tree cannot parse

    def test_analyzer_failure_leaves_the_lineage_entry_only(self):
        tree_ = self.assert_halts_after("analyzer", [{**self.base, "config": 5}], "REALIZATION_CREATED", "TypeError")
        self.assertEqual(files(tree_, "evidence"), [])

    def test_matching_failure_leaves_the_lineage_entry_and_the_evidence_file(self):
        package = copy.deepcopy(self.pkg)
        package["recommended_plan"] = 5
        tree_ = self.assert_halts_after("matching", [self.base], "REALIZATION_CREATED", "AttributeError", package=package)
        self.assertEqual(len(files(tree_, "evidence")), 1)

    def test_lifecycle_failures_keep_every_entry_decided_before_them(self):
        cases = {
            "after drift": ([self.base, self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, effective_time_quality=Unprintable())],
                            "DRIFT_DETECTED", "ValueError"),
            "after intent": ([{**self.base, "rollback_requested": Untestable()}], "INTENT_RECORDED", "ValueError"),
            "after rollback requested": ([{**self.base, "rollback_requested": True, "replacement_package_id": Untestable()}], "ROLLBACK_REQUESTED", "ValueError"),
            # the size back at its source value rolls the application back
            "after rollback observed": ([self.base, self.later(10, config=package_config(self.pkg), replacement_package_id=Untestable())], "ROLLBACK_OBSERVED", "ValueError"),
        }
        for label, (observations, last, kind) in cases.items():
            with self.subTest(case=label):
                self.assert_halts_after(label, observations, last, kind)

    def test_readiness_failure_leaves_the_whole_lifecycle(self):
        tree_ = self.assert_halts_after("readiness", [{**self.base, "elapsed_days": "abc"}], "INTENT_RECORDED", "ValueError")
        self.assertEqual(events(tree_), LIFECYCLE)
        self.assertEqual(files(tree_, "regeneration"), [])

    def test_review_failures_leave_the_validation_payloads_decided_before_them(self):
        self.assert_halts_after("performance", [{**self.base, "performance": 5}], "VALIDATION_STARTED", "AttributeError")
        self.assert_halts_after("window", [{**self.base, "window": {"start_utc": "x"}, "window_start_utc": Opaque()}], "INTENT_RECORDED", "TypeError")

    def test_estimate_failures_leave_the_facts_decided_before_them(self):
        econ = self.base["economics"]
        cases = {
            "counterfactual": ({**self.base, "economics": {**econ, "counterfactual_old_cost": "abc"}}, "VALIDATION_COMPLETED", "InvalidOperation"),
            "economics": ({**self.base, "economics": {**econ, "observed_new_cost": "abc"}}, "COUNTERFACTUAL_RECORDED", "InvalidOperation"),
            "attribution": ({**self.base, "overlap_count": "abc"}, "COUNTERFACTUAL_RECORDED", "ValueError"),
            "correction entry": ({**self.base, "correction": correction(corrects_entry_id="bogus")}, "ECONOMIC_MEASUREMENT_RECORDED", "ValueError"),
            "correction payload": ({**self.base, "correction": correction(reason=Opaque())}, "ECONOMIC_MEASUREMENT_RECORDED", "TypeError"),
        }
        for label, (observation, last, kind) in cases.items():
            with self.subTest(case=label):
                tree_ = self.assert_halts_after(label, [observation], last, kind)
                self.assertEqual(files(tree_, "regeneration"), [])

    def test_downstream_regeneration_failure_leaves_the_measurement_and_the_correction(self):
        # the policy window is read only when the plan's REMEASURE step states none
        bad_window, plan = {"remeasure_days_after_invalidating_change": "abc"}, without_remeasure_window(self.pkg)
        self.assert_halts_after("measurement", [self.base], "ECONOMIC_MEASUREMENT_RECORDED", "ValueError", policy=bad_window, package=plan)
        tree_ = self.assert_halts_after("correction", [{**self.base, "correction": correction()}], "BILLING_CORRECTION_RECORDED", "ValueError", policy=bad_window, package=plan)
        self.assertEqual(files(tree_, "regeneration"), [])

    def test_replay_cut_is_refused_before_anything_is_written(self):
        """A cut that is not a time, or is before the package was generated, is an input refusal (``InputError``): the run
        stops in ``build_context``, so no ledger entry, regeneration request, package or snapshot is written."""
        for cut, message in (("garbage", "not an ISO-8601 time"), ("1999-01-01T00:00:00Z", "before the package was generated")):
            with self.subTest(cut=cut):
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp)
                    result, tree_ = outcome(realize, copy.deepcopy(self.pkg), copy.deepcopy(self.base), out, replay_through=cut), tree(out)
                self.assertEqual(result[0], "InputError")
                self.assertIn(message, result[1])
                self.assertEqual(tree_, {}, "nothing is written")
                expect.check(self, cut, (result, compact_tree(tree_)))

    def test_a_failed_run_is_followed_by_the_same_recovery(self):
        observations = [{**self.base, "overlap_count": "abc"}, self.base, self.later(10), self.later(10, correction={"version": 2})]
        outcomes, _ = self.compare("sequence", observations)
        self.assertNotEqual(outcomes[0][0], "ok")
        self.assertEqual([o[0] for o in outcomes[1:]], ["ok", "ok", "ok"])


class PackagerMutationTests(unittest.TestCase):
    """Mutation checks: a Packager that raised a kept failure before recording the events decided ahead of it leaves another ledger."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def test_raising_a_halt_before_recording_changes_the_failed_run_tree(self):
        original = ledger.commit_events

        def eager(context, reviewed):
            for halt in (reviewed.reconciliation.halt, reviewed.halt, reviewed.estimate.halt):
                if halt is not None:
                    raise halt
            return original(context, reviewed)

        econ = self.base["economics"]
        cases = [
            [{**self.base, "elapsed_days": "abc"}],                               # reconciliation
            [{**self.base, "performance": 5}],                                    # review
            [{**self.base, "economics": {**econ, "observed_new_cost": "abc"}}],   # estimate
        ]
        for observations in cases:
            with self.subTest(observations=observations):
                expected = run_sequence(self.pkg, observations, observation_gate=False)
                with mock.patch.object(ledger, "commit_events", eager):
                    mutant = run_sequence(self.pkg, observations, observation_gate=False)
                self.assertEqual(mutant[0], expected[0], "the exception itself is the same")
                self.assertNotEqual(mutant[1], expected[1], "the ledger it leaves is not the unmutated engine's")
                self.assertEqual(run_sequence(self.pkg, observations, observation_gate=False), expected)

    def test_forcing_the_downstream_regeneration_early_is_caught(self):
        original = ledger.commit_events

        def early(context, reviewed):
            rec = reviewed.reconciliation
            if rec.upstream_regeneration is not None:
                rec.upstream_regeneration.get()
            return original(context, reviewed)

        observations = [{**self.base}]
        policy = {"remeasure_days_after_invalidating_change": "abc"}
        plan = without_remeasure_window(self.pkg)   # the policy window is read only when the plan states none
        expected = run_sequence(plan, observations, policy, observation_gate=False)
        with mock.patch.object(ledger, "commit_events", early):
            mutant = run_sequence(plan, observations, policy, observation_gate=False)
        self.assertNotEqual(mutant[1], expected[1])
        self.assertEqual(run_sequence(plan, observations, policy, observation_gate=False), expected)


if __name__ == "__main__":
    unittest.main()
