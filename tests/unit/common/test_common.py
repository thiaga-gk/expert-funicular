"""Shared utilities (src/publisher/common): deterministic keys, canonical JSON, time and value coercion."""
import dataclasses
import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.common import hashing, ids, time, utils  # noqa: E402


class InternalKeyTests(unittest.TestCase):
    def test_keys_are_deterministic_16_hex_and_prefixed(self):
        a = ids.epoch_id("ws", "wh", "2026-01-01T00:00:00+00:00")
        self.assertEqual(a, ids.epoch_id("ws", "wh", "2026-01-01T00:00:00+00:00"))
        self.assertRegex(a, r"^EPOCH-[0-9A-F]{16}$")
        self.assertRegex(ids.finding_id("ws", "wh", "warehouse_size", a, "m"), r"^FND-[0-9A-F]{16}$")
        self.assertRegex(ids.recommendation_id("ws", "wh", "max_clusters", a, {"max_clusters": 2}, {"max_clusters": 1}),
                         r"^WHOPT-MAX_CLUSTERS-[0-9A-F]{16}$")
        self.assertRegex(ids.scenario_id("ws", "wh", "Plan A", ["x", "y"]), r"^SCN-[0-9A-F]{16}$")

    def test_recommendation_key_ignores_mapping_order(self):
        one = ids.recommendation_id("ws", "wh", "k", "e", {"a": 1, "b": 2}, {})
        two = ids.recommendation_id("ws", "wh", "k", "e", {"b": 2, "a": 1}, {})
        self.assertEqual(one, two)


class CanonicalJsonTests(unittest.TestCase):
    def test_sorted_compact_and_dataclass_aware(self):
        @dataclasses.dataclass
        class Point:
            y: int
            x: int

        self.assertEqual(hashing.canonical_json({"b": [Point(2, 1)], "a": dt.date(2026, 1, 2)}),
                         '{"a":"2026-01-02","b":[{"x":1,"y":2}]}')


class TimeTests(unittest.TestCase):
    def test_naive_timestamps_are_utc_and_unparseable_is_none(self):
        self.assertEqual(time.iso("2026-01-02T03:04:05"), "2026-01-02T03:04:05+00:00")
        self.assertEqual(time.iso("2026-01-02T03:04:05Z"), "2026-01-02T03:04:05+00:00")
        self.assertIsNone(time.parse_datetime("not a time"))
        self.assertEqual(time._iso_z("2026-01-02T05:04:05+02:00"), "2026-01-02T03:04:05Z")
        with self.assertRaises(ValueError):
            time._iso_z(None)


class ValueTests(unittest.TestCase):
    def test_unknown_values_stay_unknown(self):
        self.assertIsNone(utils.optional_float(None))
        self.assertIsNone(utils.optional_float(True))
        self.assertIsNone(utils.optional_float(float("nan")))
        self.assertIsNone(utils.round_or_none(None))
        self.assertEqual(utils.safe_float("x", 3.0), 3.0)
        self.assertEqual(utils.safe_int(None, 7), 7)
        self.assertEqual(utils.clamp(5, 0, 1), 1)

    def test_map_columns_in_every_connector_shape(self):
        self.assertEqual(utils.normalize_map({"a": 1}), {"a": "1"})
        self.assertEqual(utils.normalize_map([["a", "1"]]), {"a": "1"})
        self.assertEqual(utils.normalize_map([{"key": "a", "value": "1"}]), {"a": "1"})
        self.assertEqual(utils.normalize_map('{"a": "1"}'), {"a": "1"})
        self.assertEqual(utils.normalize_map("not json"), {"raw": "not json"})
        self.assertEqual(utils.normalize_map(None), {})


if __name__ == "__main__":
    unittest.main()
