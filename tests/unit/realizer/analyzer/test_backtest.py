"""The backtest: past auto-stop changes found in configuration epochs, read through the Publisher's SQL, reported.

``realizer.analyzer.backtest`` (which epoch pairs are changes, their windows, the prediction and the realization, the
report), ``realizer.analyzer.collector.backtest_collector`` (two passes in rounds into backtest evidence) and the
backtest evidence format (``realizer.contracts.backtest``). Worlds are demo-fleet warehouses whose auto-stop was changed
in the past (``bridge.demo_fleet_client``), read with the bridge's ``DemoWorldClient`` as a live run reads system tables.
"""
import copy
import functools
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import jsonschema

from tests import realizer_world as world
from tests.unit.realizer.contracts.test_schema import COMMITTED, serialize
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from publisher.analyzer.collector.queries.events import running_idle_request_from_statement
from realizer.analyzer import backtest as bt
from realizer.analyzer.collector import backtest_collector as collector
from realizer.analyzer.collector import bridge
from realizer.common.errors import InputError
from realizer.config.settings import SUPPORTED_WAREHOUSE_TYPES
from realizer.contracts import backtest as contract

AS_OF = collector.DEMO_ANALYSIS_TIME
HISTORY = "2025-10-01"  # the first day a run on the analysis day holds
C13_LOWERED = {"scenario": "C13", "after": {"auto_stop": 10}, "change_day": "2026-08-01", "change_minute": 14 * 60 + 32}


def epoch(start, end=None, **cfg):
    base = {"warehouse_type": "PRO", "warehouse_channel": "CURRENT", "warehouse_size": "SMALL", "min_clusters": 1, "max_clusters": 2,
            "auto_stop_minutes": 30}
    return {"epoch_start": start, "epoch_end": end, **base, **cfg}


def find(epochs, settled="2026-09-29", history=HISTORY, **kw):
    return bt.find_changes("ws", "wh", epochs, settled, history, supported_types=SUPPORTED_WAREHOUSE_TYPES, **kw)


@functools.lru_cache(maxsize=None)
def _evidence(changes_json, region):
    changes = json.loads(changes_json)
    return collector.collect_backtest(bridge.demo_fleet_client(changes, AS_OF), as_of=AS_OF, source="DEMO_SIMULATION", history_first_day=HISTORY,
                                      source_region=region, aws_registry=world.registry())


def evidence(*changes, region="us-east-1"):
    return copy.deepcopy(_evidence(json.dumps(changes, sort_keys=True), region))


def report(*changes, region="us-east-1"):
    return bt.backtest_report(evidence(*changes, region=region), supported_types=SUPPORTED_WAREHOUSE_TYPES)


