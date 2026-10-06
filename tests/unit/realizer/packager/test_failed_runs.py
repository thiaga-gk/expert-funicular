"""Failed runs through the extracted Packager: same exception, same half-written ledger as the oracle.

The capabilities decide before the Packager records anything, so a failure one of them kept (``halt``) must still
surface after exactly the ledger entries and files the single-file engine had written when it raised. This file walks
the commit in order and fails the run at each position: before the lineage entry (a corrupt ledger), right after it (the
Analyzer), after the evidence file (matching), after each lifecycle entry, after the lifecycle (readiness), after each
validation payload, the counterfactual, the measurement and the correction (the Estimator, the Reviewer, the entry
itself) and the downstream regeneration, and, after the whole commit, a replay cut that is not a time or leaves nothing
to project. Each case runs the oracle and the modular engine into separate directories and compares the outcomes and the
whole output tree (files and directories); the last event it leaves is pinned. The mutation checks prove the comparison
would notice a Packager that raised a failure before recording the events decided ahead of it.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import Opaque, Unprintable, Untestable, engine, events, files, fixture_package, oengine, outcome, run_sequence, tree  # noqa: E402
from realizer.packager import ledger  # noqa: E402

CREATED = ["REALIZATION_CREATED"]
LIFECYCLE = CREATED + ["APPLICATION_OBSERVED", "INTENT_RECORDED"]
VALIDATED = LIFECYCLE + ["VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED"]


class CommitPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)
        cls.source_config = oengine.package_config(cls.pkg)

    def later(self, day, **patch):
        stamp = f"2026-10-{day:02d}T12:00:00Z"
        return {**copy.deepcopy(self.base), "observed_at_utc": stamp, "recorded_at_utc": stamp, **patch}

    def compare(self, observations, policy=None, package=None, **kwargs):
        package = self.pkg if package is None else package
        new = run_sequence(engine, package, observations, policy)
        old = run_sequence(oengine, package, observations, policy)
        self.assertEqual(new[0], old[0], "outcomes")
        self.assertEqual(new[1], old[1], "output tree")
        return old

    def assert_halts_after(self, observations, last, kind, **kwargs):
        outcomes, tree_ = self.compare(observations, **kwargs)
        self.assertEqual(outcomes[-1][0], kind, outcomes)
        self.assertEqual(events(tree_)[-1], last, events(tree_))
        return tree_

    def test_corrupt_ledger_fails_before_anything_is_written(self):
        results = []
        for module in (engine, oengine):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                context = engine.build_context(engine.RealizationRequest(package=copy.deepcopy(self.pkg), observation=None, output_dir=out))
                path = ledger.ledger_path(out, context.realization_package_id)
                path.parent.mkdir(parents=True)
                path.write_text("not json\n", encoding="utf-8")
                results.append((outcome(module.realize, copy.deepcopy(self.pkg), copy.deepcopy(self.base), out), tree(out)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0][0], "JSONDecodeError")
        self.assertEqual(files(results[0][1], "evidence"), [])

    def test_analyzer_failure_leaves_the_lineage_entry_only(self):
        tree_ = self.assert_halts_after([{**self.base, "config": 5}], "REALIZATION_CREATED", "TypeError")
        self.assertEqual(files(tree_, "evidence"), [])

    def test_matching_failure_leaves_the_lineage_entry_and_the_evidence_file(self):
        package = copy.deepcopy(self.pkg)
        package["recommended_plan"] = 5
        tree_ = self.assert_halts_after([self.base], "REALIZATION_CREATED", "AttributeError", package=package)
        self.assertEqual(len(files(tree_, "evidence")), 1)

    def test_lifecycle_failures_keep_every_entry_decided_before_them(self):
        cases = {
            "after drift": ([self.base, self.later(10, config={**self.base["config"], "warehouse_size": "SMALL"}, effective_time_quality=Unprintable())],
                            "DRIFT_DETECTED", "ValueError"),
            "after intent": ([{**self.base, "rollback_requested": Untestable()}], "INTENT_RECORDED", "ValueError"),
            "after rollback requested": ([{**self.base, "rollback_requested": True, "rollback_observed": Untestable()}], "ROLLBACK_REQUESTED", "ValueError"),
            "after rollback observed": ([{**self.base, "rollback_observed": True, "replacement_package_id": Untestable()}], "ROLLBACK_OBSERVED", "ValueError"),
        }
        for label, (observations, last, kind) in cases.items():
            with self.subTest(case=label):
                self.assert_halts_after(observations, last, kind)

    def test_readiness_failure_leaves_the_whole_lifecycle(self):
        tree_ = self.assert_halts_after([{**self.base, "elapsed_days": "abc"}], "INTENT_RECORDED", "ValueError")
        self.assertEqual(events(tree_), LIFECYCLE)
        self.assertEqual(files(tree_, "regeneration"), [])

    def test_review_failures_leave_the_validation_payloads_decided_before_them(self):
        self.assert_halts_after([{**self.base, "performance": 5}], "VALIDATION_STARTED", "AttributeError")
        self.assert_halts_after([{**self.base, "window": {"start_utc": "x"}, "window_start_utc": Opaque()}], "INTENT_RECORDED", "TypeError")

    def test_estimate_failures_leave_the_facts_decided_before_them(self):
        econ = self.base["economics"]
        cases = {
            "counterfactual": ({**self.base, "economics": {**econ, "counterfactual_old_cost": "abc"}}, "VALIDATION_COMPLETED", "InvalidOperation"),
            "economics": ({**self.base, "economics": {**econ, "observed_new_cost": "abc"}}, "COUNTERFACTUAL_RECORDED", "InvalidOperation"),
            "attribution": ({**self.base, "overlap_count": "abc"}, "COUNTERFACTUAL_RECORDED", "ValueError"),
            "correction entry": ({**self.base, "correction": {"corrects_entry_id": "bogus"}}, "ECONOMIC_MEASUREMENT_RECORDED", "ValueError"),
            "correction payload": ({**self.base, "correction": {"reason": Opaque()}}, "ECONOMIC_MEASUREMENT_RECORDED", "TypeError"),
        }
        for label, (observation, last, kind) in cases.items():
            with self.subTest(case=label):
                tree_ = self.assert_halts_after([observation], last, kind)
                self.assertEqual(files(tree_, "regeneration"), [])

    def test_downstream_regeneration_failure_leaves_the_measurement_and_the_correction(self):
        bad_window = {"remeasure_days_after_invalidating_change": "abc"}
        self.assert_halts_after([self.base], "ECONOMIC_MEASUREMENT_RECORDED", "ValueError", policy=bad_window)
        tree_ = self.assert_halts_after([{**self.base, "correction": {"type": "RESTATEMENT", "key": "c1"}}], "BILLING_CORRECTION_RECORDED", "ValueError", policy=bad_window)
        self.assertEqual(files(tree_, "regeneration"), [])

    def test_replay_cut_fails_after_the_whole_commit(self):
        for cut, kind in (("garbage", None), ("1999-01-01T00:00:00Z", "ValueError")):
            with self.subTest(cut=cut):
                results = []
                for module in (engine, oengine):
                    with tempfile.TemporaryDirectory() as tmp:
                        out = Path(tmp)
                        results.append((outcome(module.realize, copy.deepcopy(self.pkg), copy.deepcopy(self.base), out, replay_through=cut), tree(out)))
                self.assertEqual(results[0], results[1])
                (result, tree_) = results[0]
                self.assertNotEqual(result[0], "ok")
                if kind:
                    self.assertEqual(result[0], kind)
                self.assertEqual(events(tree_)[-1], "REGENERATION_REQUESTED", "the downstream regeneration was recorded too")
                self.assertEqual(len(files(tree_, "regeneration")), 1)
                self.assertEqual(files(tree_, "packages") + files(tree_, "snapshots"), [])

    def test_a_failed_run_is_followed_by_the_same_recovery(self):
        observations = [{**self.base, "overlap_count": "abc"}, self.base, self.later(10), self.later(10, correction={"version": 2})]
        outcomes, _ = self.compare(observations)
        self.assertNotEqual(outcomes[0][0], "ok")
        self.assertEqual([o[0] for o in outcomes[1:]], ["ok", "ok", "ok"])


class PackagerMutationTests(unittest.TestCase):
    """Mutation checks: a Packager that raised a kept failure before recording the events decided ahead of it leaves another ledger."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

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
                old = run_sequence(oengine, self.pkg, observations)
                with mock.patch.object(ledger, "commit_events", eager):
                    new = run_sequence(engine, self.pkg, observations)
                self.assertEqual(new[0], old[0], "the exception itself is the same")
                self.assertNotEqual(new[1], old[1], "the ledger it leaves is not the oracle's")
                self.assertEqual(run_sequence(engine, self.pkg, observations), old)

    def test_forcing_the_downstream_regeneration_early_is_caught(self):
        original = ledger.commit_events

        def early(context, reviewed):
            rec = reviewed.reconciliation
            if rec.upstream_regeneration is not None:
                rec.upstream_regeneration.get()
            return original(context, reviewed)

        observations = [{**self.base}]
        policy = {"remeasure_days_after_invalidating_change": "abc"}
        old = run_sequence(oengine, self.pkg, observations, policy)
        with mock.patch.object(ledger, "commit_events", early):
            new = run_sequence(engine, self.pkg, observations, policy)
        self.assertNotEqual(new[1], old[1])
        self.assertEqual(run_sequence(engine, self.pkg, observations, policy), old)


if __name__ == "__main__":
    unittest.main()
