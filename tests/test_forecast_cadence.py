"""Forward DBU forecast for periodic workloads (cadence model).

Billing with no gap over a week keeps the run-rate model. Slower cadences (bi-weekly, monthly, month-end close,
quarterly) group billed days into runs and average whole run-to-run cycles of the current epoch within 365 days; fewer
than two whole cycles leave the forward cost unknown, never a wrong number. Weekly billing uses cycles only when the
run-rate model has no number.
"""
import datetime as dt
import logging
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

AT = "2026-10-01T12:00:00+00:00"
TODAY = dt.date(2026, 10, 1)


def last_bdays(y, m, n):
    d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


def cost(name, d, rnd):
    if name == "ad hoc":
        return 100 if rnd.random() < 0.1 else 0
    if name == "daily":
        return 100
    if name == "weekdays":
        return 100 if d.weekday() < 5 else 0
    if name == "weekly":
        return 100 if d.weekday() == 0 else 0
    if name == "bi-weekly":
        return 100 if d.weekday() == 0 and (d - dt.date(2025, 1, 6)).days % 14 == 0 else 0
    if name == "monthly":
        return 100 if d.day == 1 else 0
    if name == "month-end":
        return 300 if d in last_bdays(d.year, d.month, 3) else 0
    if name == "quarterly":
        return 100 if d.day == 1 and d.month in (1, 4, 7, 10) else 0
    if name == "semi-yearly":
        return 100 if d.day == 1 and d.month in (1, 7) else 0
    if name == "yearly":
        return 100 if d == dt.date(d.year, 1, 15) else 0
    raise ValueError(name)


def forecast(name, age):
    rnd = random.Random(7)
    series = {}
    for i in range(800, 0, -1):
        d = TODAY - dt.timedelta(days=i)
        series[d] = cost(name, d, rnd)
    start = TODAY - dt.timedelta(days=age)
    rows = [{"usage_date": d.isoformat(), "list_cost": c, "dbu_quantity": c / 0.55, "retracted_dbu_quantity": 0.0,
             "restated_dbu_quantity": 0.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
            for d, c in series.items() if c > 0 and d >= start]
    return p.Modeler(p.Thresholds()).forecast_from_daily_cost(rows, start.isoformat(), AT)


class CadenceForecastTests(unittest.TestCase):
    def check(self, name, age, expected, model):
        f = forecast(name, age)
        if expected is None:
            self.assertIsNone(f["forecast_cost_365"], (name, age))
        else:
            self.assertAlmostEqual(f["forecast_cost_365"], expected, delta=1.0, msg=(name, age))
        self.assertEqual(f["forecast_model"], model, (name, age))
        return f

    def test_frequent_billing_keeps_the_run_rate_model(self):
        self.check("daily", 365, 36500, "RUN_RATE")
        self.check("weekdays", 90, 26071, "RUN_RATE")

    def test_weekly_billing_uses_cycles_only_when_the_run_rate_has_no_number(self):
        self.check("weekly", 90, 5214, "RUN_RATE")
        self.check("weekly", 30, 5214, "CADENCE")

    def test_bi_weekly_and_monthly_forecast_from_whole_cycles(self):
        self.check("bi-weekly", 90, 2607, "CADENCE")  # true 2,623
        self.check("monthly", 365, 1199, "CADENCE")   # true 1,199
        f = self.check("monthly", 180, 1187, "CADENCE")
        self.assertEqual((f["billing_runs"], f["billing_cycles"]), (5, 4))  # 1 May .. 1 September
        self.assertIsNone(f["cost_cv_84"])
        self.assertEqual(f["trend_adjustment_factor"], 1.0)

    def test_month_end_close_counts_the_whole_burst(self):
        # Today's run rate over the last 84 days misses part of the bursts (8,188); true is 10,793.
        self.check("month-end", 90, 10770, "CADENCE")
        self.check("month-end", 365, 10819, "CADENCE")

    def test_fewer_than_two_whole_cycles_stay_unknown(self):
        for name, age in (("semi-yearly", 365), ("monthly", 90), ("bi-weekly", 30)):
            f = self.check(name, age, None, "CADENCE")
            self.assertEqual(f["dbu_forecast_status"], "INSUFFICIENT_BILLING_CYCLES", (name, age))
        # One billed day has no gap to measure: the run-rate model, with too few billed days.
        for name, age in (("yearly", 365), ("quarterly", 180)):
            f = self.check(name, age, None, "RUN_RATE")
            self.assertEqual(f["dbu_forecast_status"], "INSUFFICIENT_ACTIVE_COST_DAYS", (name, age))
        self.check("quarterly", 365, 401, "CADENCE")  # true 400, only once the epoch is about a year old

    def test_unknown_cadence_forecast_withholds_economics_as_too_few_billed_days(self):
        e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C06")
        e.forecast_metadata = dict(e.forecast_metadata, dbu_forecast_status="INSUFFICIENT_BILLING_CYCLES")
        self.assertEqual(p.dbu_baseline_blocker(e, p.Thresholds()), "BILLED_COST_DAYS_INSUFFICIENT")


class CadenceDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C52")

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_c52_month_end_close_gets_a_forward_cost_from_its_cycles(self):
        # One billed day a month (the last business day): ten whole cycles from 2025-10-31 to 2026-08-31.
        meta = self.e.forecast_metadata
        self.assertEqual(meta["forecast_model"], "CADENCE")
        self.assertEqual(meta["dbu_forecast_status"], "COMPLETE")
        self.assertAlmostEqual(self.e.baseline_forecast_cost_365, 1937, delta=1.0)
        self.assertEqual((meta["billing_runs"], meta["billing_cycles"]), (11, 10))
        self.assertEqual((meta["cadence_window_start"], meta["cadence_window_end"]), ("2025-10-31", "2026-08-31"))

    def test_c52_aws_uses_the_whole_cycles_warehouse_events_cover(self):
        m = self.e.aws_cost_model
        self.assertEqual(m["status"], "MODELED")
        self.assertEqual(m["cadence_cycles"], 2)  # runs 06-30, 07-31, 08-31 inside event coverage
        self.assertEqual((m["resource_window_start"], m["resource_window_end"]), ("2026-06-30", "2026-08-30"))


if __name__ == "__main__":
    unittest.main()
