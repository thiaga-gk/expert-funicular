"""Shared fixture and helpers for the capability case modules under tests/unit/ and tests/component/."""
import copy
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.collector import extract_evidence  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.analyzer.collector.workspace_api import AuthContext  # noqa: E402
from publisher.analyzer.forecast import Modeler  # noqa: E402
from publisher.common.time import parse_datetime  # noqa: E402
from publisher.contracts.scenario import PlanStep  # noqa: E402
from publisher.contracts.source import WarehouseConfig  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.packager.estimator.package import _package_economics  # noqa: E402
from tests.evidence_helpers import move_current_epoch_start  # noqa: E402
from tests.golden import load_case  # noqa: E402


class PublisherCase(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()
        self.evidence = demo_evidence()
        self.results = p.evaluate_warehouses(self.evidence, self.t)
        self.w = self.results[0]
        self.by_knob = {c.knob: c for c in self.w.candidates}
        self.packages = p.build_recommendation_packages(self.evidence, self.t)

    # --- audit H1: steps marked PENDING_REEVALUATION must not feed authoritative scenario economics ----------
    def plan_a(self):
        return next(s for s in self.w.scenarios if s.name.startswith("Plan A"))

    def step_for(self, knob, state):
        c = self.by_knob[knob]
        return PlanStep(1, c.recommendation_id, knob, c.current_config, c.recommended_config, c.status,
                          "GATE", 0, c.invalidates, c.dependencies, state), c

    # --- Task-time utilization: evidence only, no decision effect ----------------------------------------------
    def _utilization(self, by, knob):
        return {f.metric: f.value for f in by[knob].findings}.get("task_minutes_per_cluster_minute")

    # --- Missing pressure evidence fails closed --------------------------------------------------------------
    PRESSURE_FIELDS = ("spill_query_pct_30", "queued_query_pct_30", "p95_execution_ms_30", "p99_execution_ms_30")

    def _without_pressure_evidence(self, fields=PRESSURE_FIELDS):
        e = copy.deepcopy(self.evidence[0])
        for k in fields:
            e.current_epoch.query[k] = None
        return e

    def _fleet_with_one_unsupported(self):
        fleet = synthetic_demo_evidence(3)
        fleet[1].current_epoch.config = WarehouseConfig("LAKEHOUSE_REAL_TIME", "CURRENT", "MEDIUM", 1, 1, 10)
        return fleet

    def _cost_rows(self, days, start="2026-09-01", cost=lambda i: 100.0):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        return [{"usage_date": (d0 + dt.timedelta(days=i)).isoformat(), "list_cost": cost(i), "currency_code": "USD"} for i in range(days)]

    def candidate_of(self, evidence, knob):
        return next(c for c in p.evaluate_warehouses(evidence, self.t)[0].candidates if c.knob == knob)

    def size_candidate_with_failure_rate(self, value, *, drop=False):
        ev = copy.deepcopy(self.evidence)
        q = ev[0].current_epoch.query
        if drop:
            q.pop("failure_cancel_pct_30", None)
        else:
            q["failure_cancel_pct_30"] = value
        return self.candidate_of(ev, "warehouse_size")

    def serverless_size_evidence(self, size, golden="serverless", **query):
        payload = load_case(golden)
        ev = payload["evidence"][0]
        for ep in ev["epochs"]:
            ep["config"]["warehouse_size"] = size
            ep["config"]["warehouse_type"] = "SERVERLESS"
        next(x for x in ev["epochs"] if x["is_current"])["query"].update(query)
        return [evidence_from_dict(ev)]

    def with_cost_history(self, days, golden=None):
        if golden:
            payload = load_case(golden)
            ev = [evidence_from_dict(payload["evidence"][0])]
        else:
            ev = copy.deepcopy(self.evidence)
        ev[0].cost_history_days = days
        return ev

    # --- M7: billing window boundary and completeness watermark -------------------------------------------------
    ANALYSIS = "2026-10-01T12:00:00+00:00"

    def forecast(self, rows, watermark="UNSET", start="2026-09-01T00:00:00+00:00"):
        # Window/watermark semantics are tested without the settle buffer; the buffer has its own tests.
        kwargs = {} if watermark == "UNSET" else {"billing_watermark": watermark}
        return Modeler(self.unsettled()).forecast_from_daily_cost(rows, start, self.ANALYSIS, **kwargs)

    def unsettled(self):
        import dataclasses
        return dataclasses.replace(self.t, billing_settle_hours=0)

    # --- Billing corrections: share of originally billed DBU later retracted (evidence only) -------------------
    def correction_rows(self, days=30, first="2026-09-01", retracted=None, restated=None):
        rows = self._cost_rows(days, first)
        for r in rows:
            r["dbu_quantity"] = 100.0
            r["retracted_dbu_quantity"] = 0.0
            r["restated_dbu_quantity"] = 0.0
        for day, qty in (retracted or {}).items():
            row = next(r for r in rows if r["usage_date"] == day)
            row["retracted_dbu_quantity"] = qty; row["dbu_quantity"] -= qty
        for day, qty in (restated or {}).items():
            row = next(r for r in rows if r["usage_date"] == day)
            row["restated_dbu_quantity"] = qty; row["dbu_quantity"] += qty
        return rows

    def with_correction_share(self, share):
        e = copy.deepcopy(self.evidence[0])
        e.forecast_metadata = {**e.forecast_metadata, "billing_correction_dbu_share_365": share}
        return e

    def _rows_with_unpriced(self, unpriced_dates):
        rows = [{"usage_date": f"2026-09-{d:02d}", "list_cost": 100.0, "dbu_quantity": 200.0, "unpriced_dbu_quantity": 0.0, "currency_code": "USD"}
                for d in range(1, 31)]
        rows += [{"usage_date": d, "list_cost": None, "dbu_quantity": 150.0, "unpriced_dbu_quantity": 150.0, "currency_code": None}
                 for d in unpriced_dates]
        return rows

    def _fake_connector(self, timezone):
        import types
        calls = {}

        class Cursor:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def execute(self_inner, statement): calls.setdefault("statements", []).append(statement)
            description = [("tz",)]
            def fetchall(self_inner): return [(timezone,)]

        class Connection:
            def cursor(self_inner): return Cursor()
            def close(self_inner): calls["closed"] = True

        def connect(**kwargs):
            calls["connect"] = kwargs
            return Connection()

        sql_mod = types.ModuleType("databricks.sql"); sql_mod.connect = connect
        pkg = types.ModuleType("databricks"); pkg.sql = sql_mod
        return calls, {"databricks": pkg, "databricks.sql": sql_mod}

    def _auth(self):
        return AuthContext("DEFAULT", "host", "/sql/1.0/warehouses/x", "token", None, "warehouse", "x")

    # --- A high or unknown failure share blocks every capacity cut ---------------------------------------------
    def _with_failure(self, pct):
        e = copy.deepcopy(self.evidence[0])
        e.current_epoch.query["failure_cancel_pct_30"] = pct
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}

    # --- Max-cluster increase has the same maturity and sample gates as every change --------------------------
    def _queueing_at_ceiling(self, age_days, queries):
        payload = load_case("concurrency-constrained")
        e = evidence_from_dict(payload["evidence"][0])
        start = (parse_datetime(e.analysis_time) - __import__("datetime").timedelta(days=age_days)).isoformat()
        move_current_epoch_start(e, start)
        e.current_epoch.query.update(queries_30=queries)
        return {c.knob: c for c in p.evaluate_warehouses([e], self.t)[0].candidates}["max_clusters"]

    def _sparse(self, cost_history_days):
        e = copy.deepcopy(self.evidence[0])
        e.baseline_forecast_cost_365 = None
        e.cost_history_days = cost_history_days
        e.forecast_metadata = dict(e.forecast_metadata, dbu_forecast_status="INSUFFICIENT_ACTIVE_COST_DAYS")
        return e

    # --- Billing settlement: settle buffer, per-workspace watermark, ingestion cut-off ----------------------------
    def _daily(self, last_day="2026-09-30"):
        import datetime as dt
        end = dt.date.fromisoformat(last_day)
        return [{"usage_date": (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat(), "list_cost": 100.0, "currency_code": "USD"}
                for i in range((end - dt.date(2026, 9, 1)).days + 1)]

    # --- Forecast basis is trimmed to whole weeks (no day-of-week bias) ---------------------------------------
    def _weekday_only(self, start, days):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        return [{"usage_date": (d0 + dt.timedelta(days=i)).isoformat(), "list_cost": 100.0 if (d0 + dt.timedelta(days=i)).weekday() < 5 else 0.0,
                 "currency_code": "USD"} for i in range(days)]

    def _two_workspace_client(self, seen):
        def epoch(ws):
            return {"workspace_id": ws, "warehouse_id": "w", "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                    "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 1, "max_clusters": 2,
                    "auto_stop_minutes": 10, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}
        test = self

        class Client:
            def query(self_inner, statement, *, label="collector_sql"):
                seen[label] = statement
                if label == "config_epochs":
                    return [epoch("ws-fresh"), epoch("ws-lagging")]
                if label == "billing_watermark":
                    return [{"workspace_id": "ws-fresh", "billing_watermark": "2026-10-03T06:00:00+00:00", "billing_ingested_through": "2026-10-03"},
                            {"workspace_id": "ws-lagging", "billing_watermark": "2026-09-25T06:00:00+00:00", "billing_ingested_through": "2026-09-25"}]
                if label == "billing_daily_cost":
                    return [dict(r, workspace_id=ws, warehouse_id="w", epoch_start="2026-09-01T00:00:00+00:00")
                            for ws in ("ws-fresh", "ws-lagging") for r in test._daily()]
                if label == "resource_daily":  # both warehouses kept running: missing billing is lag, not idleness
                    return [{"workspace_id": ws, "warehouse_id": "w", "epoch_start": "2026-09-01T00:00:00+00:00", "usage_day": r["usage_date"],
                             "uptime_minutes": 60.0, "cluster_minutes": 60.0} for ws in ("ws-fresh", "ws-lagging") for r in test._daily("2026-10-02")]
                return []
        return Client()

    def _idle_workspace_evidence(self, running_after_last_usage):
        import datetime as dt
        def epoch(ws):
            return {"workspace_id": ws, "warehouse_id": "w-" + ws, "account_id": "a", "warehouse_name": "W", "warehouse_type": "PRO",
                    "warehouse_channel": "CHANNEL_NAME_CURRENT", "warehouse_size": "MEDIUM", "min_clusters": 1, "max_clusters": 2,
                    "auto_stop_minutes": 10, "epoch_start": "2026-09-01T00:00:00+00:00", "epoch_end": None, "is_current": True, "tags": {}}
        days = [(dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat() for i in range(33)]
        resource = [{"workspace_id": "quiet", "warehouse_id": "w-quiet", "epoch_start": "2026-09-01T00:00:00+00:00", "usage_day": d,
                     "uptime_minutes": 60.0 if (d <= "2026-09-20" or running_after_last_usage) else 0.0, "cluster_minutes": 60.0 if (d <= "2026-09-20" or running_after_last_usage) else 0.0,
                     "minutes_at_min": 0.0, "minutes_at_max": 0.0, "recycle_minutes": 0.0, "max_cluster_count": 1, "starts": 0} for d in days]
        cost = [{"workspace_id": "quiet", "warehouse_id": "w-quiet", "epoch_start": "2026-09-01T00:00:00+00:00", "usage_date": d,
                 "currency_code": "USD", "list_cost": 100.0, "dbu_quantity": 125.0, "unpriced_dbu_quantity": 0.0} for d in days if d <= "2026-09-20"]

        class Client:
            def query(self_inner, statement, *, label="collector_sql"):
                if label == "config_epochs":
                    return [epoch("quiet"), epoch("busy")]
                if label == "billing_watermark":
                    return [{"workspace_id": "quiet", "billing_watermark": "2026-09-20T18:00:00+00:00", "billing_ingested_through": "2026-09-21"},
                            {"workspace_id": "busy", "billing_watermark": "2026-10-03T06:00:00+00:00", "billing_ingested_through": "2026-10-03"}]
                return {"billing_daily_cost": cost, "resource_daily": resource}.get(label, [])
        ev = extract_evidence(Client(), self.t, "us-east-1", analysis_time="2026-10-04T12:00:00+00:00")
        return next(e for e in ev if e.workspace_id == "quiet")

    def _ran(self, *days):
        return [{"usage_day": d, "uptime_minutes": 60.0} for d in days]

    def auto_stop_only(self, idle_multiple):
        """Idle-expensive fixture where auto-stop is the only plan step (no size/min/max change)."""
        import dataclasses
        payload = load_case("idle-expensive-warehouse")
        ev = evidence_from_dict(payload["evidence"][0])
        ep = ev.current_epoch
        ep.config = dataclasses.replace(ep.config, min_clusters=1)
        ep.query.update(queued_query_pct_30=0.6, queued_queries_30=72, p95_wait_capacity_ms_when_queued_30=1000)
        for row in ep.gaps["running_idle_by_target"].values():
            row["removable_running_cluster_minutes"] = ep.events["cluster_minutes"] * idle_multiple
            row["removable_running_wall_minutes"] = row["removable_running_cluster_minutes"]
        w = p.evaluate_warehouses([ev], self.t)[0]
        cand = next(c for c in w.candidates if c.knob == "auto_stop_minutes")
        plan = next(s for s in w.scenarios if [st.knob for st in s.steps] == ["auto_stop_minutes"])
        return cand, plan

    def increase_candidate_package(self):
        payload = load_case("concurrency-constrained")
        pkg = p.build_recommendation_packages([evidence_from_dict(payload["evidence"][0])], self.t)[0]
        return next(c for c in pkg.candidates if c.knob.value == "max_clusters")

    def envelope_cases(self):
        """Serverless golden: Plan A is priced, Plan B (Pro migration) is UNAVAILABLE."""
        payload = load_case("serverless")
        e = evidence_from_dict(payload["evidence"][0])
        pkg = p.build_recommendation_packages([e], self.t)[0]
        priced = next(s for s in pkg.scenarios if s.portfolio_economics.savings_365d.total is not None)
        unpriced = next(s for s in pkg.scenarios if s.portfolio_economics.savings_365d.total is None)
        return e, pkg.scenarios, priced, unpriced

    def reasons(self, e, scenarios, recommended):
        econ = _package_economics(e, scenarios, recommended.scenario_id if recommended else None)
        return {x.scenario_id: x.reason_code for x in econ.excluded_scenarios}, econ
