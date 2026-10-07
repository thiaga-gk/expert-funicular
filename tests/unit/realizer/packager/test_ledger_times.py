"""Ledger times are compared as times, never as text (src/realizer/common/utils.py ``parse_utc``).

``utc_iso`` writes ``...T12:00:00Z`` without and ``...T12:00:00.500000Z`` with microseconds; ``.`` sorts before ``Z``, so
as text the later of the two sorts first. The ledger order, the checkpoint, the replay cut, the configuration-drift check
and the snapshot's rollback and drift precedence each read the parsed time.
"""
import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.helpers import canonical, fixture_package  # noqa: E402
from realizer.analyzer import analysis  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.utils import parse_utc  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.analysis import RealizationHistory  # noqa: E402
from realizer.packager import ledger, packager, snapshot  # noqa: E402
from realizer.realizer import build_context  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402

WHOLE = "2026-10-09T12:00:00Z"
LATER = "2026-10-09T12:00:00.500000Z"  # half a second later, but '.' < 'Z' as text


def entry(package, event_type, recorded, payload=None, effective=None):
    return ledger.make_ledger_entry(
        package=package, rpkg_id=rc.make_rpkg_id(package["recommendation_package_id"]), event_type=event_type,
        effective_at_utc=effective or recorded, recorded_at_utc=recorded, payload=payload or {"note": recorded},
    )


def stored(scenario):
    package = canonical.load_scenario(scenario)["package_doc"]
    path = next((canonical.EXPECTED / scenario / "realization" / "ledger").glob("*.jsonl"))
    return package, ledger.load_ledger(path)


class TimeOrderTests(unittest.TestCase):
    def test_parse_utc_orders_what_text_misorders(self):
        self.assertLess(LATER, WHOLE)  # the defect: as text the later time sorts first
        self.assertGreater(parse_utc(LATER), parse_utc(WHOLE))
        self.assertEqual(parse_utc("2026-10-09T14:00:00+02:00"), parse_utc(WHOLE))
        self.assertEqual(parse_utc("2026-10-09T12:00:00"), parse_utc(WHOLE))  # naive is UTC
        for bad in (None, "garbage"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_utc(bad)

    def test_load_ledger_puts_a_later_time_with_microseconds_last(self):
        package = fixture_package()
        later, whole = entry(package, rc.EventType.DRIFT_DETECTED, LATER), entry(package, rc.EventType.DRIFT_DETECTED, WHOLE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.jsonl"
            for e in (later, whole):  # the later one is appended first
                ledger.append_if_absent(path, e)
            loaded = ledger.load_ledger(path)
        self.assertEqual([e.recorded_at_utc for e in loaded], [WHOLE, LATER])
        self.assertEqual(ledger.latest_event(loaded, rc.EventType.DRIFT_DETECTED).recorded_at_utc, LATER)

    def test_checkpoint_latest_times_are_the_latest_times(self):
        package = fixture_package()
        entries = [entry(package, rc.EventType.DRIFT_DETECTED, WHOLE, effective=WHOLE),
                   entry(package, rc.EventType.ROLLBACK_OBSERVED, WHOLE, effective=LATER)]
        cp = ledger.ledger_checkpoint(entries)
        self.assertEqual(cp["latest_effective_at_utc"], LATER)
        cp = ledger.ledger_checkpoint([entry(package, rc.EventType.DRIFT_DETECTED, LATER), entry(package, rc.EventType.ROLLBACK_OBSERVED, WHOLE)])
        self.assertEqual(cp["latest_recorded_at_utc"], LATER)

    def test_config_drift_reads_a_later_observation_with_microseconds_as_later(self):
        package = fixture_package()
        previous = entry(package, rc.EventType.APPLICATION_OBSERVED, WHOLE, payload={"observed_config_hash": "SHA256:old"})
        history = RealizationHistory(entries=(previous,), previous_application=previous, prior_measurement=None)
        self.assertTrue(analysis.config_drift(history, "SHA256:new", LATER))
        self.assertFalse(analysis.config_drift(history, "SHA256:new", WHOLE))  # not newer than the record

    def test_rollback_and_drift_recorded_after_the_application_win_by_time(self):
        package, entries = stored("GR-003")  # an exact application that realized
        app = next(e for e in entries if e.event_type == rc.EventType.APPLICATION_OBSERVED)
        base = [dataclasses.replace(e, recorded_at_utc=WHOLE) if e.recorded_at_utc == app.recorded_at_utc else e for e in entries]
        for event_type, state in ((rc.EventType.ROLLBACK_OBSERVED, "ROLLED_BACK"), (rc.EventType.DRIFT_DETECTED, "DRIFTED")):
            with self.subTest(event=event_type.value):
                later = entry(package, event_type, LATER, payload={"state": "DETECTED"})
                result = snapshot.project_snapshot(package, base + [later])
                self.assertEqual(result.application["state"], state)

    def test_replay_cut_keeps_an_entry_recorded_before_a_cut_with_microseconds(self):
        package = fixture_package()
        observation = {**demo_observation(package), "observed_at_utc": WHOLE, "recorded_at_utc": WHOLE}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            from realizer.realizer import realize
            realize(copy.deepcopy(package), observation, out)
            full = ledger.load_ledger(ledger.ledger_path(out, rc.make_rpkg_id(package["recommendation_package_id"])))
            cut = realize(copy.deepcopy(package), None, out, replay_through=LATER)
        self.assertEqual(cut.history["entry_count"], len(full))
        self.assertIn(WHOLE, {e.recorded_at_utc for e in full})


if __name__ == "__main__":
    unittest.main()
