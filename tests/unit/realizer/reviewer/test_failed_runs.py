"""Failed runs through the extracted Reviewer: same exception, same half-written ledger as the oracle.

The Reviewer assembles the validation episode before the engine records it, so a failure while building it must still
surface where the single-file engine raised it: before any validation entry for the episode id (a policy whose
snapshot cannot be hashed, a window value the id cannot hash), and after VALIDATION_READY and VALIDATION_STARTED for the
performance verdict. The episode is recorded before the estimate, so its failure wins over a later one of the
Estimator. Each case compares the outcomes and the whole output tree with the oracle; the mutation check proves the
comparison would notice a Reviewer that raised at once.

Nothing the started payload reads can fail after the ready entry: every value it records is part of the episode id.
"""
import unittest
from unittest import mock

from tests.unit.realizer.parity import Opaque, engine, events, fixture_package, oengine, run_sequence  # noqa: E402
from realizer.reviewer import reviewer  # noqa: E402

LIFECYCLE = ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED"]


class ReviewerFailedRunParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = oengine.demo_observation(cls.pkg)

    def compare(self, observations, policy=None):
        new = run_sequence(engine, self.pkg, observations, policy)
        old = run_sequence(oengine, self.pkg, observations, policy)
        self.assertEqual(new[0], old[0], "outcomes")
        self.assertEqual(new[1], old[1], "output tree")
        return old

    def test_episode_id_failure_records_no_validation_entry(self):
        cases = {
            "policy snapshot": (self.base, {"realized_ratio_min": "NaN"}, "ValueError"),
            "window value": ({**self.base, "window": {"start_utc": "x"}, "window_start_utc": Opaque()}, None, "TypeError"),
            "performance value": ({**self.base, "performance": {"reliability_pass": Opaque()}}, None, "TypeError"),
        }
        for label, (observation, policy, kind) in cases.items():
            with self.subTest(case=label):
                outcomes, tree = self.compare([observation], policy)
                self.assertEqual(outcomes[0][0], kind, outcomes)
                self.assertEqual(events(tree), LIFECYCLE)

    def test_performance_failure_leaves_ready_and_started(self):
        cases = {
            "not a mapping": ({"performance": 5}, "AttributeError"),
            "p95 not a number": ({"performance": {"normalized_p95_runtime_regression_pct": "abc"}}, "InvalidOperation"),
            "p95 not finite": ({"performance": {"normalized_p95_runtime_regression_pct": "NaN"}}, "ValueError"),
            # the economics are malformed too, but the episode is recorded first
            "with malformed economics": ({"performance": 5, "economics": 5, "overlap_count": "abc"}, "AttributeError"),
        }
        for label, (patch, kind) in cases.items():
            with self.subTest(case=label):
                outcomes, tree = self.compare([{**self.base, **patch}])
                self.assertEqual(outcomes[0][0], kind, outcomes)
                self.assertEqual(events(tree), LIFECYCLE + ["VALIDATION_READY", "VALIDATION_STARTED"])

    def test_unreached_performance_input_does_not_fail(self):
        outcomes, _ = self.compare([{**self.base, "elapsed_days": 1, "performance": 5}])
        self.assertEqual(outcomes[0][0], "ok")


class ReviewerMutationTests(unittest.TestCase):
    def test_raising_before_recording_changes_the_failed_run_tree(self):
        package = fixture_package()
        base = oengine.demo_observation(package)
        original = reviewer.run

        def eager(*args):
            result = original(*args)
            if result.halt is not None:
                raise result.halt
            return result

        observations = [{**base, "performance": {"normalized_p95_runtime_regression_pct": "abc"}}]
        old = run_sequence(oengine, package, observations)
        with mock.patch.object(engine.reviewer, "run", eager):
            new = run_sequence(engine, package, observations)
        self.assertEqual(new[0], old[0], "the exception itself is the same")
        self.assertNotEqual(new[1], old[1], "but the ledger it leaves is not")
        self.assertEqual(run_sequence(engine, package, observations), old)


if __name__ == "__main__":
    unittest.main()
