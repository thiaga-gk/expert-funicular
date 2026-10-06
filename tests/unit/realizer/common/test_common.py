"""Realizer shared helpers (src/realizer/common): logging and deterministic value, time and JSON file utilities.

Each helper returns, writes and raises what the single-file release's copy did (stored in
``tests/fixtures/realizer/expectations/``).
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
from tests import realizer_expect as expect  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402
from tests.unit.realizer.helpers import outcome  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.common import logging as rlogging, utils  # noqa: E402


class ValueHelperTests(unittest.TestCase):
    def check_results(self, name, values):
        results = {}
        for value in values:
            with self.subTest(helper=name, value=value):
                results[repr(value)] = outcome(getattr(utils, name), value)
        self.assertEqual(len(results), len(values))
        expect.check(self, "results", results)

    def test_utc_iso(self):
        self.check_results("utc_iso", [
            "2026-10-01T12:00:00Z", "2026-10-01T12:00:00+02:00", "2026-10-01T12:00:00", "2026-10-01",
            "2026-10-01T12:00:00.123456Z", dt.datetime(2026, 10, 1, 12), dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
            None, "", "garbage", 20261001,
        ])

    def test_decimal(self):
        self.check_results("decimal", [None, "1.50", 3, 0.1, Decimal("2E+2"), "-0", "NaN", "Infinity", "abc", ""])

    def test_money(self):
        self.check_results("money", [None, "2.345", "-2.345", "2.344999", 10, 0.005, "1E+3", "NaN", "x"])
        self.assertEqual(utils.money("2.345"), "2.35")
        self.assertEqual(utils.money("-2.345"), "-2.35")

    def test_ratio_string(self):
        self.check_results("ratio_string", [None, Decimal("0.80"), Decimal("1"), Decimal("0"), Decimal("1.2345000")])


class JsonFileTests(unittest.TestCase):
    VALUE = {"b": [1, 2.5, None], "a": {"é": "ü", "z": True}, "n": "x" * 3}

    def test_write_json_bytes_and_leave_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "deep" / "v.json"
            utils.write_json(path, self.VALUE)
            expect.check(self, "bytes", path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["v.json"])
            utils.write_json(path, {"replaced": 1})
            self.assertEqual(utils.load_json(path), {"replaced": 1})

    def test_load_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            path.write_text(json.dumps(self.VALUE, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(utils.load_json(path), self.VALUE)
            self.assertEqual(utils.load_json(str(path)), self.VALUE)
            expect.check(self, "missing file", outcome(utils.load_json, Path(tmp) / "missing.json")[0])


class DeferredTests(unittest.TestCase):
    """``Deferred`` holds a pure computation's outcome until the consumer needs it (the Governor's hand-off)."""

    def test_value_is_returned_by_every_get(self):
        d = utils.Deferred.of(lambda a, b=1: {"sum": a + b}, 2, b=3)
        self.assertEqual(d.get(), {"sum": 5})
        self.assertIs(d.get(), d.get())
        self.assertIsNone(d.error)

    def test_exception_is_kept_not_raised_by_of_and_raised_by_get(self):
        for fn, exc in [(lambda: int("x"), ValueError), (lambda: {}["k"], KeyError), (lambda: None.get, AttributeError)]:
            d = utils.Deferred.of(fn)
            self.assertIsInstance(d.error, exc)
            self.assertIsNone(d.value)
            for _ in range(2):
                with self.assertRaises(exc) as caught:
                    d.get()
                self.assertIs(caught.exception, d.error)

    def test_a_none_result_is_a_value_and_does_not_raise(self):
        self.assertIsNone(utils.Deferred.of(lambda: None).get())

    def test_only_exceptions_are_kept(self):
        def interrupt():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            utils.Deferred.of(interrupt)

    def test_is_frozen(self):
        import dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            utils.Deferred.of(lambda: 1).value = 2


class LoggingTests(unittest.TestCase):
    def test_logger_is_the_realizer_logger(self):
        self.assertEqual(rlogging.LOGGER.name, "realizer")
        self.assertIs(rlogging.LOGGER, logging.getLogger("realizer"))

    def test_setup_logging_configures_root_logging_as_the_single_file_main(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            with self.subTest(level=level), mock.patch("logging.basicConfig") as basic:
                rlogging.setup_logging(level)
                basic.assert_called_once_with(level=getattr(logging, level), format="%(asctime)sZ %(levelname)s %(message)s")


if __name__ == "__main__":
    unittest.main()
