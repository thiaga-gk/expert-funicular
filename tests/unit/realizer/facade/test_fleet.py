"""Fleet run (src/realizer/fleet.py, the facade's ``realize_fleet``): every warehouse of a Publisher run.

* Over a real Publisher demo run, every warehouse is realized from the chosen source into one run folder, the history
  goes to the state, and a run manifest and the realization workbook are written; a second daily run continues the
  same state, and the evidence a demo run kept replays to the same realization packages.
* A failure is isolated to its warehouse: a refusal (RealizerError) is REFUSED with its message, any other exception
  CRASHED with its type only; the other warehouses are realized; exit status 1 on a crash, else 2 on a refusal, else 0.
* Every open lineage of a warehouse kept in the state is realized before the run's package, so the owner of a change
  records its claim in the same run even when the Publisher run carries only a newer package.
* One warehouse at a time under a per-warehouse lock: a second run of the same warehouse at the same time is refused
  for that warehouse.
"""
import copy
import io
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.helpers import fixture_package  # noqa: F401  (src/realizer on the path)
from tests.unit.realizer import fleet_support
from tests.unit.realizer.facade.test_lineages import BALANCED, measured, stored_run
from realizer import fleet
from realizer import realizer as facade
from realizer.common.errors import InputError
from realizer.common.logging import LOGGER
from realizer.config.loader import load_publisher_run
from realizer.contracts.fleet import SOURCE_DEMO, SOURCE_EVIDENCE, SOURCE_LIVE, FleetRequest, LiveSource
from realizer.contracts.realization import make_rpkg_id
from realizer.packager import ledger, packager
from realizer.realizer import measure_demo, realize, realize_fleet


def request(publisher_run, out: Path, state: Path, source=SOURCE_DEMO, **kwargs) -> FleetRequest:
    out.mkdir(parents=True, exist_ok=True)
    return FleetRequest(publisher_run=load_publisher_run(publisher_run), source=source, run_dir=out, state_dir=state, **kwargs)


def files(root: Path):
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file() and p.suffix != ".xlsx"}


def rpkg(out: Path, package) -> dict:
    return json.loads((out / "realization" / "packages" / f"{make_rpkg_id(package['recommendation_package_id'])}.json").read_text(encoding="utf-8"))


class FleetDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fleet_support.publisher_demo_root()
        cls.publisher = load_publisher_run(cls.root)

    def test_every_warehouse_is_realized_into_the_run_folder_and_its_history_into_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, state = Path(tmp, "run"), Path(tmp, "state")
            report = realize_fleet(request(self.root, out, state))
            self.assertEqual([(r.warehouse_id, r.status, r.reason) for r in report.warehouse_runs],
                             [(w, "OK", None) for w in fleet_support.DEMO_WAREHOUSES])
            self.assertEqual(report.exit_status, 0)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["manifest.json", "realization", "realization_workbook.xlsx"])
            self.assertEqual(report.workbook_path, out / "realization_workbook.xlsx")
            manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"], "DEMO_SIMULATION")
            self.assertEqual(manifest["publisher_run"], {"run_dir": str(self.publisher.run_dir), "manifest_package_count": 3, "selected_package_count": 3})
            self.assertEqual(manifest["state_dir"], str(state))
            self.assertEqual(manifest["workbook_file"], "realization_workbook.xlsx")
            self.assertEqual(manifest["status_counts"], {"OK": 3, "REFUSED": 0, "CRASHED": 0})
            for row, entry in zip(manifest["warehouses"], self.publisher.entries):
                self.assertEqual((row["workspace_id"], row["warehouse_id"], row["recommendation_package_id"]),
                                 (entry.workspace_id, entry.warehouse_id, entry.recommendation_package_id))
                self.assertEqual(row["realization_package_ids"], [make_rpkg_id(entry.recommendation_package_id)])
                self.assertEqual([f.split("/")[1] for f in row["files"]], ["packages", "snapshots"])
                self.assertTrue(all((out / f).is_file() for f in row["files"]))
            # No decision changes: each realization package is the one a single-package run of the same observation writes.
            for entry in self.publisher.entries:
                package = json.loads((self.publisher.run_dir / entry.path).read_text(encoding="utf-8"))
                with tempfile.TemporaryDirectory() as single:
                    realize(package, measure_demo(package, Path(single)), Path(single))
                    self.assertEqual(rpkg(out, package), rpkg(Path(single), package))
            self.assertTrue({p.parent.name for p in (state / "realization").rglob("*.json*")} >= {"ledger", "measurements", "state"})
            runs = {r.warehouse_id: r for r in report.warehouse_runs}
            self.assertEqual(len(runs["wh-demo-013"].realizations), 1)
            self.assertIsNotNone(runs["wh-demo-013"].state_index)

    def test_a_second_daily_run_continues_the_state_and_writes_the_same_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "state")
            first = realize_fleet(request(self.root, Path(tmp, "day1"), state))
            ledgers = files(state / "realization" / "ledger")
            second = realize_fleet(request(self.root, Path(tmp, "day2"), state))
            self.assertEqual(second.manifest_path.read_text(encoding="utf-8"), first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(files(state / "realization" / "ledger"), ledgers, "the same observation again appends nothing")
            self.assertEqual(len(list((state / "realization" / "ledger").glob("*.jsonl"))), 3)

    def test_the_evidence_a_run_kept_replays_to_the_same_realization_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            realize_fleet(request(self.root, Path(tmp, "demo"), Path(tmp, "state")))
            report = realize_fleet(request(self.root, Path(tmp, "replay"), Path(tmp, "state2"), SOURCE_EVIDENCE,
                                           evidence_dir=Path(tmp, "state", "realization", "measurements")))
            self.assertEqual(report.exit_status, 0)
            self.assertEqual(files(Path(tmp, "replay", "realization")), files(Path(tmp, "demo", "realization")))

    def test_a_package_without_evidence_in_the_folder_is_refused_for_its_warehouse(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "empty").mkdir()
            report = realize_fleet(request(self.root, Path(tmp, "run"), Path(tmp, "state"), SOURCE_EVIDENCE, evidence_dir=Path(tmp, "empty")))
            self.assertEqual({r.status for r in report.warehouse_runs}, {"REFUSED"})
            self.assertRegex(report.warehouse_runs[0].reason, r"^no measurement evidence of package PKG-[0-9A-F]{24} in ")
            self.assertEqual(report.exit_status, 2)


class FailureIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fleet_support.publisher_demo_root()

    def run_with(self, failures):
        def observe(req, package):
            error = failures.get(package["warehouse"]["warehouse_id"])
            if error is not None:
                raise error
            return measure_demo(package, req.state_dir)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        LOGGER.addHandler(handler)
        level = LOGGER.level
        LOGGER.setLevel(logging.INFO)
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(fleet, "observe", observe):
                report = realize_fleet(request(self.root, Path(tmp, "run"), Path(tmp, "state")))
                manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
                workbook = report.workbook_path.is_file()
        finally:
            LOGGER.removeHandler(handler)
            LOGGER.setLevel(level)
        return report, manifest, workbook, stream.getvalue()

    def test_a_refusal_and_a_crash_stay_with_their_warehouses(self):
        report, manifest, workbook, log = self.run_with({"wh-demo-006": InputError("warehouse refused for a reason"),
                                                         "wh-demo-013": RuntimeError("secret value 4242")})
        self.assertEqual([(r.warehouse_id, r.status, r.reason) for r in report.warehouse_runs], [
            ("wh-demo-006", "REFUSED", "warehouse refused for a reason"), ("wh-demo-013", "CRASHED", "RuntimeError"), ("wh-demo-039", "OK", None)])
        self.assertEqual(report.exit_status, 1)
        self.assertEqual(manifest["status_counts"], {"OK": 1, "REFUSED": 1, "CRASHED": 1})
        self.assertEqual([(w["status"], w["realization_package_ids"] != [], w["files"] != []) for w in manifest["warehouses"]],
                         [("REFUSED", False, False), ("CRASHED", False, False), ("OK", True, True)])
        self.assertTrue(workbook)
        self.assertIn("fleet.warehouse_crashed workspace=2234567890123456 warehouse=wh-demo-013 error_type=RuntimeError", log)
        self.assertNotIn("4242", log)
        self.assertNotIn("4242", json.dumps(manifest))

    def test_only_refusals_exit_2(self):
        report, _, _, _ = self.run_with({"wh-demo-039": InputError("no")})
        self.assertEqual([r.status for r in report.warehouse_runs], ["OK", "OK", "REFUSED"])
        self.assertEqual(report.exit_status, 2)

    def test_a_package_that_is_not_the_manifest_entry_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = load_publisher_run(self.root).run_dir
            copy_dir = Path(tmp, "pub")
            shutil.copytree(source, copy_dir)
            manifest = json.loads((copy_dir / "manifest.json").read_text(encoding="utf-8"))
            manifest["packages"][0]["path"], manifest["packages"][1]["path"] = manifest["packages"][1]["path"], manifest["packages"][0]["path"]
            (copy_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = realize_fleet(request(copy_dir, Path(tmp, "run"), Path(tmp, "state")))
            statuses = {r.warehouse_id: r.status for r in report.warehouse_runs}
            self.assertEqual(sorted(statuses.values()), ["OK", "REFUSED", "REFUSED"])


class OpenLineageTests(unittest.TestCase):
    """wh-demo-039: PKG-A (2026-10-01, auto-stop 365 to 210) is applied on 2026-10-08 14:32; PKG-B (2026-10-15) starts
    from auto-stop 210. A was realized before the change; the Publisher's run of 2026-10-15 carries only B."""

    @classmethod
    def setUpClass(cls):
        cls.a = stored_run(BALANCED)[0]
        cls.b = fixture_package("successor-after-autostop")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        realize(copy.deepcopy(self.a), None, self.tmp / "day1", state_dir=self.state)  # A before the change: lineage only
        self.publisher = fleet_support.run_folder(self.tmp / "publisher", [self.b])

    def live(self):
        return mock.patch.object(fleet, "measure_live", lambda package, state_dir, **kw: measured(package))

    def test_a_fleet_run_over_the_newer_package_records_the_owners_claim(self):
        with self.live():
            report = realize_fleet(request(self.publisher, self.tmp / "day2", self.state, SOURCE_LIVE, live=LiveSource(profile="p", sql_warehouse_id="w")))
        self.assertEqual(report.exit_status, 0)
        owner = rpkg(self.tmp / "day2", self.a)
        self.assertEqual(owner["application"]["implementation_conformance"], "EXACT")
        self.assertEqual(owner["attribution"]["attribution_claim_status"], "CLAIMABLE")
        self.assertEqual(owner["attribution"]["recommendation_attributed_realized_savings"], "1069.73")
        self.assertEqual(owner["source_recommendation"]["successor_package_id"], self.b["recommendation_package_id"])
        (run,) = report.warehouse_runs
        self.assertEqual(sorted(r.realization_package_id for r in run.realizations),
                         sorted(make_rpkg_id(p["recommendation_package_id"]) for p in (self.a, self.b)))
        self.assertEqual([o["recommendation_package_id"] for o in run.state_index["applied_knob_owners"]], [self.a["recommendation_package_id"]])
        self.assertTrue(set(run.next_steps) <= {self.a["recommendation_package_id"], self.b["recommendation_package_id"]})

    def test_realizing_only_the_newer_package_records_no_claim(self):
        # The control: the run of B alone (what a single-package run does) never records A's application.
        realize(copy.deepcopy(self.b), measured(self.b), self.tmp / "day2", state_dir=self.state)
        index = json.loads(next((self.state / "realization" / "state").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(index["applied_knob_owners"], [])
        self.assertEqual({r["recommendation_package_id"]: r["application_state"] for r in index["lineages"]}[self.a["recommendation_package_id"]], "NOT_APPLIED")

    def test_each_lineage_is_measured_separately_and_the_runs_package_once(self):
        calls = []
        original = fleet.realize_request

        def counted(req):
            calls.append(req.package["recommendation_package_id"])
            return original(req)
        with self.live(), mock.patch.object(fleet, "realize_request", counted):
            realize_fleet(request(self.publisher, self.tmp / "day2", self.state, SOURCE_LIVE, live=LiveSource(profile="p", sql_warehouse_id="w")))
            realize_fleet(request(self.publisher, self.tmp / "day3", self.state, SOURCE_LIVE, live=LiveSource(profile="p", sql_warehouse_id="w")))
        a, b = self.a["recommendation_package_id"], self.b["recommendation_package_id"]
        self.assertEqual(calls, [a, b, a, b])  # oldest first; B, open on day 3, realized once

    def test_the_live_measurements_of_two_lineages_keep_two_evidence_files(self):
        with mock.patch.object(fleet, "measure_live", self.real_live_with_world()):
            realize_fleet(request(self.publisher, self.tmp / "day2", self.state, SOURCE_LIVE, live=LiveSource(profile="p", sql_warehouse_id="w")))
        names = sorted(p.name for p in (self.state / "realization" / "measurements").glob("*.json"))
        self.assertEqual(len(names), 2)
        self.assertEqual({n.split("__")[2] for n in names}, {self.a["recommendation_package_id"], self.b["recommendation_package_id"]})
        self.assertEqual({n.split("__")[3] for n in names}, {"20261107T120000Z.json"})

    def real_live_with_world(self):
        """``measure_live`` itself, its system tables replaced by the simulated world (it writes the evidence file)."""
        from realizer.analyzer.collector import collector
        from tests import realizer_world
        from tests.golden.realizer import e2e
        from tests.unit.realizer.facade.test_lineages import CHANGE

        def live(package, state_dir, **kwargs):
            evidence = realizer_world.measure(package, e2e.world_spec(39, CHANGE), 39)
            with mock.patch.object(collector.bridge, "resolve_live_access", return_value=object()), \
                    mock.patch.object(collector.bridge, "LiveSqlClient") as client, \
                    mock.patch.object(collector, "collect_measurement", return_value=evidence):
                client.return_value.__enter__.return_value = object()
                return collector.measure_live(package, state_dir, profile="p", sql_warehouse_id="w", as_of=evidence["as_of_utc"])
        return live


class ConcurrentRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fleet_support.publisher_demo_root()

    def test_a_warehouse_another_run_holds_is_refused_and_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ledger, "LOCK_WAIT_SECONDS", 0.1):
            state = Path(tmp, "state")
            with packager.warehouse_lock(state, "2234567890123456", "wh-demo-013"):
                report = realize_fleet(request(self.root, Path(tmp, "run"), state))
            self.assertEqual([(r.warehouse_id, r.status, r.reason) for r in report.warehouse_runs], [
                ("wh-demo-006", "OK", None),
                ("wh-demo-013", "REFUSED", "warehouse 2234567890123456/wh-demo-013 is locked by another run"),
                ("wh-demo-039", "OK", None)])
            self.assertEqual(report.exit_status, 2)
            self.assertFalse(list((state / "realization" / "sources").glob("2234567890123456__wh-demo-013")))
            self.assertFalse(list(state.rglob("*.lock")), "every lock is released")
            again = realize_fleet(request(self.root, Path(tmp, "run2"), state))
            self.assertEqual({r.status for r in again.warehouse_runs}, {"OK"})

    def test_a_single_package_cli_run_holds_the_same_lock(self):
        package = json.loads((load_publisher_run(self.root).run_dir / load_publisher_run(self.root).entries[1].path).read_text(encoding="utf-8"))
        from realizer.contracts.request import RealizationRequest
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ledger, "LOCK_WAIT_SECONDS", 0.1):
            with packager.warehouse_lock(Path(tmp), "2234567890123456", "wh-demo-013"):
                with self.assertRaisesRegex(Exception, "warehouse 2234567890123456/wh-demo-013 is locked by another run"):
                    facade.realize_package(RealizationRequest(package=package, observation=None, output_dir=Path(tmp)))



class NoDomainLogicTests(unittest.TestCase):
    """The fleet module only sequences ``realize_request`` per lineage per warehouse; every decision is a capability's."""

    def test_no_sql_lifecycle_savings_ledger_or_rendering_vocabulary(self):
        import ast
        import inspect
        module = ast.parse(Path(inspect.getsourcefile(fleet)).read_text(encoding="utf-8"))
        source = ast.unparse([n for n in module.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))])
        for word in ("SELECT ", "FROM system", "append_if_absent", "project_snapshot", "make_ledger_entry", "calculate_economics",
                     "realized_savings", "openpyxl", "EventType", "RealizationState", "superseded", "claimed_by", "disposition"):
            self.assertNotIn(word, source)

    def test_the_cli_reaches_the_fleet_through_the_facade(self):
        self.assertEqual(facade.realize_fleet.__module__, "realizer.realizer")
        self.assertNotIn("realizer.fleet", Path(__import__("realizer.cli.realize", fromlist=["x"]).__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
