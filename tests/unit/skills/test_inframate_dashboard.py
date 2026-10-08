"""inframate-dashboard skill: run discovery, the as-of join, aging, KPI rules and the rendered page.

The fixture runs are the committed demo packages copied into three dated run folders (only the manifest's
generated_at differs, as it would between real runs); the realization snapshots come from the end-to-end goldens.
One integration test runs the Publisher and the Realizer demos in process on a few warehouses and checks the page
against the run's own workbook.
"""
import contextlib
import copy
import importlib.util
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
SCRIPT = ROOT / ".claude" / "skills" / "inframate-dashboard" / "scripts" / "build_dashboard.py"
DEMO_PACKAGES = ROOT / "tests" / "fixtures" / "publisher" / "expected" / "demo"
E2E = ROOT / "tests" / "golden" / "realizer" / "cases"
RUN_TIMES = ("2026-10-01T12:00:00Z", "2026-11-01T12:00:00Z", "2026-12-01T12:00:00Z")


def load_builder():
    spec = importlib.util.spec_from_file_location("build_dashboard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_builder()


def write_run(root: Path, stamp: str, generated_at: str, packages_dir: Path = DEMO_PACKAGES, manifest_extra=None) -> Path:
    run = root / f"run_date={generated_at[:10]}" / f"run_ts={stamp}"
    shutil.copytree(packages_dir / "packages", run / "packages")
    manifest = json.loads((packages_dir / "manifest.json").read_text())
    manifest.update({"generated_at": generated_at, "workbook_status": "SKIPPED"}, **(manifest_extra or {}))
    (run / "manifest.json").write_text(json.dumps(manifest))
    return run


def realizer_root(root: Path, cases=("ge01", "ge04", "ge08", "ge13")) -> Path:
    """A flat Realizer root holding the snapshots and ledgers of a few end-to-end golden cases."""
    out = root / "realizer"
    for prefix in cases:
        case = next(E2E.glob(prefix + "-*"))
        for sub in ("packages", "ledger"):
            src = case / "expected" / "realizer" / "realization" / sub
            if src.is_dir():
                shutil.copytree(src, out / "realization" / sub, dirs_exist_ok=True)
    return out


class DashboardFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.tmp = Path(tempfile.mkdtemp(prefix="dash-test-"))
        cls.pub = cls.tmp / "pub"
        for i, t in enumerate(RUN_TIMES):
            write_run(cls.pub, f"2026100{i + 1}T000000Z", t)
        (cls.pub / "run_date=2026-10-09" / "run_ts=20261009T000000Z").mkdir(parents=True)  # crashed run: no manifest
        realizer_run = cls.pub / "run_date=2026-10-09" / "run_ts=20261009T010000Z"
        realizer_run.mkdir(parents=True)
        (realizer_run / "manifest.json").write_text(json.dumps({"run_manifest_version": "1.1.0"}))
        cls.real = realizer_root(cls.tmp)
        cls.data = B.build_dataset(cls.pub, cls.real, detail_runs=6, raw="lean", demo=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def test_complete_publisher_runs_only(self):
        self.assertEqual([r["key"] for r in self.data["runs"]], list(RUN_TIMES))
        self.assertEqual([s["reason"] for s in self.data["skipped"]], ["incomplete: no manifest.json"])

    def test_kpis_follow_the_workbook_rules(self):
        packages = [json.loads(p.read_text()) for p in sorted((DEMO_PACKAGES / "packages").glob("*.json"))]
        k = self.data["runs"][-1]["kpis"]["by_currency"]["USD"]
        totals = [p["cost_baseline"]["actual_365d"]["total_cost"] for p in packages]
        self.assertIn(None, totals)  # the demo fleet's unpriced-SKU warehouse
        self.assertIsNone(k["actual"]["total"])  # one unknown makes the fleet total unknown, never a smaller number
        self.assertEqual(k["actual_known"]["count"], sum(1 for t in totals if t is not None))
        self.assertAlmostEqual(k["actual_known"]["total"], round(sum(t for t in totals if t is not None), 2), places=2)
        self.assertEqual(k["warehouses"], len(packages))
        bases = [p["package_economics"]["estimated_savings_365d"]["base"] for p in packages if p["package_economics"]["estimated_savings_365d"]]
        self.assertAlmostEqual(k["net"], round(sum(bases), 2), places=2)
        self.assertAlmostEqual(k["gross_savings"] - k["cost_increase"], k["net"], places=2)

    def test_an_unknown_plan_component_makes_the_fleet_component_unknown(self):
        packages = sorted((json.loads(f.read_text()) for f in (DEMO_PACKAGES / "packages").glob("*.json")),
                          key=lambda p: (p["warehouse"]["workspace_id"], p["warehouse"]["warehouse_id"]))
        warehouses = copy.deepcopy(self.data["details"][RUN_TIMES[-1]]["warehouses"])
        manifest = json.loads((DEMO_PACKAGES / "manifest.json").read_text())
        known = B.run_kpis(warehouses, packages, manifest, [])["by_currency"]["USD"]
        self.assertIsNotNone(known["identified_aws"])
        next(w for w in warehouses if w["plan"] and w["plan"]["aws"])["plan"]["aws"] = None
        k = B.run_kpis(warehouses, packages, manifest, [])["by_currency"]["USD"]
        self.assertIsNone(k["identified_aws"])  # unavailable, never the sum of the known warehouses
        self.assertEqual(k["identified_dbu"], known["identified_dbu"])

    def test_aging_counts_days_from_the_first_run_of_the_streak(self):
        ages = [sorted({r["age_days"] for r in self.data["details"][t]["recommendations"] if r["status"] == "OPEN"}) for t in RUN_TIMES]
        self.assertEqual(ages[0], [0])
        self.assertIn(31, ages[1])
        self.assertIn(61, ages[2])
        first = {r["first_seen"] for r in self.data["details"][RUN_TIMES[2]]["recommendations"] if r["status"] == "OPEN" and r["age_days"] == 61}
        self.assertEqual(first, {RUN_TIMES[0]})

    def test_age_buckets(self):
        self.assertEqual([B.age_bucket(d) for d in (0, 30, 31, 60, 61, 90, 91, None)], ["0-30", "0-30", "31-60", "31-60", "61-90", "61-90", "90+", None])

    def test_as_of_join_keeps_snapshots_before_the_next_run(self):
        self.assertTrue(self.data["meta"]["realizer_found"])
        for i, t in enumerate(RUN_TIMES):
            lineages = [l for w in self.data["details"][t]["warehouses"] for l in w["realization"]]
            cutoff = RUN_TIMES[i + 1] if i + 1 < len(RUN_TIMES) else None
            for l in lineages:
                if cutoff:
                    self.assertLess(l["as_of"], cutoff)
                for e in l["timeline"]:
                    if cutoff:
                        self.assertLess(e["recorded"], cutoff)
        newest = [l for w in self.data["details"][RUN_TIMES[-1]]["warehouses"] for l in w["realization"]]
        self.assertTrue(newest, "the e2e snapshots join to the demo warehouses by identity")

    def test_realized_counts_claimable_lineages_only(self):
        k = self.data["runs"][-1]["kpis"]["by_currency"]["USD"]
        lineages = [l for w in self.data["details"][RUN_TIMES[-1]]["warehouses"] for l in w["realization"]]
        claimable = [l["annualized"] for l in lineages if l["claim"] == "CLAIMABLE" and l["annualized"] is not None]
        self.assertAlmostEqual(k["realized"], round(sum(claimable), 2), places=2)

    def test_render_is_deterministic_self_contained_and_verified(self):
        html = B.render(self.data, "Test")
        self.assertEqual(html, B.render(copy.deepcopy(self.data), "Test"))
        self.assertNotIn(str(self.tmp), html)  # a demo build embeds no local path
        self.assertEqual(B.verify(html, self.data, None), [])
        for marker in B.MARKERS:
            self.assertNotIn(marker, html)

    def test_evidence_rows_decode_through_the_string_table(self):
        w = next(w for w in self.data["details"][RUN_TIMES[-1]]["warehouses"] if w["evidence"])
        row = w["evidence"][0]
        fields = self.data["evidence_fields"]
        self.assertTrue(row[0].startswith("EVID-"))
        self.assertIsInstance(self.data["strings"][row[1 + fields.index("metric")]], str)
        refs = {i for c in w["candidates"] for i in c["evidence"]}
        self.assertTrue(refs <= set(range(len(w["evidence"]))))

    def test_every_scenario_carries_its_steps_in_execution_order(self):
        for w in self.data["details"][RUN_TIMES[-1]]["warehouses"]:
            for s in w["scenarios"]:
                self.assertEqual([st["step"] for st in s["steps"]], sorted(st["step"] for st in s["steps"]))
            recommended = [s for s in w["scenarios"] if s["recommended"]]
            self.assertEqual(len(recommended), 1)
            self.assertEqual(recommended[0]["steps"], w["steps"])

    def test_config_history_falls_back_to_the_package_change_without_evidence(self):
        # the fixture runs carry no evidence.json: history is the package's observed_change, or nothing
        for w in self.data["details"][RUN_TIMES[-1]]["warehouses"]:
            self.assertIn(w["history"]["source"], ("package", "none"))
            for period in w["history"]["periods"]:
                self.assertTrue(period["changes"])

    def test_config_history_from_evidence_lists_each_change(self):
        package = json.loads(next((DEMO_PACKAGES / "packages").glob("*wh-demo-040.json")).read_text())
        config = {"warehouse_type": "PRO", "warehouse_size": "LARGE", "min_clusters": 1, "max_clusters": 2, "auto_stop_minutes": 15, "warehouse_channel": "CURRENT"}
        epochs = [
            {"epoch_start": "2025-10-01T00:00:00+00:00", "epoch_end": "2026-09-27T00:00:00+00:00", "is_current": False, "config": config,
             "cost": {"cost_days": 300, "list_cost": 48000.0, "dbu_quantity": 1.0, "unpriced_dbu_quantity": 0.0}, "query": {"epoch_p95_execution_ms": 2500}, "events": {}},
            {"epoch_start": "2026-09-27T00:00:00+00:00", "epoch_end": None, "is_current": True, "config": {**config, "warehouse_size": "MEDIUM"},
             "cost": {"cost_days": 4, "list_cost": 380.0, "dbu_quantity": 1.0, "unpriced_dbu_quantity": 5.0}, "query": {}, "events": {}},
        ]
        hist = B.config_history({"epochs": epochs, "analysis_time": "2026-10-01T12:00:00+00:00"}, package, [])
        self.assertEqual(hist["source"], "evidence")
        first, current = hist["periods"]
        self.assertEqual(first["changes"], [])
        self.assertEqual(current["changes"], [{"knob": "warehouse_size", "from": "LARGE", "to": "MEDIUM"}])
        self.assertEqual((first["days"], current["days"]), (361, 4))
        self.assertEqual(first["list_cost"], 48000.0)
        self.assertIsNone(current["list_cost"])  # unpriced DBU in the period: its cost is unknown, never a smaller number
        self.assertIsNone(current["link"])

    def test_an_unsupported_contract_run_is_skipped_and_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "20261001T000000Z", RUN_TIMES[0])
            path = next((run / "packages").glob("*.json"))
            doc = json.loads(path.read_text()); doc["contract_version"] = "3.1.0"; path.write_text(json.dumps(doc))
            data = B.build_dataset(Path(tmp), None, detail_runs=1, raw="none", demo=True)
        self.assertEqual(data["runs"], [])
        self.assertIn("unsupported Publisher contract", data["skipped"][0]["reason"])

    def test_verify_rejects_external_resources(self):
        html = B.render(self.data, "Test").replace("</head>", '<script src="https://example.com/x.js"></script></head>')
        self.assertTrue(any("external" in p for p in B.verify(html, self.data, None)))


class DashboardPipelineTests(unittest.TestCase):
    """The Publisher and Realizer demos on a few warehouses, then a build reconciled with the run's workbook."""

    def test_built_page_reconciles_with_the_workbook(self):
        from publisher.cli.main import main as publisher_main
        from realizer.cli.main import main as realizer_main
        out_text = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out_text), contextlib.redirect_stderr(io.StringIO()):
            pub, real = Path(tmp) / "pub", Path(tmp) / "real"
            self.assertEqual(publisher_main(["--demo", "--demo-count", "4", "--output-dir", str(pub), "--log-level", "ERROR"]), 0)
            run = next(pub.glob("run_date=*/run_ts=*"))
            self.assertEqual(realizer_main(["--publisher-run", str(run), "--demo", "--output-dir", str(real), "--log-level", "ERROR"]), 0)
            out = Path(tmp) / "dash.html"
            code = B.main(["--publisher-root", str(pub), "--realizer-root", str(real), "--out", str(out), "--verify"])
            self.assertEqual(code, 0)
            html = out.read_text()
            data = json.loads(html.split('id="dashboard-data">', 1)[1].split("</script>", 1)[0].replace("<\\/", "</"))
            problems = B.reconcile_workbooks(data, pub)
            self.assertEqual(B.main(["--refresh", "--publisher-root", str(pub), "--realizer-root", str(real), "--out", str(out)]), 0)
            logging.disable(logging.NOTSET)
        self.assertEqual(problems, [])
        self.assertIn("reconciled 1 run(s)", out_text.getvalue())  # the build's own --verify compared the workbook cells
        self.assertIn("refresh: newest run 2026-10-01; 0 run(s) new since the last build", out_text.getvalue())
        k = data["runs"][0]["kpis"]["by_currency"]["USD"]
        self.assertEqual(k["warehouses"], 4)
        self.assertTrue(data["meta"]["realizer_found"])


if __name__ == "__main__":
    unittest.main()
