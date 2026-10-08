"""Live collection (``realizer.analyzer.collector.bridge`` and ``collector.LiveSession``): errors and the fleet's shared reads.

* A failure of the SQL client during collection (a missing SELECT grant on a system table, a warehouse that does not start,
  a connection that cannot open) is an ``InputError`` naming the statement label (or the connection) and the exception
  class with the first line of its message: no SQL text, no token. A ``--package`` run exits 2 with that message instead of
  a traceback.
* A fleet run reads the account-wide statements (configuration epochs, billing watermark, billed daily cost) once and
  keeps one authenticated connection; each warehouse's evidence is the same as when each is measured on its own.
"""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from tests.unit.realizer import fleet_support
from realizer.analyzer.collector import bridge, collector, demo_world
from realizer.cli.main import EXIT_REFUSED, main
from realizer.common.errors import InputError
from realizer.config.loader import load_publisher_run
from realizer.contracts.fleet import SOURCE_LIVE, FleetRequest, LiveSource
from realizer.realizer import realize_fleet

SECRET_SQL = "SELECT secret_column FROM system.compute.warehouses"


class ServerError(Exception):
    """Stands for the connector's ``ServerOperationError``."""


class FailingClient:
    def __init__(self, fail_on, message="[INSUFFICIENT_PERMISSIONS] User cannot SELECT on system.billing.usage.\n== SQL ==\n" + SECRET_SQL):
        self.fail_on, self.message, self.labels = fail_on, message, []

    def query(self, statement, *, label="collector_sql"):
        self.labels.append(label)
        if label == self.fail_on:
            raise ServerError(self.message)
        return []


