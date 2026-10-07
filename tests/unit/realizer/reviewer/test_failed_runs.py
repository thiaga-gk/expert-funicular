"""Failed runs through the extracted Reviewer: the exception and the half-written ledger are stored.

The Reviewer assembles the validation episode before the engine records it, so a failure while building it must still
surface where the single-file release raised it: before any validation entry for the episode id (a policy whose
snapshot cannot be hashed, a window value the id cannot hash), and after VALIDATION_READY and VALIDATION_STARTED for the
performance verdict. The episode is recorded before the estimate, so its failure wins over a later one of the
Estimator. Each case compares the outcomes and the whole output tree (compacted to the ledger events and a digest per
file) with the stored expectation; the mutation check proves the comparison would notice a Reviewer that raised at once.

Nothing the started payload reads can fail after the ready entry: every value it records is part of the episode id.
"""
import unittest
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, compact_tree, events, fixture_package, run_sequence  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.reviewer import reviewer  # noqa: E402

LIFECYCLE = ["REALIZATION_CREATED", "APPLICATION_OBSERVED", "INTENT_RECORDED"]


class ReviewerFailedRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def compare(self, label, observations, policy=None):
        """The observation gate is open: the loader refuses these observations before anything is written (see
        ``tests/unit/realizer/config``). Run *observations*, check the outcomes and the output tree against the stored expectation *label*, and return them."""
        outcomes, tree = run_sequence(self.pkg, observations, policy, observation_gate=False)
        expect.check(self, label, (outcomes, compact_tree(tree)))
        return outcomes, tree

    def test_episode_id_failure_records_no_validation_entry(self):
        cases = {
            "policy snapshot": (self.base, {"realized_ratio_min": "NaN"}, "ValueError"),
            "window value": ({**self.base, "window": {"start_utc": "x"}, "window_start_utc": Opaque()}, None, "TypeError"),
            "performance value": ({**self.base, "performance": {"reliability_pass": Opaque()}}, None, "TypeError"),
        }
        for label, (observation, policy, kind) in cases.items():
            with self.subTest(case=label):
                outcomes, tree = self.compare(f"episode id: {label}", [observation], policy)
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
                outcomes, tree = self.compare(f"performance: {label}", [{**self.base, **patch}])
                self.assertEqual(outcomes[0][0], kind, outcomes)
                self.assertEqual(events(tree), LIFECYCLE + ["VALIDATION_READY", "VALIDATION_STARTED"])

    def test_unreached_performance_input_does_not_fail(self):
        outcomes, _ = self.compare("unreached performance input", [{**self.base, "elapsed_days": 1, "performance": 5}])
        self.assertEqual(outcomes[0][0], "ok")


class ReviewerMutationTests(unittest.TestCase):
    def test_raising_before_recording_changes_the_failed_run_tree(self):
        package = fixture_package()
        base = demo_observation(package)
        original = reviewer.run

        def eager(*args):
            result = original(*args)
            if result.halt is not None:
                raise result.halt
            return result

        observations = [{**base, "performance": {"normalized_p95_runtime_regression_pct": "abc"}}]
        expected = run_sequence(package, observations, observation_gate=False)
        with mock.patch.object(reviewer, "run", eager):
            mutated = run_sequence(package, observations, observation_gate=False)
        self.assertEqual(mutated[0], expected[0], "the exception itself is the same")
        self.assertNotEqual(mutated[1], expected[1], "but the ledger it leaves is not")
        self.assertEqual(run_sequence(package, observations, observation_gate=False), expected)


if __name__ == "__main__":
    unittest.main()
