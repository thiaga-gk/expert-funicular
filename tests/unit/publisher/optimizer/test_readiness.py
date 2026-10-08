"""Evidence-based readiness after a configuration change.

A change is decided once the current configuration is ready (enough settled billed days in the new epoch), with each
knob's own sample and, for a capacity cut, the older-epoch peak hold; not after a fixed number of calendar days. A cost
change applied without a canary also waits for its own price. Each case simulates a demo warehouse whose setting was
changed `days` ago and runs it through the real collector path (extract_evidence).
"""
import copy
import datetime as dt
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import DEMO_ACCOUNT_ID, DEMO_ANALYSIS_TIME, DEMO_FLEET, DEMO_SKU_PRICES, DEMO_WORKSPACES, _simulate_demo_warehouse, replay_demo_rows, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.pricing import load_aws_price_registry  # noqa: E402
from publisher.analyzer.epochs import ready_billed_days  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402

AT = DEMO_ANALYSIS_TIME
DAY0 = dt.date.fromisoformat(AT[:10])
KEYMAP = {"auto_stop_minutes": "auto_stop", "max_clusters": "max", "min_clusters": "min", "warehouse_size": "size"}


def changed(scenario, knob, value, days, t=None, rows_hook=None):
    """Evidence and evaluation for a demo warehouse whose `knob` changed to `value` at 00:00 UTC `days` days ago.

    `knob` may be a dict of knob -> value for several settings changed together; `rows_hook(rows)` may edit the
    simulated collector rows before extraction."""
    t = t or Thresholds()
    changes = knob if isinstance(knob, dict) else {knob: value}
    idx, spec = next((i, s) for i, s in enumerate(DEMO_FLEET, start=1) if s["scenario"] == scenario)
    s2 = copy.deepcopy(spec)
    created = spec.get("created") or (spec["prior"][0]["start"] if spec.get("prior") else spec.get("epoch_start") or "2025-07-01")
    old = {k: spec[k] for k in ("size", "min", "max", "auto_stop")}
    prior = [dict(x) for x in spec.get("prior", [])] or [{"start": created}]
    s2["prior"] = [dict(x, **{k: v for k, v in old.items() if k not in x}) for x in prior]
    s2["epoch_start"] = (DAY0 - dt.timedelta(days=days)).isoformat()
    s2["created"] = created
    for k, v in changes.items():
        s2[KEYMAP[k]] = v
    rows = _simulate_demo_warehouse(s2, idx, AT)
    if rows_hook is not None:
        rows_hook(rows)
    rows["billing_watermark"] = [{"workspace_id": DEMO_WORKSPACES[spec["ws"]], "billing_watermark": AT[:10] + "T06:00:00+00:00",
                                  "billing_ingested_through": AT[:10]}]
    rows["sql_sku_prices"] = [{"account_id": DEMO_ACCOUNT_ID, "sku_name": n, "cloud": "AWS", "currency_code": "USD", "usage_unit": "DBU",
                               "effective_list_price": pr} for n, pr in sorted(DEMO_SKU_PRICES.items())]

    class Client:
        def query(self, sql, *, label="collector_sql"):
            return replay_demo_rows(rows, sql, label)

    reg = load_aws_price_registry(str(ROOT / "pricing" / "aws_ec2_on_demand.json"))
    e = extract_evidence(Client(), t, "us-east-1", reg, analysis_time=AT)[0]
    return e, {c.knob: c for c in p.evaluate_warehouses([e], t)[0].candidates}


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_not_ready_before_two_settled_billed_days(self):
        # Changed two days ago at 00:00: only one day has settled (billing settles a day after it ends).
        e, by = changed("C06", "auto_stop_minutes", 10, 2)
        self.assertEqual(ready_billed_days(e), 1)
        self.assertIn("CURRENT_CONFIG_EPOCH_TOO_NEW", by["warehouse_size"].blockers)
        self.assertEqual(by["max_clusters"].blockers[:1], ["CURRENT_CONFIG_EPOCH_TOO_NEW"])

    def test_ready_after_two_settled_billed_days_not_seven_calendar_days(self):
        # C06 three days after its auto-stop change: two settled billed days, 500+ executed queries since the change.
        # The size canary and the ceiling cut are decided now; under the 7-day age gate they waited until day 7.
        e, by = changed("C06", "auto_stop_minutes", 10, 3)
        self.assertEqual(ready_billed_days(e), 2)
        self.assertEqual((by["warehouse_size"].status, by["warehouse_size"].recommended_config["warehouse_size"]), ("TEST_REQUIRED", "MEDIUM"))
        self.assertEqual((by["max_clusters"].status, by["max_clusters"].recommended_config["max_clusters"]), ("ACTIONABLE", 2))

    def test_a_cost_change_without_a_canary_waits_for_its_price(self):
        # Three days in, the forward cost is still unknown (fewer than seven settled days): an approval-only cost change
        # is held with the cost-history code instead of shipping unpriced.
        e, by = changed("C06", "auto_stop_minutes", 15, 3)  # back at 15: the cheaper value 10 is approval-only
        a = by["auto_stop_minutes"]
        if a.current_config != a.recommended_config or a.status != "NO_CHANGE":
            self.assertEqual(a.status, "INSUFFICIENT_EVIDENCE")
            self.assertIn("COST_HISTORY_INSUFFICIENT", a.blockers)
        self.assertNotEqual(a.status, "ACTIONABLE")

    def test_reevaluation_waits_for_the_next_settled_day(self):
        e, _ = changed("C06", "auto_stop_minutes", 10, 2)
        pkg = p.build_recommendation_packages([e], Thresholds())[0]
        window = pkg.recommended_plan.reevaluation
        self.assertIn("CURRENT_CONFIG_EPOCH_TOO_NEW", window.blocking_codes)
        # The settled basis ends two days before the analysis day; at 00:00Z on 2026-10-02 one more day has settled.
        self.assertEqual(rc.expected_earliest_reevaluation_time(e.current_epoch.epoch_start, ["CURRENT_CONFIG_EPOCH_TOO_NEW"], AT), "2026-10-02T00:00:00Z")
        e_next, by_next = changed("C06", "auto_stop_minutes", 10, 3)  # the same change seen one day later is ready
        self.assertEqual(ready_billed_days(e_next), 2)
        billed = [x.value for x in pkg.evidence.values() if x.metric == "billed_cost_days"]
        self.assertEqual(billed, [1])

    def test_a_dormant_warehouse_is_not_called_too_new(self):
        e = next(x for x in synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C06")
        e = copy.deepcopy(e)
        e.daily_cost_365 = [dict(r, dbu_quantity=0.0, list_cost=0.0) for r in e.daily_cost_365]
        pkg = p.build_recommendation_packages([e], Thresholds())[0]
        self.assertNotIn("too new", pkg.recommended_plan.decision_summary)
        self.assertIn("settled billed days", pkg.recommended_plan.decision_summary)

    def test_contract_constants_match_policy(self):
        t = Thresholds()
        self.assertEqual((rc.REEVALUATION_READY_BILLED_DAYS, rc.REEVALUATION_COST_HISTORY_DAYS),
                         (t.readiness_min_billed_days, t.min_economics_cost_history_days))
        self.assertEqual(rc.CONTRACT_VERSION, "3.1.0")


if __name__ == "__main__":
    unittest.main()