class ErrorMappingTests(unittest.TestCase):
    def assert_mapped(self, raised, label):
        text = str(raised.exception)
        self.assertIn(label, text)
        self.assertIn("ServerError", text)
        self.assertIn("[INSUFFICIENT_PERMISSIONS] User cannot SELECT on system.billing.usage.", text)
        self.assertNotIn("secret_column", text)
        self.assertNotIn("== SQL ==", text)

    def test_a_failed_history_statement_is_an_input_error(self):
        for label in ("config_epochs", "billing_watermark", "billing_daily_cost"):
            with self.subTest(label=label), self.assertRaises(InputError) as raised:
                bridge.collect_history(FailingClient(label), "1", "wh", as_of="2026-11-07T12:00:00+00:00", settle_hours=12)
            self.assert_mapped(raised, label)

    def test_a_failed_window_statement_is_an_input_error(self):
        windows = {"post": {"first_day": "2026-10-09", "last_day": "2026-11-05", "epoch_start": "2026-10-08T14:32:00+00:00", "targets": [30]}}
        with self.assertRaises(InputError) as raised:
            bridge.collect_windows(FailingClient("running_idle"), "1", "wh", as_of="2026-11-07T12:00:00+00:00", windows=windows)
        self.assert_mapped(raised, "running_idle")

    def test_the_backtest_statements_are_mapped_too(self):
        with self.assertRaises(InputError) as raised:
            bridge.collect_fleet_history(FailingClient("billing_daily_cost"), as_of="2026-11-07T12:00:00+00:00", settle_hours=12)
        self.assert_mapped(raised, "billing_daily_cost")

    def test_a_message_is_cut_to_one_short_line(self):
        with self.assertRaises(InputError) as raised:
            bridge.collect_history(FailingClient("config_epochs", "x" * 5000), "1", "wh", as_of="2026-11-07T12:00:00+00:00", settle_hours=12)
        self.assertLess(len(str(raised.exception)), 400)

    def test_a_connection_that_cannot_open_is_an_input_error(self):
        def refuse(**kwargs):
            raise TimeoutError("warehouse did not start within 300 s\nconnection token: tok-123")
        fake = types.SimpleNamespace(sql=types.SimpleNamespace(connect=refuse))
        auth = bridge.AuthContext("p", "host", "/sql/1.0/warehouses/w", "tok-123", None, "warehouse", "w")
        with mock.patch.dict(sys.modules, {"databricks": fake, "databricks.sql": fake.sql}), self.assertRaises(InputError) as raised:
            with bridge.LiveSqlClient(auth):
                pass
        self.assertIn("TimeoutError: warehouse did not start within 300 s", str(raised.exception))
        self.assertNotIn("tok-123", str(raised.exception))

    def test_a_package_run_exits_2_with_the_message_and_no_traceback(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(collector.bridge, "resolve_live_access", return_value=object()), \
                mock.patch.object(collector.bridge, "LiveSqlClient") as client:
            client.return_value.__enter__.return_value = FailingClient("config_epochs")
            code = main(["--package", str(world.PACKAGES / "autostop-scoped-pro.json"), "--profile", "p", "--sql-warehouse-id", "wh-collector",
                         "--output-dir", tmp, "--flat-output", "--as-of", world.AS_OF, "--log-level", "ERROR"])
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("config_epochs", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("secret_column", err.getvalue())


class CountingClient(bridge.DemoWorldClient):
    """The demo world as the system tables, counting the statements asked."""

    def __init__(self, rows, analysis_time, tally):
        super().__init__(rows, analysis_time)
        self.tally = tally

    def query(self, statement, *, label="collector_sql"):
        self.tally["labels"].append(label)
        return super().query(statement, label=label)


def fleet_rows(root, as_of):
    """The simulated system tables of the warehouses of the demo Publisher run at *root* after the demo owner applied each
    package's plan, as one account's."""
    run = load_publisher_run(root)
    merged = {}
    for entry in run.entries:
        package = json.loads((run.run_dir / entry.path).read_text(encoding="utf-8"))
        index = demo_world.fleet_index(entry.warehouse_id)
        change = demo_world.applied_change(package)
        changes = [] if change is None else [{"at": demo_world.change_at(package).isoformat(), "set": change}]
        for label, rows in world.world_rows(demo_world.world_spec(index, changes), index, as_of).items():
            merged.setdefault(label, []).extend(rows)
    return merged


class SharedReadsTests(unittest.TestCase):
    ACCOUNT_WIDE = ("config_epochs", "billing_watermark", "billing_daily_cost")

    @classmethod
    def setUpClass(cls):
        cls.root = fleet_support.publisher_demo_root()
        cls.rows = fleet_rows(cls.root, world.AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            cls.report, cls.tally, cls.token = cls.run_fleet(tmp)
            cls.shared = cls.evidence(tmp)

    @classmethod
    def run_fleet(cls, tmp):
        tally = {"labels": [], "connections": 0, "closed": 0}
        rows = cls.rows

        class FakeClient:
            def __init__(self, auth):
                self.inner = CountingClient(rows, world.AS_OF, tally)

            def __enter__(self):
                tally["connections"] += 1
                return self.inner

            def __exit__(self, *exc):
                tally["closed"] += 1

        request = FleetRequest(publisher_run=load_publisher_run(cls.root), source=SOURCE_LIVE, run_dir=Path(tmp, "run"), state_dir=Path(tmp, "state"),
                               live=LiveSource(profile="p", sql_warehouse_id="wh-collector", aws_price_registry=None), as_of=world.AS_OF)
        request.run_dir.mkdir(parents=True)
        with mock.patch.object(bridge, "LiveSqlClient", FakeClient), \
                mock.patch.object(bridge.access, "resolve_cli_token", return_value=("host", "tok", None)) as token:
            report = realize_fleet(request)
        return report, tally, token

    @staticmethod
    def evidence(tmp):
        folder = Path(tmp, "state", "realization", "measurements")
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.json"))}

    def test_a_fleet_reads_the_account_wide_statements_once_over_one_connection(self):
        tally = self.tally
        self.assertEqual([r.status for r in self.report.warehouse_runs], ["OK", "OK", "OK"])
        for label in self.ACCOUNT_WIDE:
            self.assertEqual(tally["labels"].count(label), 1, label)
        self.assertEqual((tally["connections"], tally["closed"], self.token.call_count), (1, 1, 1))
        self.assertGreaterEqual(tally["labels"].count("running_idle"), 3)  # each warehouse's own windows are still read per warehouse

    def test_the_evidence_is_the_one_each_warehouse_reads_on_its_own(self):
        self.assertEqual(len(self.shared), 3)
        solo = {}
        run = load_publisher_run(self.root)
        for entry in run.entries:
            package = json.loads((run.run_dir / entry.path).read_text(encoding="utf-8"))
            solo[entry.warehouse_id] = collector.collect_measurement(
                bridge.DemoWorldClient(self.rows, world.AS_OF), package, as_of=world.AS_OF, source="LIVE_SYSTEM_TABLES", aws_registry=world.registry())
        for name, text in self.shared.items():
            evidence = json.loads(text)
            self.assertEqual(evidence, solo[evidence["warehouse_id"]], name)


class SessionTests(unittest.TestCase):
    def test_the_session_reconnects_when_the_token_is_about_to_expire(self):
        tokens = iter([("h", "t1", (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()),
                       ("h", "t2", (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat())])
        opened = []

        class FakeClient:
            def __init__(self, auth):
                self.auth = auth

            def __enter__(self):
                opened.append(self.auth.access_token)
                return self

            def __exit__(self, *exc):
                return None

            def query(self, statement, *, label="collector_sql"):
                return [{"label": label}]

        with mock.patch.object(bridge, "LiveSqlClient", FakeClient), mock.patch.object(bridge.access, "resolve_cli_token", lambda *a, **k: next(tokens)):
            with collector.LiveSession(profile="p", sql_warehouse_id="wh-collector") as session:
                session.client()
                session.client()  # the first token expires within the refresh margin
                session.client()
        self.assertEqual(opened, ["t1", "t2"])

    def test_a_failed_measurement_reconnects_for_the_next_warehouse(self):
        opened = []

        class FakeClient:
            def __init__(self, auth):
                pass

            def __enter__(self):
                opened.append(1)
                return self

            def __exit__(self, *exc):
                return None

            def query(self, statement, *, label="collector_sql"):
                raise ServerError("connection dropped")

        with mock.patch.object(bridge, "LiveSqlClient", FakeClient), mock.patch.object(bridge.access, "resolve_cli_token", return_value=("h", "t", None)):
            with collector.LiveSession(profile="p", sql_warehouse_id="wh-collector") as session:
                for _ in range(2):
                    with self.assertRaises(InputError):
                        collector.measure_live(world.package("autostop-scoped-pro"), Path("unused"), profile="p", sql_warehouse_id="wh-collector",
                                               session=session, as_of=world.AS_OF)
        self.assertEqual(len(opened), 2)

    def test_a_session_refuses_the_warehouse_it_runs_on(self):
        with mock.patch.object(bridge.access, "resolve_cli_token", return_value=("h", "t", None)), \
                collector.LiveSession(profile="p", sql_warehouse_id=" WH-Demo-013 ") as session, self.assertRaises(InputError):
            collector.measure_live(world.package("autostop-scoped-pro"), Path("unused"), profile="p", sql_warehouse_id="wh-demo-013", session=session)


if __name__ == "__main__":
    unittest.main()
