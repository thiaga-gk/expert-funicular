"""Realizer ledger read side (src/realizer/packager/ledger.py): path layout, loading and the latest-event lookup.

Each function returns, and raises, what the frozen oracle's copy did; every ledger of the compatibility baseline loads
to the same entries.
"""
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.parity import canonical, engine, oengine, outcome  # noqa: E402
from realizer.packager import ledger  # noqa: E402


class LedgerReadTests(unittest.TestCase):
    def test_engine_reexports_and_path_layout(self):
        for name in ("ledger_path", "load_ledger", "latest_event"):
            self.assertIs(getattr(engine, name), getattr(ledger, name))
        out = Path("/tmp/out")
        self.assertEqual(ledger.ledger_path(out, "RPKG-X"), oengine.ledger_path(out, "RPKG-X"))
        self.assertEqual(ledger.ledger_path(out, "RPKG-X"), out / "realization" / "ledger" / "RPKG-X.jsonl")

    def test_every_stored_ledger_loads_like_the_oracle_and_latest_event_agrees(self):
        types = [("APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED"), ("ECONOMIC_MEASUREMENT_RECORDED",), ("DRIFT_DETECTED",), ("NOPE_TYPE",)]
        count = 0
        for name in canonical.scenario_names():
            for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
                with self.subTest(scenario=name):
                    new, old = ledger.load_ledger(path), oengine.load_ledger(path)
                    self.assertEqual([e.to_dict() for e in new], [e.to_dict() for e in old])
                    for names in types:
                        found_new = ledger.latest_event(new, *[engine.rc.EventType(n) for n in names if n != "NOPE_TYPE"])
                        found_old = oengine.latest_event(old, *[oengine.rc.EventType(n) for n in names if n != "NOPE_TYPE"])
                        self.assertEqual(None if found_new is None else found_new.ledger_entry_id,
                                         None if found_old is None else found_old.ledger_entry_id)
                    count += 1
        self.assertGreater(count, 30)
        self.assertEqual(ledger.latest_event([]), None)

    def test_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ledger.load_ledger(Path(tmp) / "none.jsonl"), [])
            self.assertEqual(oengine.load_ledger(Path(tmp) / "none.jsonl"), [])

    def test_malformed_ledger_raises_the_oracle_exception(self):
        good = next((canonical.EXPECTED / "GR-024" / "realization" / "ledger").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
        import json
        first = json.loads(good[0])
        cases = {
            "not json": "not json\n",
            "unknown event type": json.dumps({**first, "event_type": "NOPE"}) + "\n",
            "missing event type": json.dumps({k: v for k, v in first.items() if k != "event_type"}) + "\n",
            "unexpected field": json.dumps({**first, "extra": 1}) + "\n",
            "missing field": json.dumps({k: v for k, v in first.items() if k != "payload"}) + "\n",
            "invalid id": json.dumps({**first, "ledger_entry_id": "BAD"}) + "\n",
            "bad second line": good[0] + "\n[1]\n",
            "blank lines only": "\n  \n",
        }
        for label, text in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "l.jsonl"
                path.write_text(text, encoding="utf-8")
                new = outcome(lambda: [e.to_dict() for e in ledger.load_ledger(path)])
                old = outcome(lambda: [e.to_dict() for e in oengine.load_ledger(path)])
                self.assertEqual(new, old)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(outcome(ledger.load_ledger, Path(tmp))[0], outcome(oengine.load_ledger, Path(tmp))[0])


if __name__ == "__main__":
    unittest.main()
