"""Realized to date (src/realizer/packager/to_date.py): the latest measurement covering each instant, pro rata, summed.

The windows are the GE-12 world's daily runs (wh-demo-005, auto-stop 45 to 10 on 2026-10-08 14:32 UTC): each run measures
the latest whole weeks after the change, so the windows overlap and no single one says what was realized since the change.
"""
import unittest
from decimal import Decimal

from realizer.packager.to_date import MATURE_DAYS, measured_window, realized_to_date


def w(start, end, realized):
    return measured_window(f"{start}T00:00:00Z", f"{end}T00:00:00Z", realized)


# The GE-12 runs of Oct 23, 24, 25 and 30 (window end exclusive: midnight after the last settled day).
OCT23 = w("2026-10-15", "2026-10-22", "141.13")
OCT24 = w("2026-10-09", "2026-10-23", "282.42")
OCT25 = w("2026-10-10", "2026-10-24", "282.80")
OCT30 = w("2026-10-15", "2026-10-29", "282.71")


class RealizedToDateTests(unittest.TestCase):
    def test_overlapping_daily_windows_sum_each_day_once(self):
        # Oct 9 from the Oct 24 window (282.42 / 14), Oct 10-14 from the Oct 25 window (5 x 282.80 / 14), Oct 15-28 from
        # the Oct 30 window: 20.17 + 101.00 + 282.71. The latest window alone says 282.71 for 14 of the 20 days.
        got = realized_to_date([OCT23, OCT24, OCT25, OCT30])
        self.assertEqual(got["realized_to_date"], "403.88")
        self.assertEqual((got["first_day"], got["last_day"], got["days_covered"], got["uncovered_days"]),
                         ("2026-10-09", "2026-10-28", "20.00", 0))
        self.assertFalse(got["mature"])
        self.assertIsNone(got["mature_ttm_realized_savings"])

    def test_a_window_inside_the_latest_one_adds_nothing(self):
        # The Nov 7 run's window covers Oct 9 - Nov 5: every earlier window is inside it, so it alone counts.
        nov7 = w("2026-10-09", "2026-11-06", "564.91")
        self.assertEqual(realized_to_date([OCT23, OCT24, OCT25, OCT30, nov7])["realized_to_date"], "564.91")

    def test_recording_order_decides_not_window_order(self):
        # A re-measured window (a restatement) recorded later replaces the earlier value of its days.
        restated = w("2026-10-15", "2026-10-29", "270.00")
        self.assertEqual(realized_to_date([OCT30, restated])["realized_to_date"], "270.00")
        self.assertEqual(realized_to_date([restated, OCT30])["realized_to_date"], "282.71")

    def test_a_gap_between_windows_is_stated(self):
        # Runs four weeks apart with one-week windows: the days between are not measured and say so.
        got = realized_to_date([w("2026-10-09", "2026-10-16", "140.00"), w("2026-10-30", "2026-11-06", "140.00")])
        self.assertEqual((got["realized_to_date"], got["days_covered"], got["uncovered_days"]), ("280.00", "14.00", 14))

    def test_signed_sum_keeps_a_cost_increase(self):
        got = realized_to_date([w("2026-10-09", "2026-10-16", "-70.00"), w("2026-10-16", "2026-10-23", "140.00")])
        self.assertEqual(got["realized_to_date"], "70.00")

    def test_mature_after_365_covered_days_with_the_trailing_year(self):
        # One window of 400 days at 1.00 a day: mature, and the trailing year is its latest 365 days.
        windows = [w("2025-10-01", "2026-11-05", "400.00")]
        got = realized_to_date(windows)
        self.assertTrue(got["mature"])
        self.assertEqual(got["mature_ttm_realized_savings"], "365.00")
        self.assertEqual(MATURE_DAYS, 365)

    def test_no_window_or_an_empty_one(self):
        self.assertIsNone(realized_to_date([]))
        self.assertIsNone(measured_window("2026-10-09T00:00:00Z", "2026-10-09T00:00:00Z", "1"))
        self.assertIsNone(measured_window("2026-10-09T00:00:00Z", None, "1"))
        self.assertIsNone(measured_window("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z", None))

    def test_partial_day_windows_of_caller_observations(self):
        # A caller window that starts at noon covers half of its first day.
        got = realized_to_date([measured_window("2026-10-09T12:00:00Z", "2026-10-11T00:00:00Z", "30")])
        self.assertEqual((got["realized_to_date"], got["days_covered"]), ("30.00", "1.50"))
        self.assertEqual(Decimal(got["realized_to_date"]), Decimal("30"))


if __name__ == "__main__":
    unittest.main()
