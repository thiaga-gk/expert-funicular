"""The ledger lock and the quarantine of a truncated last line (src/realizer/packager/ledger.py ``ledger_lock``).

A realize run holds an advisory lock on its package's ledger from before the lineage is recorded until the Reporter has
written its files; a second run on the same ledger waits a short, bounded time and then refuses ("locked by another
run"). Under the lock, one unterminated trailing line that is not a valid entry (a write cut short) is moved to the
quarantine file next to the ledger and the ledger is truncated to its last newline, so the lineage continues; a
malformed line that is newline-terminated, or anywhere else, still fails closed. A read without the lock ignores such a
fragment and never changes the file.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.helpers import fixture_package, outcome  # noqa: E402
from tests.unit.realizer.packager.test_ledger import capture_logs  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.errors import LedgerError  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.packager import ledger  # noqa: E402
from realizer.reporter import reporter  # noqa: E402
from realizer.realizer import realize  # noqa: E402


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.package = fixture_package()
        self.observation = demo_observation(self.package)
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.path = ledger.ledger_path(self.out, rc.make_rpkg_id(self.package["recommendation_package_id"]))

    def tearDown(self):
        self.tmp.cleanup()

    def realize(self, observation=None):
        return realize(copy.deepcopy(self.package), copy.deepcopy(observation or self.observation), self.out)

    def lineage(self):
        """A ledger holding the lineage entry only (a lineage-only run)."""
        realize(copy.deepcopy(self.package), None, self.out)
        return self.path.read_text(encoding="utf-8")


class QuarantineTests(LedgerFixture):
    def test_a_truncated_last_line_is_quarantined_and_the_run_continues(self):
        good = self.lineage()
        fragment = '{"ledger_entry_id": "RLE-0123'  # a write cut short: no closing brace, no newline
        self.path.write_text(good + fragment, encoding="utf-8")
        self.assertEqual(len(ledger.load_ledger(self.path)), 1)  # a read without the lock ignores it ...
        self.assertEqual(self.path.read_text(encoding="utf-8"), good + fragment)  # ... and leaves the file alone
        with capture_logs() as logs:
            result = self.realize()
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(good) and text.endswith("\n"))
        self.assertNotIn(fragment, text)
        self.assertEqual(ledger.quarantine_path(self.path).read_text(encoding="utf-8"), fragment + "\n")
        self.assertEqual([line for line in logs if "ledger.quarantined" in line],
                         [f"WARNING ledger.quarantined ledger={self.path.name} bytes={len(fragment)}"])
        self.assertEqual(result.history["entry_count"], len(text.splitlines()))
        self.assertGreater(result.history["entry_count"], 1)

    def test_a_fragment_cut_inside_a_character_is_quarantined(self):
        good = self.lineage()
        self.path.write_bytes(good.encode("utf-8") + '{"actor": "é'.encode("utf-8")[:-1])
        self.assertEqual(len(ledger.load_ledger(self.path)), 1)
        self.realize()
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        self.assertTrue(ledger.quarantine_path(self.path).exists())

    def test_a_complete_last_entry_without_its_newline_is_kept_and_terminated(self):
        good = self.lineage()
        self.path.write_text(good.rstrip("\n"), encoding="utf-8")
        self.assertEqual(len(ledger.load_ledger(self.path)), 1)
        self.realize()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], good.rstrip("\n"))
        self.assertTrue(all(json.loads(line) for line in lines))
        self.assertFalse(ledger.quarantine_path(self.path).exists())

    def test_a_malformed_terminated_or_inner_line_still_fails_closed(self):
        good = self.lineage()
        for label, text in (("terminated last line", good + "not json\n"), ("inner line", "not json\n" + good),
                            ("inner line before a fragment", "not json\n" + good + "{")):
            with self.subTest(label):
                self.path.write_text(text, encoding="utf-8")
                result = outcome(self.realize)
                self.assertEqual(result[0], "LedgerError")
                self.assertEqual(self.path.read_text(encoding="utf-8"), text)
                self.assertFalse(ledger.quarantine_path(self.path).exists())

    def test_an_append_without_the_lock_refuses_to_join_a_fragment(self):
        good = self.lineage()
        self.path.write_text(good + "{", encoding="utf-8")
        entry = ledger.load_ledger(self.path)[0]
        other = ledger.make_ledger_entry(package=self.package, rpkg_id=entry.realization_package_id, event_type=rc.EventType.DRIFT_DETECTED,
                                         effective_at_utc="2026-10-09T12:00:00Z", recorded_at_utc="2026-10-09T12:00:00Z", payload={})
        self.assertEqual(outcome(ledger.append_if_absent, self.path, other)[0], "LedgerError")
        self.assertEqual(self.path.read_text(encoding="utf-8"), good + "{")


class LockTests(LedgerFixture):
    def test_a_second_run_on_a_locked_ledger_is_refused(self):
        self.lineage()
        before = self.path.read_text(encoding="utf-8")
        with ledger.ledger_lock(self.path), mock.patch.object(ledger, "LOCK_WAIT_SECONDS", 0.2):
            result = outcome(self.realize)
        self.assertEqual(result, ("LedgerError", f"ledger {self.path.name} is locked by another run"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.realize()  # released: the next run proceeds

    def test_the_lock_is_held_from_the_lineage_until_the_reporter_wrote(self):
        held = []
        original = reporter.run

        def probing(*args):
            result = original(*args)
            held.append(outcome(self._try_lock))
            return result

        with mock.patch.object(reporter, "run", probing), mock.patch.object(ledger, "LOCK_WAIT_SECONDS", 0):
            begin = ledger.record_creation

            def probing_begin(context):
                held.append(outcome(self._try_lock))
                return begin(context)

            with mock.patch.object(ledger, "record_creation", probing_begin):
                self.realize()
        self.assertEqual([h[0] for h in held], ["LedgerError", "LedgerError"])
        self.assertFalse(ledger.lock_path(self.path).exists(), "the lock file is removed on release")
        self.assertEqual(outcome(self._try_lock), ("ok", None))

    def test_a_failed_run_releases_the_lock(self):
        with mock.patch.object(reporter, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(outcome(self.realize), ("RuntimeError", "boom"))
        self.assertFalse(ledger.lock_path(self.path).exists())
        self.realize()

    def _try_lock(self):
        with ledger.ledger_lock(self.path):
            return None


if __name__ == "__main__":
    unittest.main()