class ChangeFinderTests(unittest.TestCase):
    """Which epoch pairs are past auto-stop changes, and the whole UTC days each is measured on."""

    def test_consecutive_auto_stop_changes_with_every_other_knob_equal(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
                  epoch("2026-06-01T00:00:00+00:00", "2026-08-15T09:00:00+00:00", auto_stop_minutes=10),
                  epoch("2026-08-15T09:00:00+00:00", "2026-09-01T00:00:00+00:00", auto_stop_minutes=10, warehouse_size="X_SMALL"),
                  epoch("2026-09-01T00:00:00+00:00", auto_stop_minutes=20, warehouse_size="SMALL")]
        changes = find(epochs)
        self.assertEqual([(c.old, c.new, c.reason) for c in changes], [(30, 10, None), (10, 20, "OTHER_KNOB_CHANGED")])
        self.assertIsNone(changes[1].post)  # a size change made with the auto-stop change cannot isolate auto-stop

    def test_a_partial_change_day_is_in_neither_window(self):
        partial = find([epoch("2025-07-01T00:00:00+00:00", "2026-08-01T14:32:00+00:00"), epoch("2026-08-01T14:32:00+00:00", auto_stop_minutes=10)])[0]
        self.assertEqual((partial.post["first_day"], partial.post["last_day"]), ("2026-08-05", "2026-09-29"))  # 8 whole weeks from 08-02
        self.assertEqual((partial.pre["first_day"], partial.pre["last_day"]), ("2026-06-06", "2026-07-31"))
        at_midnight = [epoch("2025-07-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"), epoch("2026-08-01T00:00:00+00:00", auto_stop_minutes=10)]
        midnight = find(at_midnight, settled="2026-08-07")[0]
        self.assertEqual((midnight.post["first_day"], midnight.post["last_day"], midnight.pre["last_day"]), ("2026-08-01", "2026-08-07", "2026-07-31"))
        late = [epoch("2025-07-01T00:00:00+00:00", "2026-08-01T14:32:00+00:00"), epoch("2026-08-01T14:32:00+00:00", auto_stop_minutes=10)]
        self.assertEqual(find(late, settled="2026-08-07")[0].reason, "NOT_SETTLED")  # six whole days after a change at 14:32
        self.assertEqual(partial.windows()["post"]["targets"], [30])  # the old value replayed after the change
        self.assertEqual(partial.windows()["pre"]["targets"], [10])  # the new value replayed before it: the prediction

    def test_a_closed_epoch_is_measured_to_the_day_before_it_ended(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"),
                  epoch("2026-03-02T00:00:00+00:00", "2026-04-20T10:00:00+00:00", auto_stop_minutes=10),
                  epoch("2026-04-20T10:00:00+00:00", auto_stop_minutes=20)]
        first, second = find(epochs)
        self.assertEqual((first.post["first_day"], first.post["last_day"]), ("2026-03-02", "2026-04-19"))  # 7 whole weeks
        self.assertEqual(first.post["epoch_start"], "2026-03-02T00:00:00+00:00")
        self.assertEqual((second.old, second.new, second.raised, first.raised), (10, 20, True, False))
        self.assertEqual(second.pre["last_day"], "2026-04-19")

    def test_windows_are_at_most_twelve_weeks_and_the_pre_window_is_shorter_when_its_epoch_is(self):
        long = find([epoch("2025-10-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"), epoch("2026-06-01T00:00:00+00:00", auto_stop_minutes=10)])[0]
        self.assertEqual((bt.window_days(long.post), bt.window_days(long.pre), long.post["last_day"]), (84, 84, "2026-09-29"))
        short = find([epoch("2026-07-20T00:00:00+00:00", "2026-08-01T14:32:00+00:00"), epoch("2026-08-01T14:32:00+00:00", auto_stop_minutes=10)])[0]
        self.assertEqual((bt.window_days(short.post), bt.window_days(short.pre)), (56, 7))
        self.assertEqual(short.pre["first_day"], "2026-07-25")

    def test_without_a_whole_week_before_the_change_it_is_measured_without_a_prediction(self):
        change = find([epoch("2026-07-28T00:00:00+00:00", "2026-08-01T14:32:00+00:00"), epoch("2026-08-01T14:32:00+00:00", auto_stop_minutes=10)])[0]
        self.assertIsNone(change.reason)
        self.assertIsNone(change.pre)
        self.assertIsNone(change.windows()["pre"])

    def test_not_settled_and_before_the_history(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-09-25T12:00:00+00:00"), epoch("2026-09-25T12:00:00+00:00", auto_stop_minutes=10)]
        self.assertEqual(find(epochs)[0].reason, "NOT_SETTLED")  # four whole days
        self.assertEqual(find(epochs, settled=None)[0].reason, "NOT_SETTLED")
        old = [epoch("2025-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00"), epoch("2025-06-01T00:00:00+00:00", "2025-10-05T00:00:00+00:00", auto_stop_minutes=10),
               epoch("2025-10-05T00:00:00+00:00", auto_stop_minutes=15)]
        first, second = find(old)
        self.assertEqual(first.reason, "BEFORE_HISTORY")  # its whole weeks ended before the first day the history holds
        self.assertIsNone(second.pre)  # the epoch before holds four days of history: no whole pre week

    def test_unsupported_types_and_the_execution_warehouse_are_not_measured(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", warehouse_type="SERVERLESS_V9"),
                  epoch("2026-06-01T00:00:00+00:00", auto_stop_minutes=10, warehouse_type="SERVERLESS_V9")]
        self.assertEqual(find(epochs)[0].reason, "UNSUPPORTED_WAREHOUSE_TYPE")
        mine = [epoch("2025-07-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"), epoch("2026-06-01T00:00:00+00:00", auto_stop_minutes=10)]
        self.assertEqual(find(mine, execution_warehouse_id="wh")[0].reason, "EXECUTION_WAREHOUSE")
        self.assertIsNone(find(mine, execution_warehouse_id="other")[0].reason)

    def test_rounds_read_one_epoch_per_warehouse(self):
        a = bt.find_changes("ws", "a", [epoch("2025-07-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"),
                                        epoch("2026-03-02T00:00:00+00:00", "2026-06-01T00:00:00+00:00", auto_stop_minutes=10),
                                        epoch("2026-06-01T00:00:00+00:00", auto_stop_minutes=20)], "2026-09-29", HISTORY, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        b = bt.find_changes("ws", "b", [epoch("2025-07-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"), epoch("2026-06-01T00:00:00+00:00", auto_stop_minutes=5)],
                            "2026-09-29", HISTORY, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        rounds = bt.collection_rounds(b + a)
        self.assertEqual([[(c.warehouse_id, c.new) for c in r] for r in rounds], [[("a", 10), ("b", 5)], [("a", 20)]])


class SimulatedBacktestTests(unittest.TestCase):
    """Past changes of simulated demo-fleet warehouses, read through the Publisher SQL and reported."""

    def test_pro_auto_stop_30_to_10_prediction_and_realization_agree(self):
        ev = evidence(C13_LOWERED)
        collector.validate_backtest_evidence(ev)
        out = bt.backtest_report(ev, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        (c,) = out["changes"]
        self.assertEqual((c["change_at_utc"], c["old_auto_stop_minutes"], c["new_auto_stop_minutes"], c["direction"]),
                         ("2026-08-01T14:32:00Z", 30, 10, "LOWERED"))
        # 8 weekday-weeks of 14 queries a day, 45 minutes apart, 3 minutes each: 560 gaps of 42 minutes. At 30 the warehouse
        # runs 33 minutes per query, at 10 it runs 13: 20 cluster-minutes a gap, 11,200 in the window, both ways.
        self.assertEqual((c["post_window"]["days"], c["post_window"]["queries"], c["post_window"]["running_cluster_minutes"]), (56, 560, "7280"))
        self.assertEqual((c["pre_window"]["first_day"], c["pre_window"]["running_cluster_minutes"]), ("2026-06-06", "18480"))
        self.assertEqual((c["prediction"]["net_cluster_minutes"], c["realization"]["net_cluster_minutes"]), ("11200", "11200"))
        self.assertEqual((c["prediction"]["replayed_target_minutes"], c["realization"]["replayed_target_minutes"]), (10, 30))
        self.assertEqual(c["realization"]["aws_saving"], "698.88")  # 11,200 / 60 x 3.744 per cluster-hour
        # Priced at the window's billed cost per running cluster-minute (the billed total is shown rounded to the cent).
        self.assertAlmostEqual(float(c["realization"]["dbu_saving"]), 11200 * float(c["post_window"]["billed_cost"]) / 7280, delta=0.02)
        self.assertAlmostEqual(float(c["realized_to_predicted_ratio"]), 1.0, delta=0.03)
        self.assertEqual((c["cross_check"]["outcome"], c["cost_basis"], c["volume_ratio"]), ("AGREES", "DBU_AND_MODELED_AWS", "1"))
        self.assertEqual(c["cold_start_share_pct"], {"pre": "100", "post": "100"})  # every query starts the warehouse both ways
        self.assertEqual(c["guards"]["post"]["queued_query_pct"], "0")
        self.assertAlmostEqual(float(c["realization"]["annual_saving"]), float(c["realization"]["window_saving"]) * 365 / 56, delta=0.05)  # x 365 / days
        self.assertEqual((out["skipped"], out["warehouses_read"], out["report_format_version"]), ([], 1, "1.0.0"))

    def test_a_change_without_a_whole_week_before_it_is_reported_without_a_prediction(self):
        ev = evidence(C13_LOWERED)
        held = ev["warehouses"][0]
        held["epochs"][0]["epoch_start"] = "2026-07-29T00:00:00+00:00"  # three days before the change: no whole pre-change week
        held["changes"][0]["windows"]["pre"], held["changes"][0]["guard"] = None, None
        (c,) = bt.backtest_report(ev, supported_types=SUPPORTED_WAREHOUSE_TYPES)["changes"]
        self.assertIsNone(c["pre_window"])
        self.assertIsNone(c["prediction"])
        self.assertEqual(c["realization"]["net_cluster_minutes"], "11200")
        self.assertEqual((c["cross_check"]["outcome"], c["cross_check"]["reason"]), ("INCONCLUSIVE", "NO_PRE_WINDOW"))

    def test_a_raised_auto_stop_realizes_a_cost_increase(self):
        (c,) = report({**C13_LOWERED, "after": {"auto_stop": 45}})["changes"]
        self.assertEqual(c["direction"], "RAISED")
        self.assertLess(Decimal(c["realization"]["annual_saving"]), 0)  # 42-minute gaps now bridged: more idle than at 30
        self.assertLess(Decimal(c["prediction"]["annual_saving"]), 0)
        self.assertIsNone(c["realized_to_predicted_ratio"])  # no ratio against a predicted cost increase

    def test_serverless_has_no_customer_aws_cost(self):
        (c,) = report({"scenario": "C02", "after": {"auto_stop": 5}, "change_day": "2026-08-20", "change_minute": 16 * 60 + 5})["changes"]
        self.assertEqual((c["aws_status"], c["realization"]["aws_saving"], c["warehouse_type"]), ("NOT_CUSTOMER_BILLED", "0.00", "SERVERLESS"))
        self.assertEqual(c["restart_cost_cluster_minutes"], "0.1")
        self.assertGreater(Decimal(c["realization"]["dbu_saving"]), 0)

    def test_without_a_region_pro_is_dbu_only(self):
        (c,) = report(C13_LOWERED, region=None)["changes"]
        self.assertEqual((c["cost_basis"], c["aws_status"], c["realization"]["aws_saving"]), ("DBU_ONLY", "UNAVAILABLE", None))
        self.assertEqual(c["realization"]["window_saving"], c["realization"]["dbu_saving"])

    def test_a_warehouse_that_never_stopped_is_replayed_running_through_every_gap(self):
        (c,) = report({"scenario": "C01", "after": {"auto_stop": 35}, "change_day": "2026-07-01", "change_minute": 525})["changes"]
        self.assertEqual((c["old_auto_stop_minutes"], c["direction"], c["realization"]["replayed_target_minutes"]), (0, "LOWERED", bt.replay_target(0)))
        self.assertEqual(c["cold_start_share_pct"]["pre"], "0")
        self.assertEqual(c["cross_check"]["outcome"], "AGREES")

    def test_a_size_change_made_with_the_auto_stop_change_is_skipped_and_not_read(self):
        ev = evidence({"scenario": "C39", "after": {"size": "X_SMALL", "auto_stop": 15}, "change_day": "2026-08-12", "change_minute": 600})
        self.assertEqual(ev["warehouses"][0]["changes"], [])
        out = bt.backtest_report(ev, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        self.assertEqual([(s["warehouse_id"], s["old_auto_stop_minutes"], s["new_auto_stop_minutes"], s["reason"]) for s in out["skipped"]],
                         [("wh-demo-027", 30, 15, "OTHER_KNOB_CHANGED")])
        self.assertEqual(out["changes"], [])

    def test_two_changes_of_one_warehouse_are_read_in_two_rounds_with_one_epoch_per_statement(self):
        spec = world.world_spec("C13", {"auto_stop": 10}, change_day="2026-08-01")
        spec["prior"].append({"start": "2026-05-01", "auto_stop": 20})  # 30 -> 20 on 05-01, 20 -> 10 on 08-01 14:32
        rows = copy.deepcopy(world.world_rows(spec, 13, AS_OF))
        for label, items in world.world_rows(world.world_spec("C05", {"auto_stop": 10}, change_day="2026-07-15"), 5, AS_OF).items():
            rows.setdefault(label, []).extend(copy.deepcopy(items))
        statements = []

        class Recording(bridge.DemoWorldClient):
            def query(self, statement, *, label="collector_sql"):
                statements.append((label, statement))
                return super().query(statement, label=label)

        ev = collector.collect_backtest(Recording(rows, AS_OF), as_of=AS_OF, source="DEMO_SIMULATION", history_first_day=HISTORY,
                                        source_region="us-east-1", aws_registry=world.registry())
        out = bt.backtest_report(ev, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        self.assertEqual([(c["warehouse_id"], c["old_auto_stop_minutes"], c["new_auto_stop_minutes"]) for c in out["changes"]],
                         [("wh-demo-005", 45, 10), ("wh-demo-013", 30, 20), ("wh-demo-013", 20, 10)])
        first = out["changes"][1]
        self.assertEqual(first["post_window"]["last_day"], "2026-07-31")  # the closed epoch, to the day before it ended
        labels = [label for label, _ in statements]
        self.assertEqual(labels.count("running_idle"), 4)  # two rounds, pre and post each
        self.assertEqual(labels.count("canary_guard"), 2)
        for label, statement in statements:
            if label == "running_idle":
                epochs = running_idle_request_from_statement(statement)["epochs"]
                self.assertEqual(len(epochs), len(set(epochs)))  # one epoch per warehouse
        self.assertNotIn("SELECT", json.dumps(ev))

    def test_identical_evidence_gives_an_identical_report_and_a_written_file_replays_to_it(self):
        ev = evidence(C13_LOWERED, {"scenario": "C49", "after": {"auto_stop": 20}, "change_day": "2026-06-10", "change_minute": 660})
        first = bt.backtest_report(ev, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps(ev, indent=2, sort_keys=True), encoding="utf-8")
            replayed, written = collector.backtest_from_evidence(path, Path(tmp) / "out")
            self.assertEqual(replayed, first)
            self.assertEqual(written.name, "backtest-20261001T120000Z.json")
            self.assertEqual(json.loads(written.read_text(encoding="utf-8")), first)
        self.assertEqual([c["warehouse_id"] for c in first["changes"]], ["wh-demo-034", "wh-demo-013"])  # by workspace, warehouse
        for c in first["changes"]:
            for block in ("prediction", "realization"):
                self.assertRegex(c[block]["annual_saving"], r"^-?\d+\.\d\d$")

    def test_evidence_that_does_not_hold_the_planned_windows_is_refused(self):
        ev = evidence(C13_LOWERED)
        moved = copy.deepcopy(ev)
        moved["warehouses"][0]["changes"][0]["windows"]["post"]["first_day"] = "2026-08-06"
        with self.assertRaises(InputError):
            bt.backtest_report(moved, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        missing = copy.deepcopy(ev)
        missing["warehouses"][0]["changes"] = []
        with self.assertRaises(InputError):
            bt.backtest_report(missing, supported_types=SUPPORTED_WAREHOUSE_TYPES)
        with self.assertRaises(InputError):
            collector.validate_backtest_evidence({**ev, "backtest_evidence_format_version": "0.9.0"})
        with self.assertRaises(InputError):
            collector.validate_backtest_evidence({**ev, "owner": "someone@example.com"})

    def test_the_summary_has_one_line_per_change(self):
        out = report(C13_LOWERED, {"scenario": "C39", "after": {"size": "X_SMALL", "auto_stop": 15}, "change_day": "2026-08-12", "change_minute": 600})
        lines = bt.summary_lines(out)
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^2234567890123456/wh-demo-013 2026-08-01T14:32:00Z auto-stop 30->10: post 56 days, predicted \d+\.\d\d/yr, "
                                   r"realized \d+\.\d\d/yr USD \(DBU_AND_MODELED_AWS\), cross-check AGREES$")
        self.assertEqual(lines[1], "3234567890123456/wh-demo-027 2026-08-12T10:00:00Z auto-stop 30->15: skipped OTHER_KNOB_CHANGED")


class BridgeFleetTests(unittest.TestCase):
    def test_selectors_choose_warehouses_and_one_that_names_none_is_refused(self):
        client = bridge.demo_fleet_client([C13_LOWERED, {"scenario": "C02", "after": {"auto_stop": 5}, "change_day": "2026-08-20"}], AS_OF)
        everything = bridge.collect_fleet_history(client, as_of=AS_OF, settle_hours=24)
        self.assertEqual([w["warehouse_id"] for w in everything], ["wh-demo-002", "wh-demo-013"])
        self.assertEqual(everything[0]["billing"]["settled_through"], "2026-09-29")
        named = bridge.collect_fleet_history(client, as_of=AS_OF, settle_hours=24, selectors=collector.parse_selectors(["2234567890123456/wh-demo-013"]))
        self.assertEqual([w["warehouse_id"] for w in named], ["wh-demo-013"])
        with self.assertRaises(InputError) as raised:
            bridge.collect_fleet_history(client, as_of=AS_OF, settle_hours=24, selectors=[(None, "wh-nope")])
        self.assertNotIn("wh-nope", str(raised.exception))
        with self.assertRaises(InputError):
            collector.parse_selectors(["a/b/c"])

    def test_history_first_day_is_a_year_before_the_run_day(self):
        import datetime as dt
        self.assertEqual(bridge.history_first_day(dt.date(2026, 10, 1)), "2025-10-01")


class BacktestEvidenceSchemaTests(unittest.TestCase):
    FILE = COMMITTED / "realization-backtest-evidence.schema.json"

    def test_builder_reproduces_the_committed_schema_file_byte_for_byte(self):
        self.assertTrue(self.FILE.is_file())
        self.assertEqual(serialize(contract.backtest_evidence_schema()), self.FILE.read_bytes())

    def test_schema_is_strict_and_a_simulated_backtest_satisfies_it(self):
        built = contract.backtest_evidence_schema()
        jsonschema.Draft202012Validator.check_schema(built)
        self.assertIs(built["additionalProperties"], False)
        self.assertEqual(built["required"], sorted(built["properties"]))
        validator = jsonschema.Draft202012Validator(built)
        ev = evidence(C13_LOWERED)
        self.assertTrue(validator.is_valid(ev))
        self.assertFalse(validator.is_valid({**ev, "tags": {"team": "x"}}))
        held = copy.deepcopy(ev)
        held["warehouses"][0]["owner"] = "someone@example.com"
        self.assertFalse(validator.is_valid(held))


if __name__ == "__main__":
    unittest.main()
