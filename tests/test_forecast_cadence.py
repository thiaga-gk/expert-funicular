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
        self.check("monthly", 365, 1200.66, "CADENCE")  # true 1,199 (the 1 October run starts the basis: not an anchor)
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
        self.check("quarterly", 365, 403.31, "CADENCE")  # true 400, only once the epoch is about a year old

    def test_unknown_cadence_forecast_withholds_economics_as_too_few_billed_days(self):
        e = next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C06")
        e.forecast_metadata = dict(e.forecast_metadata, dbu_forecast_status="INSUFFICIENT_BILLING_CYCLES")
        self.assertEqual(p.dbu_baseline_blocker(e, p.Thresholds()), "BILLED_COST_DAYS_INSUFFICIENT")


def forecast_fn(fn, age):
    start = TODAY - dt.timedelta(days=age)
    rows = []
    for i in range(age, 0, -1):
        d = TODAY - dt.timedelta(days=i)
        c = fn(d, i)
        if c:
            rows.append({"usage_date": d.isoformat(), "list_cost": c, "dbu_quantity": c / 0.55, "retracted_dbu_quantity": 0.0,
                         "restated_dbu_quantity": 0.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"})
    return p.Modeler(p.Thresholds()).forecast_from_daily_cost(rows, start.isoformat(), AT)


class CadenceClassificationTests(unittest.TestCase):
    """Only short runs repeating on a current cadence are periodic; long runs with pauses keep the run rate."""

    def test_daily_warehouse_with_a_pause_keeps_the_run_rate(self):
        for fn, age in ((lambda d, i: 0 if 60 <= i < 70 else 100, 120),        # one 10-day holiday
                        (lambda d, i: 0 if 270 <= i < 284 else 100, 365),      # two-week year-end shutdown
                        (lambda d, i: 0 if (100 <= i < 110 or 50 <= i < 60) else (300 if i < 50 else 100), 150),
                        (lambda d, i: 100 if i == 100 or i <= 60 else 0, 100)):  # one test day, then daily
            f = forecast_fn(fn, age)
            self.assertEqual(f["forecast_model"], "RUN_RATE")
            self.assertIsNotNone(f["forecast_cost_365"])

    def test_a_cadence_that_stopped_is_not_forecast_from_old_cycles(self):
        # Monthly job last billed on 1 May; the basis ends 29 September: five missed cycles.
        f = forecast_fn(lambda d, i: 100 if d.day == 1 and d <= dt.date(2026, 5, 1) else 0, 365)
        self.assertEqual(f["forecast_model"], "RUN_RATE")
        self.assertIsNone(f["forecast_cost_365"])

    def test_a_weekend_inside_a_close_does_not_split_the_run(self):
        # Last five business days of each month: a weekend splits most closes into two pieces. Read on 26 December
        # 2026 with a 120-day epoch, the pieces once pulled the median cycle down and the forecast fell to the run rate
        # (3,015/yr); bridged into one run per close, the cadence model gives the true level (6,000/yr) within 5%.
        closes = {d for y in (2025, 2026) for m in range(1, 13) for d in last_bdays(y, m, 5)}
        today = dt.date(2026, 12, 26)
        start = today - dt.timedelta(days=120)
        rows = [{"usage_date": d.isoformat(), "list_cost": 100, "dbu_quantity": 100 / 0.55, "retracted_dbu_quantity": 0.0,
                 "restated_dbu_quantity": 0.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
                for d in (start + dt.timedelta(days=k) for k in range(120)) if d in closes]
        f = p.Modeler(p.Thresholds()).forecast_from_daily_cost(rows, start.isoformat(), "2026-12-26T12:00:00+00:00")
        self.assertEqual(f["forecast_model"], "CADENCE")
        self.assertAlmostEqual(f["forecast_cost_365"], 6000, delta=0.05 * 6000)

    def test_five_day_quarter_end_close_is_periodic(self):
        closes = {d for y in (2025, 2026) for m in (3, 6, 9, 12) for d in last_bdays(y, m, 5)}
        f = forecast_fn(lambda d, i: 100 if d in closes else 0, 365)
        self.assertEqual(f["forecast_model"], "CADENCE")
        self.assertAlmostEqual(f["forecast_cost_365"], 2000, delta=0.03 * 2000)

    def test_long_runs_keep_the_run_rate(self):
        # Ten days on, twenty off: runs longer than a week are not a periodic burst.
        f = forecast_fn(lambda d, i: 100 if i % 30 < 10 else 0, 180)
        self.assertEqual(f["forecast_model"], "RUN_RATE")

    def test_two_runs_a_month_stay_current_between_them(self):
        # Billed on the 1st and the 8th: cycles alternate 7 and 23-24 days. Recency on the median (7) once called the
        # cadence stopped for about eight days a month; the upper quartile keeps it current (true 2,400/yr).
        for k in range(30):
            today = dt.date(2026, 6, 1) + dt.timedelta(days=k)
            start = today - dt.timedelta(days=365)
            rows = [{"usage_date": d.isoformat(), "list_cost": 100, "dbu_quantity": 100 / 0.55, "retracted_dbu_quantity": 0.0,
                     "restated_dbu_quantity": 0.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
                    for d in (start + dt.timedelta(days=j) for j in range(365)) if d.day in (1, 8)]
            f = p.Modeler(p.Thresholds()).forecast_from_daily_cost(rows, start.isoformat(), f"{today.isoformat()}T12:00:00+00:00")
            self.assertEqual(f["forecast_model"], "CADENCE", today)
            self.assertAlmostEqual(f["forecast_cost_365"], 2400, delta=0.05 * 2400, msg=today)

    def test_frequent_ad_hoc_use_keeps_the_run_rate_and_its_trend(self):
        # Random billing on about 30% of days bridges into short runs, but they rest less than a week: not periodic.
        rnd = random.Random(1)
        days = {i for i in range(1, 366) if rnd.random() < 0.3}
        f = forecast_fn(lambda d, i: 100 if i in days else 0, 365)
        self.assertEqual(f["forecast_model"], "RUN_RATE")

    def test_a_run_bridged_across_the_basis_start_is_not_an_anchor(self):
        # Four-day month-end close; the basis starts on Saturday 29 August 2026 inside the August close, whose
        # Monday 31 August piece is index 2. Anchoring there gave 3,202/yr against a true 4,800.
        closes = {d for y in (2025, 2026) for m in range(1, 13) for d in last_bdays(y, m, 4)}
        today = dt.date(2026, 11, 2)
        start = today - dt.timedelta(days=65)
        rows = [{"usage_date": d.isoformat(), "list_cost": 100, "dbu_quantity": 100 / 0.55, "retracted_dbu_quantity": 0.0,
                 "restated_dbu_quantity": 0.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
                for d in (start + dt.timedelta(days=k) for k in range(65)) if d in closes]
        f = p.Modeler(p.Thresholds()).forecast_from_daily_cost(rows, start.isoformat(), "2026-11-02T12:00:00+00:00")
        self.assertNotEqual(f["cadence_window_start"], "2026-08-31")
        if f["forecast_cost_365"] is not None:
            self.assertAlmostEqual(f["forecast_cost_365"], 4800, delta=0.06 * 4800)

    def test_a_stopped_monthly_job_lapses_within_about_forty_days(self):
        # Billed on the 1st until 1 May 2026: still current on 5 June, lapsed (run rate, too few billed days) by 15 June.
        def at(today):
            start = today - dt.timedelta(days=365)
            rows = [{"usage_date": d.isoformat(), "list_cost": 100, "dbu_quantity": 100 / 0.55, "retracted_dbu_quantity": 0.0,
                     "restated_dbu_quantity": 0.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
                    for d in (start + dt.timedelta(days=j) for j in range(365)) if d.day == 1 and d <= dt.date(2026, 5, 1)]
            return p.Modeler(p.Thresholds()).forecast_from_daily_cost(rows, start.isoformat(), f"{today.isoformat()}T12:00:00+00:00")
        self.assertEqual(at(dt.date(2026, 6, 5))["forecast_model"], "CADENCE")
        late = at(dt.date(2026, 6, 15))
        self.assertEqual(late["forecast_model"], "RUN_RATE")
        self.assertIsNone(late["forecast_cost_365"])

    def test_a_run_cut_by_the_basis_start_is_not_a_cycle_anchor(self):
        # Two-day runs on the 1st and 2nd of each month; the epoch starts on 2 June, inside a run. Anchoring on that
        # cut run would count half a run for a whole cycle (2,005/yr); whole cycles from 1 July give 2,355 (true 2,400).
        f = forecast_fn(lambda d, i: 100 if d.day in (1, 2) else 0, (TODAY - dt.date(2026, 6, 2)).days)
        self.assertEqual(f["forecast_model"], "CADENCE")
        self.assertEqual(f["cadence_window_start"], "2026-07-01")
        self.assertAlmostEqual(f["forecast_cost_365"], 400 / 62 * 365, delta=1.0)

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
