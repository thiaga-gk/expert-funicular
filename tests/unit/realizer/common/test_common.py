"""Realizer shared helpers (src/realizer/common): logging and deterministic value, time and JSON file utilities.

Each helper returns, writes and raises exactly what the frozen single-file oracle's copy did.
"""
import datetime as dt
import json
import logging
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer import realizer as engine  # noqa: E402
from realizer.common import logging as rlogging, utils  # noqa: E402

oengine = oracle.engine()


def _outcome(fn, *args):
    """The value *fn* returns, or (exception type name, message) when it raises."""
    try:
        return "ok", fn(*args)
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)


class ValueHelperTests(unittest.TestCase):
    def assert_same(self, name, values):
        new, old = getattr(utils, name), getattr(oengine, name)
        self.assertIs(getattr(engine, name), new)
        for value in values:
            with self.subTest(helper=name, value=value):
                self.assertEqual(_outcome(new, value), _outcome(old, value))

    def test_utc_iso(self):
        self.assert_same("utc_iso", [
            "2026-10-01T12:00:00Z", "2026-10-01T12:00:00+02:00", "2026-10-01T12:00:00", "2026-10-01",
            "2026-10-01T12:00:00.123456Z", dt.datetime(2026, 10, 1, 12), dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
            None, "", "garbage", 20261001,
        ])

    def test_decimal(self):
        self.assert_same("decimal", [None, "1.50", 3, 0.1, Decimal("2E+2"), "-0", "NaN", "Infinity", "abc", ""])

    def test_money(self):
        self.assert_same("money", [None, "2.345", "-2.345", "2.344999", 10, 0.005, "1E+3", "NaN", "x"])
        self.assertEqual(utils.money("2.345"), "2.35")
        self.assertEqual(utils.money("-2.345"), "-2.35")

    def test_ratio_string(self):
        self.assert_same("ratio_string", [None, Decimal("0.80"), Decimal("1"), Decimal("0"), Decimal("1.2345000")])


class JsonFileTests(unittest.TestCase):
    VALUE = {"b": [1, 2.5, None], "a": {"é": "ü", "z": True}, "n": "x" * 3}

    def test_write_json_bytes_match_oracle_and_leave_no_temp_file(self):
        self.assertIs(engine.write_json, utils.write_json)
        with tempfile.TemporaryDirectory() as tmp:
            new, old = Path(tmp) / "new" / "deep" / "v.json", Path(tmp) / "old" / "deep" / "v.json"
            utils.write_json(new, self.VALUE)
            oengine.write_json(old, self.VALUE)
            self.assertEqual(new.read_bytes(), old.read_bytes())
            self.assertEqual(sorted(p.name for p in new.parent.iterdir()), ["v.json"])
            utils.write_json(new, {"replaced": 1})
            self.assertEqual(utils.load_json(new), {"replaced": 1})

    def test_load_json_matches_oracle(self):
        self.assertIs(engine.load_json, utils.load_json)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            path.write_text(json.dumps(self.VALUE, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(utils.load_json(path), oengine.load_json(path))
            self.assertEqual(utils.load_json(str(path)), self.VALUE)
            self.assertEqual(_outcome(utils.load_json, Path(tmp) / "missing.json")[0],
                             _outcome(oengine.load_json, Path(tmp) / "missing.json")[0])


class LoggingTests(unittest.TestCase):
    def test_logger_is_the_realizer_logger(self):
        self.assertEqual(rlogging.LOGGER.name, "realizer")
        self.assertIs(rlogging.LOGGER, logging.getLogger("realizer"))
        self.assertIs(engine.LOGGER, rlogging.LOGGER)
        self.assertIs(oengine.LOGGER, rlogging.LOGGER)

    def test_setup_logging_configures_root_logging_as_the_oracle_main(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            with self.subTest(level=level), mock.patch("logging.basicConfig") as basic:
                rlogging.setup_logging(level)
                basic.assert_called_once_with(level=getattr(logging, level), format="%(asctime)sZ %(levelname)s %(message)s")


if __name__ == "__main__":
    unittest.main()
