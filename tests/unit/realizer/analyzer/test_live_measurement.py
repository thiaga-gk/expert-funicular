"""The live measurement path: plan the windows, collect them through the Publisher's SQL, project an observation.

``realizer.analyzer.measurement`` (which epochs and whole UTC days), ``realizer.analyzer.collector.bridge`` (the
Publisher SQL, read-only access), ``realizer.analyzer.collector.collector.collect_measurement`` (two passes into the
measurement evidence format) and ``realizer.analyzer.projector`` (evidence -> observation 3.0.0, with the auto-stop
replay counterfactual, or the billed change before and after a change without a replay). Worlds are simulated demo-fleet warehouses changed after their package (``tests/realizer_world``).
"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from publisher.analyzer.collector.queries.events import NEVER_STOP_TARGET_MINUTES
from realizer.analyzer.collector import collector
from realizer.analyzer import measurement as m
from realizer.analyzer.analysis import candidate_steps
from realizer.analyzer.collector import bridge
from realizer.analyzer.projector import project_observation
from realizer.cli.main import EXIT_REFUSED, main
from realizer.common.errors import InputError
from realizer.config.loader import validate_measurement_evidence, validate_observation

PRO = world.package("autostop-scoped-pro")
SERVERLESS = world.package("autostop-scoped-serverless")
NEVER = world.package("autostop-scoped-never-stop")


def epoch(start, end=None, **cfg):
    base = {"warehouse_type": "PRO", "warehouse_channel": "CURRENT", "warehouse_size": "SMALL", "min_clusters": 1, "max_clusters": 2,
            "auto_stop_minutes": 30}
    return {"epoch_start": start, "epoch_end": end, **base, **cfg}


class PlanTests(unittest.TestCase):
    """Which epochs and whole UTC days a measurement reads (the package: Pro, auto-stop 30 to 10, generated 2026-10-01)."""

    def plan(self, epochs, settled="2026-11-05"):
        return m.plan_measurement(PRO, epochs, settled)

    def test_nothing_to_measure_until_the_step_knob_changes(self):
        plan = self.plan([epoch("2025-07-01T00:00:00+00:00")])
        self.assertEqual((plan.reason, plan.post, plan.step), ("NOT_APPLIED", None, None))

    def test_whole_weeks_after_the_change_day_and_as_many_before_it(self):
        plan = self.plan([epoch("2025-07-01T00:00:00+00:00", "2026-10-08T14:32:00+00:00"), epoch("2026-10-08T14:32:00+00:00", auto_stop_minutes=10)])
        self.assertIsNone(plan.reason)
        self.assertEqual((plan.post["first_day"], plan.post["last_day"]), ("2026-10-09", "2026-11-05"))  # 4 weeks, change day excluded
        self.assertEqual((plan.pre["first_day"], plan.pre["last_day"]), ("2026-09-10", "2026-10-07"))
        self.assertEqual(plan.settled_days_since_change, 28)
        self.assertEqual(plan.windows()["post"]["targets"], [30])  # the package's source value replayed after the change
        self.assertEqual(plan.windows()["pre"]["targets"], [10])  # the applied value on the days before it

    def test_a_partial_week_waits_and_long_windows_are_the_latest_twelve_weeks(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-10-08T14:32:00+00:00"), epoch("2026-10-08T14:32:00+00:00", auto_stop_minutes=10)]
        self.assertEqual(self.plan(epochs, "2026-10-14").reason, "NOT_SETTLED")  # six whole days
        self.assertEqual(self.plan(epochs, None).reason, "NOT_SETTLED")  # nothing settled
        late = self.plan(epochs, "2027-03-01")
        self.assertEqual(m.window_days(late.post), 84)
        self.assertEqual(late.post["last_day"], "2027-03-01")
        self.assertEqual(m.window_days(late.pre), 84)

    def test_a_short_epoch_before_the_change_limits_only_the_pre_window(self):
        epochs = [epoch("2026-09-28T00:00:00+00:00", "2026-10-08T14:32:00+00:00"), epoch("2026-10-08T14:32:00+00:00", auto_stop_minutes=10)]
        plan = self.plan(epochs)
        self.assertEqual((m.window_days(plan.post), m.window_days(plan.pre)), (28, 7))
        none_before = self.plan([epoch("2026-10-05T00:00:00+00:00", "2026-10-08T14:32:00+00:00"), epoch("2026-10-08T14:32:00+00:00", auto_stop_minutes=10)])
        self.assertIsNone(none_before.pre)

    def test_a_later_change_of_another_knob_is_measured_in_the_epoch_in_effect(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-10-08T14:32:00+00:00"),
                  epoch("2026-10-08T14:32:00+00:00", "2026-10-12T09:00:00+00:00", auto_stop_minutes=10),
                  epoch("2026-10-12T09:00:00+00:00", auto_stop_minutes=10, warehouse_size="X_SMALL")]
        plan = self.plan(epochs)
        self.assertEqual(plan.change_epoch["epoch_start"], "2026-10-08T14:32:00+00:00")  # the auto-stop change
        self.assertEqual(plan.post["epoch_start"], "2026-10-12T09:00:00+00:00")  # measured in the epoch in effect
        self.assertEqual(plan.post["first_day"], "2026-10-16")  # the latest three whole weeks after 10-12
        self.assertEqual(plan.pre["last_day"], "2026-10-07")
        self.assertEqual(plan.other_knob_changes, ("warehouse_size",))

    def test_a_closed_epoch_is_measured_to_the_day_before_it_ended(self):
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-10-08T14:32:00+00:00"),
                  epoch("2026-10-08T14:32:00+00:00", "2026-10-30T10:00:00+00:00", auto_stop_minutes=10)]
        # The as-of time is inside the closed epoch's successor only when it is observed; here the last epoch observed is the
        # changed one, closed by a change the evidence does not hold yet, so its last whole day is 2026-10-29.
        plan = self.plan(epochs)
        self.assertEqual((plan.post["first_day"], plan.post["last_day"]), ("2026-10-09", "2026-10-29"))  # three whole weeks

    def test_a_knob_without_a_replay_is_observed_not_measured(self):
        size_package = world.package("inplace-size-canary")
        cfg = {k: size_package["current_config"][k] for k in ("warehouse_type", "warehouse_channel", "min_clusters", "max_clusters", "auto_stop_minutes")}
        step = next(s for s in candidate_steps(size_package) if s["knob"] == "warehouse_size")
        epochs = [epoch("2025-07-01T00:00:00+00:00", "2026-10-08T14:32:00+00:00", **cfg, warehouse_size=step["current_value"]),
                  epoch("2026-10-08T14:32:00+00:00", **cfg, warehouse_size=step["recommended_value"])]
        plan = m.plan_measurement(size_package, epochs, "2026-11-05")
        self.assertEqual((plan.reason, plan.step["knob"], plan.replayed_value), ("KNOB_NOT_REPLAYED", "warehouse_size", None))
        # The same windows as a replayed knob (whole weeks after the change day, as many before it), without replay values.
        windows = plan.windows()
        self.assertEqual((windows["post"]["first_day"], windows["post"]["last_day"], windows["post"]["targets"]), ("2026-10-09", "2026-11-05", []))
        self.assertEqual((windows["pre"]["first_day"], windows["pre"]["last_day"], windows["pre"]["targets"]), ("2026-09-10", "2026-10-07", []))
        self.assertEqual(plan.settled_days_since_change, 28)
        # Before a whole settled week it is still not replayed, with no window yet.
        early = m.plan_measurement(size_package, epochs, "2026-10-14")
        self.assertEqual((early.reason, early.post, early.pre), ("KNOB_NOT_REPLAYED", None, None))

    def test_never_stop_is_replayed_as_a_value_longer_than_any_gap(self):
        self.assertEqual(m.replay_target(0), NEVER_STOP_TARGET_MINUTES)
        self.assertEqual(m.replay_target(35), 35)


class SimulatedWorldTests(unittest.TestCase):
    """Collected through the Publisher SQL on simulated worlds and projected into observations."""

    def test_pro_auto_stop_30_to_10_realizes_dbu_and_modeled_aws(self):
        spec = world.world_spec("C13", {"auto_stop": 10})
        evidence = world.measure(PRO, spec, 13)
        validate_measurement_evidence(evidence)
        obs = project_observation(PRO, evidence)
        validate_observation(obs, PRO)
        self.assertEqual(obs["applied_at_utc"], "2026-10-08T14:32:00Z")
        self.assertEqual((obs["window_start_utc"], obs["window_end_utc"]), ("2026-10-09T00:00:00Z", "2026-11-06T00:00:00Z"))
        self.assertEqual((obs["elapsed_days"], obs["query_count"], obs["representative_regime"], obs["evidence_quality"]), (28, 280, True, "SUFFICIENT"))
        e, meas = obs["economics"], obs["measurement"]
        # 280 gaps of 42 minutes: the warehouse now stops 10 minutes in; with 30 it would run 20 more per gap = 5,600 cluster-minutes.
        self.assertEqual(meas["replay"]["added_idle_cluster_minutes"], "5600")
        self.assertEqual(meas["post_window"]["running_cluster_minutes"], "3640")
        self.assertEqual(meas["post_window"]["billed_cost"], "396.25")
        self.assertEqual(e["dbu_realized"], "609.61")  # 5,600 x 396.25 / 3,640
        self.assertEqual(e["aws_economic_realized"], "349.44")  # 5,600 / 60 x 3.744 per cluster-hour
        self.assertEqual(Decimal(e["counterfactual_old_cost"]) - Decimal(e["observed_new_cost"]), Decimal("959.05"))
        self.assertEqual((e["counterfactual_method"], e["aws_evidence_type"], meas["cost_basis"]), ("CALIBRATED_PRIOR_STATE", "MODELED", "DBU_AND_MODELED_AWS"))
        self.assertEqual((meas["pre_window"]["first_day"], meas["volume_ratio"]), ("2026-09-10", "1"))
        self.assertEqual(evidence["guard"]["CURRENT"]["queries"], 280)

    def test_serverless_has_no_customer_aws_cost(self):
        evidence = world.measure(SERVERLESS, world.world_spec("C02", {"auto_stop": 5}), 2)
        obs = project_observation(SERVERLESS, evidence)
        self.assertEqual(evidence["aws"]["post"]["status"], "NOT_CUSTOMER_BILLED")
        self.assertEqual(obs["economics"]["aws_economic_realized"], "0.00")
        self.assertGreater(Decimal(obs["economics"]["dbu_realized"]), 0)
        self.assertIs(obs["financial_quality"]["material_cost_component_available"], True)

    def test_a_warehouse_that_never_stopped_is_replayed_running_through_every_gap(self):
        evidence = world.measure(NEVER, world.world_spec("C01", {"auto_stop": 35}), 1)
        obs = project_observation(NEVER, evidence)
        replay = obs["measurement"]["replay"]
        self.assertEqual(replay["replayed_target_minutes"], NEVER_STOP_TARGET_MINUTES)
        post = obs["measurement"]["post_window"]
        # Never stopping, it runs every gap minute: the replay adds the idle and saves each restart (4 cluster-minutes each).
        self.assertEqual(Decimal(replay["avoided_starts"]), Decimal(post["restarts"]))
        self.assertGreater(Decimal(obs["economics"]["dbu_realized"]), Decimal("6000"))

    def test_without_an_aws_price_the_realization_is_dbu_only_and_not_a_complete_cost(self):
        evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13, aws_registry={})
        obs = project_observation(PRO, evidence)
        self.assertEqual(evidence["aws"]["post"]["status"], "UNAVAILABLE")
        self.assertEqual(obs["measurement"]["cost_basis"], "DBU_ONLY")
        self.assertIsNone(obs["economics"]["aws_economic_realized"])
        self.assertIs(obs["financial_quality"]["material_cost_component_available"], False)

    def test_before_a_whole_settled_week_there_is_no_measurement(self):
        evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13, as_of="2026-10-14T12:00:00+00:00")
        obs = project_observation(PRO, evidence)
        self.assertEqual(obs["measurement"]["not_measured_reason"], "NOT_SETTLED")
        self.assertIsNone(obs["economics"])
        self.assertEqual(obs["applied_at_utc"], "2026-10-08T14:32:00Z")
        self.assertEqual((obs["evidence_quality"], obs["window_start_utc"]), ("INSUFFICIENT", None))

    def test_unchanged_warehouse_is_not_applied(self):
        unchanged = world.world_spec("C13", {"auto_stop": 30})
        obs = project_observation(PRO, world.measure(PRO, unchanged, 13))
        self.assertIsNone(obs["applied_at_utc"])
        self.assertIsNone(obs["measurement"])
        self.assertIsNone(obs["economics"])

    def test_evidence_must_belong_to_the_package_and_hold_the_planned_windows(self):
        evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13)
        other = dict(evidence, warehouse_id="wh-other")
        with self.assertRaises(InputError):
            project_observation(PRO, other)
        moved = copy.deepcopy(evidence)
        moved["windows"]["post"]["first_day"] = "2026-10-10"
        with self.assertRaises(InputError):
            project_observation(PRO, moved)


class ReplayDirectionTests(unittest.TestCase):
    """The saving of a change, replayed in either direction on the days run at the applied value."""

    def test_both_directions(self):
        from realizer.analyzer.projector import replay_net_cluster_minutes
        row = {"added_idle_cluster_minutes": Decimal("5600"), "avoided_starts": Decimal("3"), "removable_running_cluster_minutes": Decimal("900"),
               "additional_starts": Decimal("20")}
        self.assertEqual(replay_net_cluster_minutes(row, 30, 10, Decimal("4")), Decimal("5588"))  # lowered: the old value adds idle
        self.assertEqual(replay_net_cluster_minutes(row, 0, 35, Decimal("4")), Decimal("5588"))  # never stopped before
        self.assertEqual(replay_net_cluster_minutes(row, 10, 15, Decimal("4")), Decimal("-820"))  # raised: the old value would save idle, add restarts
        self.assertEqual(replay_net_cluster_minutes(row, 10, 0, Decimal("4")), Decimal("-820"))  # set to never stop

    def test_a_raised_auto_stop_realizes_a_cost_increase(self):
        spec = world.world_spec("C13", {"auto_stop": 10})
        raised = world.world_spec("C13", {"auto_stop": 45})
        pkg = copy.deepcopy(PRO)  # the package says 30 -> 10; the owner raises to 45 instead (contrary)
        obs = project_observation(pkg, world.measure(pkg, raised, 13))
        self.assertEqual(obs["measurement"]["applied_value"], 45)
        self.assertLess(Decimal(obs["economics"]["dbu_realized"]), 0)  # 42-minute gaps now bridged: more idle than at 30
        self.assertIsNotNone(spec)


class UnreplayedChangeTests(unittest.TestCase):
    """A changed knob without a replay: observed over the same windows (guards, billed change), never measured."""

    def test_billed_change_is_post_minus_the_pre_window_at_post_volume_and_length(self):
        from realizer.analyzer.projector import billed_pre_post_change
        # Pre: 14 days, 1,400 billed, 2,800 running cluster-minutes at 2/cluster-hour. Post: 7 days, 900 billed, 1,680 minutes
        # at 4/cluster-hour, 1.2 x the pre-change queries per day. Expected without the change: 100/day x 1.2 x 7 = 840 DBU
        # cost and 2,800 / 60 x 2 / 14 x 1.2 x 7 = 56 AWS; observed 900 and 1,680 / 60 x 4 = 112.
        change = billed_pre_post_change(pre_days=14, pre_cost=Decimal("1400"), pre_running=Decimal("2800"), post_days=7,
                                        post_cost=Decimal("900"), post_running=Decimal("1680"), volume_ratio=Decimal("1.2"),
                                        pre_aws_hourly=Decimal("2"), post_aws_hourly=Decimal("4"))
        self.assertEqual((change["dbu"], change["aws_modeled"], change["total"]), (Decimal("60"), Decimal("56"), Decimal("116")))
        self.assertEqual(change["annualized_dbu"].quantize(Decimal("0.01")), Decimal("3128.57"))  # 60 x 365 / 7
        self.assertEqual(change["annualized_total"].quantize(Decimal("0.01")), Decimal("6048.57"))
        cheaper = billed_pre_post_change(pre_days=14, pre_cost=Decimal("1400"), pre_running=Decimal("2800"), post_days=7,
                                         post_cost=Decimal("700"), post_running=Decimal("1680"), volume_ratio=Decimal("1"),
                                         pre_aws_hourly=None, post_aws_hourly=Decimal("4"))
        self.assertEqual(cheaper["dbu"], Decimal("0"))  # 700 - 100/day x 7: no change
        self.assertEqual((cheaper["aws_modeled"], cheaper["total"], cheaper["annualized_total"]), (None, None, None))  # one AWS price only

    def test_a_size_cut_is_observed_with_guards_and_its_billed_change_priced_at_each_size(self):
        size_package = world.package("inplace-size-canary")  # Pro LARGE to MEDIUM, step 1
        evidence = world.measure(size_package, world.world_spec("C06", {"size": "MEDIUM"}), 6)
        validate_measurement_evidence(evidence)
        obs = project_observation(size_package, evidence)
        validate_observation(obs, size_package)
        meas = obs["measurement"]
        self.assertEqual((meas["knob"], meas["method"], meas["not_measured_reason"], meas["holds_other_knobs"]),
                         ("warehouse_size", None, "KNOB_NOT_REPLAYED", False))
        self.assertIsNone(obs["economics"])
        self.assertIsNone(meas["replay"])
        self.assertIsNone(meas["cross_check"])
        self.assertEqual((meas["pre_window"]["first_day"], meas["pre_window"]["last_day"]), ("2026-09-10", "2026-10-07"))
        self.assertEqual((meas["post_window"]["first_day"], meas["post_window"]["last_day"]), ("2026-10-09", "2026-11-05"))
        self.assertIsNotNone(meas["volume_ratio"])
        guards = obs["performance"]["guards"]
        self.assertIsNotNone(guards["pre"]["p95_execution_ms_shared_sources"])
        self.assertIsNotNone(guards["post"]["queued_query_pct"])
        self.assertEqual(guards["cold_start"]["post_queries"], meas["post_window"]["queries"])
        # The AWS price of each window's own size: the pre-change epoch's LARGE, the observed epoch's MEDIUM.
        self.assertEqual(evidence["aws"]["pre"]["cluster_hourly_price"], bridge.aws_price("PRO", "LARGE", "us-east-1", world.registry())["cluster_hourly_price"])
        self.assertEqual(evidence["aws"]["post"]["cluster_hourly_price"], bridge.aws_price("PRO", "MEDIUM", "us-east-1", world.registry())["cluster_hourly_price"])
        change = meas["billed_change"]
        self.assertEqual((change["basis"], change["days"], change["currency"]), ("BILLED_PRE_POST_AT_PRE_MIX", 28, "USD"))
        self.assertLess(Decimal(change["dbu"]), 0)  # a smaller size bills less
        self.assertLess(Decimal(change["aws_modeled"]), 0)
        self.assertEqual(Decimal(change["total"]), Decimal(change["dbu"]) + Decimal(change["aws_modeled"]))
        self.assertEqual(obs["evidence_quality"], "SUFFICIENT")

    def test_without_an_aws_price_the_billed_change_is_dbu_only(self):
        size_package = world.package("inplace-size-canary")
        obs = project_observation(size_package, world.measure(size_package, world.world_spec("C06", {"size": "MEDIUM"}), 6, aws_registry={}))
        change = obs["measurement"]["billed_change"]
        self.assertIsNotNone(change["dbu"])
        self.assertEqual((change["aws_modeled"], change["total"], change["annualized_total"]), (None, None, None))

    def test_a_replayed_knob_keeps_billed_change_null(self):
        obs = project_observation(PRO, world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13))
        self.assertIsNone(obs["measurement"]["billed_change"])

    def test_before_a_whole_settled_week_a_change_without_a_replay_has_no_window(self):
        size_package = world.package("inplace-size-canary")
        obs = project_observation(size_package, world.measure(size_package, world.world_spec("C06", {"size": "MEDIUM"}), 6,
                                                              as_of="2026-10-14T12:00:00+00:00"))
        meas = obs["measurement"]
        self.assertEqual(meas["not_measured_reason"], "KNOB_NOT_REPLAYED")
        self.assertEqual((meas["post_window"], meas["billed_change"], obs["performance"]["guards"]), (None, None, None))


class BridgeTests(unittest.TestCase):
    def test_an_epoch_row_without_a_start_is_skipped_not_fatal(self):
        rows = [{"epoch_start": "2026-10-08T14:32:00+00:00", "epoch_end": None, "auto_stop_minutes": 10},
                {"epoch_start": None, "epoch_end": None, "auto_stop_minutes": 99},
                {"epoch_start": "2025-07-01T00:00:00+00:00", "epoch_end": "2026-10-08T14:32:00+00:00", "auto_stop_minutes": 30}]
        with self.assertLogs("realizer", level="WARNING"):
            epochs = bridge._observed_epochs(rows, "2026-11-07T12:00:00+00:00")
        self.assertEqual([e["auto_stop_minutes"] for e in epochs], [30, 10])

    def test_the_collector_never_runs_on_the_warehouse_it_measures(self):
        with mock.patch.object(bridge.access, "resolve_cli_token") as token, self.assertRaises(InputError):
            bridge.resolve_live_access("prod", "wh-demo-013", ["wh-demo-013"])
        token.assert_not_called()  # refused before any credential is touched
        with self.assertRaises(InputError):
            bridge.resolve_live_access("prod", "", ["wh-demo-013"])

    def test_assumptions_take_the_package_profile_values(self):
        values = bridge.assumptions(PRO["cost_profile"]["applied_thresholds"])
        self.assertEqual(values["restart_cost_cluster_minutes_pro_classic"], 4.0)
        self.assertEqual(values["canary_p95_regression_pct"], 25.0)
        aggressive = bridge.assumptions({"cross_technique": {"canary_p95_regression_pct": 40.0}, "auto_stop": {"cold_start_added_share_max_pct": 10.0}})
        self.assertEqual((aggressive["canary_p95_regression_pct"], aggressive["cold_start_added_share_max_pct"]), (40.0, 10.0))

    def test_settled_billing_day_is_the_publishers_rule(self):
        day, source, _ = bridge.settled_through("2026-11-07T12:00:00+00:00", "2026-11-07T06:00:00+00:00", "2026-11-07T06:00:00+00:00", None, 12)
        self.assertEqual((day, source), ("2026-11-06", "WORKSPACE"))
        lag, _, _ = bridge.settled_through("2026-11-07T12:00:00+00:00", "2026-11-03T06:00:00+00:00", "2026-11-07T06:00:00+00:00", None, 12)
        self.assertEqual(lag, "2026-11-02")  # the workspace's own watermark bounds it
        self.assertEqual(bridge.settled_through("2026-11-07T12:00:00+00:00", None, None, None, 12)[0], None)

    def test_aws_price_is_the_publishers_topology_and_registry(self):
        self.assertEqual(bridge.aws_price("PRO", "SMALL", "us-east-1", world.registry())["cluster_hourly_price"], 3.744)
        self.assertEqual(bridge.aws_price("PRO", "SMALL", None, world.registry())["blocker"], "SOURCE_REGION_NOT_GIVEN")
        self.assertEqual(bridge.aws_price("PRO", "SMALL", "mars-1", world.registry())["status"], "UNAVAILABLE")
        with self.assertRaises(InputError):
            bridge.load_registry("/nonexistent/registry.json")

    def test_only_the_bridge_and_config_import_the_publisher(self):
        importers = sorted(str(p.relative_to(ROOT / "src")) for p in (ROOT / "src" / "realizer").rglob("*.py")
                           if "from publisher" in p.read_text(encoding="utf-8") or "import publisher" in p.read_text(encoding="utf-8"))
        self.assertEqual(importers, ["realizer/analyzer/collector/bridge.py", "realizer/config/loader.py", "realizer/config/settings.py"])


class EvidenceReplayCliTests(unittest.TestCase):
    """A live run writes its measurement evidence; ``--from-evidence`` replays it to the same realization."""

    def run_cli(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(list(argv) + ["--log-level", "ERROR"])
        return code, out.getvalue(), err.getvalue()

    def test_replayed_evidence_realizes_the_package(self):
        evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            code, out, err = self.run_cli("--package", str(world.PACKAGES / "autostop-scoped-pro.json"), "--from-evidence", str(path),
                                          "--output-dir", str(Path(tmp) / "out"), "--print-json")
            self.assertEqual(code, 0, err)
            snapshot = json.loads(out)
            self.assertEqual(snapshot["application"]["implementation_conformance"], "EXACT")
            self.assertEqual(snapshot["economics"]["observed_economic_delta"], "959.05")

    def test_live_measurement_writes_its_evidence_before_projecting(self):
        evidence = world.measure(PRO, world.world_spec("C13", {"auto_stop": 10}), 13)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(collector.bridge, "resolve_live_access", return_value=object()), \
                mock.patch.object(collector.bridge, "LiveSqlClient") as client, \
                mock.patch.object(collector, "collect_measurement", return_value=evidence):
            client.return_value.__enter__.return_value = object()
            obs = collector.measure_live(PRO, Path(tmp), profile="p", sql_warehouse_id="wh-collector", as_of=world.AS_OF)
            written = sorted(Path(tmp, "realization", "measurements").glob("*.json"))
            self.assertEqual([p.name for p in written], ["2234567890123456__wh-demo-013__20261107T120000Z.json"])
            self.assertEqual(json.loads(written[0].read_text()), evidence)
            self.assertEqual(obs["economics"]["dbu_realized"], "609.61")

    def test_one_observation_source_per_run(self):
        code, _, err = self.run_cli("--package", str(world.PACKAGES / "autostop-scoped-pro.json"), "--demo", "--from-evidence", "x.json")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("choose one observation source", err)

    def test_a_live_run_on_the_measured_warehouse_is_refused_with_status_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli("--package", str(world.PACKAGES / "autostop-scoped-pro.json"), "--profile", "p",
                                        "--sql-warehouse-id", "wh-demo-013", "--output-dir", tmp)
            self.assertEqual(code, EXIT_REFUSED)
            self.assertIn("cannot run on the warehouse it measures", err)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_an_evidence_file_in_another_format_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.json"
            path.write_text(json.dumps({"measurement_evidence_format_version": "0.9"}), encoding="utf-8")
            code, _, err = self.run_cli("--package", str(world.PACKAGES / "autostop-scoped-pro.json"), "--from-evidence", str(path), "--output-dir", tmp)
            self.assertEqual(code, EXIT_REFUSED)
            self.assertIn("unsupported measurement evidence format", err)


if __name__ == "__main__":
    unittest.main()
