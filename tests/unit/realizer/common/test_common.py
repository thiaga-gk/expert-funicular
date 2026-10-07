"""Realizer shared helpers (src/realizer/common): logging and deterministic value, time and JSON file utilities.

Each helper returns, writes and raises what the single-file release's copy did (stored in
``tests/fixtures/realizer/expectations/``).
"""
import datetime as dt
import io
import json
import logging
import os
import sys
import tempfile
import time
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

    def setUp(self):
        self.root_handlers, self.root_level = list(logging.getLogger().handlers), logging.getLogger().level
        self.saved = (list(rlogging.LOGGER.handlers), rlogging.LOGGER.level, rlogging.LOGGER.propagate)
        rlogging.LOGGER.handlers = []
        self.addCleanup(self.restore)

    def restore(self):
        for handler in rlogging.LOGGER.handlers:
            handler.close()
        rlogging.LOGGER.handlers, rlogging.LOGGER.level, rlogging.LOGGER.propagate = self.saved

    def test_setup_logging_configures_the_realizer_logger_only(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            with self.subTest(level=level):
                rlogging.setup_logging(level)
                self.assertEqual(rlogging.LOGGER.level, getattr(logging, level))
                self.assertFalse(rlogging.LOGGER.propagate)
                self.assertEqual(len(rlogging.LOGGER.handlers), 1)
        self.assertEqual(logging.getLogger().handlers, self.root_handlers)
        self.assertEqual(logging.getLogger().level, self.root_level)

    def test_repeated_setup_replaces_the_handler_so_a_line_is_written_once(self):
        stream = io.StringIO()
        with mock.patch.object(sys, "stderr", stream):
            for _ in range(3):
                rlogging.setup_logging("INFO")
            rlogging.LOGGER.info("realizer.test once")
        self.assertEqual(len(rlogging.LOGGER.handlers), 1)
        self.assertEqual(stream.getvalue().count("realizer.test once"), 1)

    def test_lines_go_to_stderr_at_the_chosen_level(self):
        stream = io.StringIO()
        with mock.patch.object(sys, "stderr", stream):
            rlogging.setup_logging("WARNING")
            rlogging.LOGGER.info("realizer.test hidden")
            rlogging.LOGGER.warning("realizer.test shown")
        self.assertNotIn("hidden", stream.getvalue())
        self.assertRegex(stream.getvalue(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ WARNING realizer\.test shown\n$")

    def test_the_timestamp_is_utc_so_the_z_is_true(self):
        rlogging.setup_logging("INFO")
        formatter = rlogging.LOGGER.handlers[0].formatter
        record = logging.LogRecord("realizer", logging.INFO, __file__, 1, "realizer.test %s", ("x",), None)
        record.created, record.msecs = 0, 0
        for zone in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
            with self.subTest(tz=zone), mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                self.assertEqual(formatter.format(record), "1970-01-01T00:00:00Z INFO realizer.test x")
        time.tzset()


if __name__ == "__main__":
    unittest.main()
