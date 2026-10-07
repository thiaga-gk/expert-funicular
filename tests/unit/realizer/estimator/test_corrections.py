"""Billing restatements (src/realizer/estimator/economics.py ``correction_facts``).

A window is identified by its application episode and its start and end, never by the measurement id (which hashes the
evidence, so a restated window gets a new one). When the window was measured before and the economics differ, the run
records the new measurement and a correction naming the earlier measurement of that window, whether or not the
observation carries a correction object (a supplied one keeps its key, type, version and reason). Identical economics for
the same window is a duplicate, and reprocessing a restatement records nothing new (one correction per key and version).
A repeated run does not rewrite a file whose content is unchanged.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.helpers import correction, fixture_package, ledger_lines  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.packager import ledger  # noqa: E402
from realizer.realizer import realize  # noqa: E402

MEASURED, CORRECTED = "ECONOMIC_MEASUREMENT_RECORDED", "BILLING_CORRECTION_RECORDED"


def later(observation, day, **economics):
    """*observation* collected again on *day* (October 2026): same window, the given costs restated."""
    at = f"2026-10-{day:02d}T12:00:00Z"
    return {**copy.deepcopy(observation), "observed_at_utc": at, "recorded_at_utc": at,
            "economics": {**observation["economics"], **economics}}


def window(observation, start_day, end_day):
    return {**copy.deepcopy(observation), "window_start_utc": f"2026-10-{start_day:02d}T12:00:00Z", "window_end_utc": f"2026-10-{end_day:02d}T12:00:00Z"}


class RestatementTests(unittest.TestCase):
    def setUp(self):
        self.package = fixture_package()
        self.base = demo_observation(self.package)  # window Oct 2 - Oct 9, observed new cost 7386.88 against 10000.00
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_all(self, *observations):
        result = None
        for observation in observations:
            result = realize(copy.deepcopy(self.package), copy.deepcopy(observation), self.out)
        return result, ledger_lines(self.out)

    def test_a_silent_restatement_of_the_same_window_is_a_correction_of_its_measurement(self):
        result, lines = self.run_all(self.base, later(self.base, 10, observed_new_cost="9200.00"))
        first, second = [x for x in lines if x["event_type"] == MEASURED]
        (corr,) = [x for x in lines if x["event_type"] == CORRECTED]
        self.assertEqual(corr["corrects_entry_id"], first["ledger_entry_id"])
        self.assertEqual(corr["measurement_id"], second["measurement_id"])
        self.assertEqual({k: corr["payload"][k] for k in ("correction_key", "type", "version", "measurement_id", "corrects_entry_id")},
                         {"correction_key": second["measurement_id"], "type": "RESTATEMENT", "version": None,
                          "measurement_id": second["measurement_id"], "corrects_entry_id": first["ledger_entry_id"]})
        self.assertEqual(second["payload"]["correction_status"], "RESTATEMENT")
        self.assertEqual((first["payload"]["economics"]["successful_realized_savings"], result.economics["successful_realized_savings"]),
                         ("2613.12", "800.00"))
        self.assertEqual(result.history["correction_count"], 1)

    def test_reprocessing_a_restatement_records_nothing_new(self):
        restated = later(self.base, 10, observed_new_cost="9200.00")
        _, before = self.run_all(self.base, restated)
        _, after = self.run_all(restated, restated)
        self.assertEqual(after, before)
        self.assertEqual(sum(x["event_type"] == CORRECTED for x in after), 1)

    def test_a_supplied_restatement_of_a_reprocessed_observation_adds_one_correction(self):
        restated = {**later(self.base, 10, observed_new_cost="9200.00"), "correction": correction(key="oct", reason="late billing")}
        result, lines = self.run_all(self.base, restated, restated, restated)
        (corr,) = [x for x in lines if x["event_type"] == CORRECTED]
        first = next(x for x in lines if x["event_type"] == MEASURED)
        self.assertEqual({k: corr["payload"][k] for k in ("correction_key", "type", "version", "reason", "corrects_entry_id")},
                         {"correction_key": "oct", "type": "RESTATEMENT", "version": 2, "reason": "late billing",
                          "corrects_entry_id": first["ledger_entry_id"]})
        self.assertEqual(result.history["correction_count"], 1)

    def test_the_correction_names_the_measurement_of_that_window_not_the_latest(self):
        other = later(window(self.base, 3, 10), 10)  # another window measured after the first
        restated = {**later(self.base, 11, observed_new_cost="9200.00"), "correction": correction(key="w1")}
        _, lines = self.run_all(self.base, other, restated)
        measured = [x for x in lines if x["event_type"] == MEASURED]
        self.assertEqual(len(measured), 3)
        (corr,) = [x for x in lines if x["event_type"] == CORRECTED]
        self.assertEqual(corr["corrects_entry_id"], measured[0]["ledger_entry_id"])  # the Oct 2 - Oct 9 window, not Oct 3 - Oct 10

    def test_a_further_restatement_corrects_the_latest_measurement_of_the_window(self):
        v2 = {**later(self.base, 10, observed_new_cost="9200.00"), "correction": correction(key="oct", version=2)}
        v3 = {**later(self.base, 11, observed_new_cost="9300.00"), "correction": correction(key="oct", version=3)}
        _, lines = self.run_all(self.base, v2, v3)
        measured = [x for x in lines if x["event_type"] == MEASURED]
        corrections = [x for x in lines if x["event_type"] == CORRECTED]
        self.assertEqual([c["corrects_entry_id"] for c in corrections], [measured[0]["ledger_entry_id"], measured[1]["ledger_entry_id"]])
        self.assertEqual([c["payload"]["version"] for c in corrections], [2, 3])

    def test_identical_economics_for_the_same_window_is_a_duplicate(self):
        _, lines = self.run_all(self.base, later(self.base, 10))
        self.assertEqual(sum(x["event_type"] == CORRECTED for x in lines), 0)
        self.assertEqual(len({x["measurement_id"] for x in lines if x["event_type"] == MEASURED}), 1)

    def test_a_correction_object_with_the_same_economics_records_nothing_new(self):
        """Its version alone changes the measurement id; the window and its facts are the same, so nothing is recorded."""
        _, before = self.run_all(self.base)
        _, after = self.run_all({**later(self.base, 10), "correction": correction(key="oct")})
        self.assertEqual([x["event_type"] for x in after if x["event_type"] in (MEASURED, CORRECTED)], [MEASURED])
        self.assertEqual(len([x for x in after if x["event_type"] == MEASURED]), len([x for x in before if x["event_type"] == MEASURED]))

    def test_a_new_window_without_a_correction_object_is_no_correction(self):
        _, lines = self.run_all(self.base, later(window(self.base, 3, 10), 10, observed_new_cost="9200.00"))
        self.assertEqual(sum(x["event_type"] == MEASURED for x in lines), 2)
        self.assertEqual(sum(x["event_type"] == CORRECTED for x in lines), 0)


class UnchangedFileTests(unittest.TestCase):
    def test_a_repeated_run_does_not_rewrite_unchanged_files(self):
        package = fixture_package()
        observation = demo_observation(package)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            realize(copy.deepcopy(package), copy.deepcopy(observation), out)
            files = sorted(p for p in (out / "realization").rglob("*.json"))
            self.assertTrue(any("evidence" in p.parts for p in files) and any("regeneration" in p.parts for p in files))
            before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in files}
            realize(copy.deepcopy(package), copy.deepcopy(observation), out)
            after = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in files}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
