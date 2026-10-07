"""databricks-golden-miner skill scripts (.claude/skills/databricks-golden-miner/scripts): miner, preflight, case maker.

The demo fleet is the oracle: each simulated warehouse carries its golden_scenario tag, so the miner must match every
one to its own catalog row, find no behaviour mismatch on rows that pass, and give byte-identical reports on reruns.
The preflight runs against a fake `databricks` executable, so no live access is needed.
"""
import contextlib
import io
import json
import logging
import os
import re
import stat
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".claude" / "skills" / "databricks-golden-miner" / "scripts"
for p in (ROOT, ROOT / "src", SCRIPTS):
    sys.path.insert(0, str(p))
import cli_sql  # noqa: E402
import make_case  # noqa: E402
import mine_patterns  # noqa: E402
import preflight  # noqa: E402
import real_data_queries  # noqa: E402
from publisher.cli.main import main as publisher_main  # noqa: E402

REGISTRY = ROOT / "docs" / "proposals" / "publisher" / "golden-scenarios.md"
FAKE_CLI = """#!/bin/sh
case "$1 $2" in
  "--version "*) echo "Databricks CLI v0.230.0";;
  "auth token") echo '{"access_token":"not-a-real-token","expiry":"2026-10-05T12:00:00Z"}';;
  "current-user me") echo '{"userName":"someone@example.com"}';;
  "warehouses get") echo '{"name":"Collector","warehouse_type":"PRO","state":"RUNNING"}';;
  "api post") echo '{"statement_id":"s1","status":{"state":"SUCCEEDED"},"manifest":{"schema":{"columns":[{"name":"ok"}]}},"result":{"data_array":[["1"]]}}';;
  *) exit 1;;
esac
"""


def _quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = fn(*args)
    return code, out.getvalue()


class GoldenMinerDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir = Path(cls.tmp.name) / "run"
        code, _ = _quiet(publisher_main, ["--demo", "--output-dir", str(cls.run_dir), "--flat-output", "--no-workbook"])
        assert code == 0
        logging.disable(logging.CRITICAL)
        code, _ = _quiet(mine_patterns.main, ["--run-dir", str(cls.run_dir), "--out", str(Path(cls.tmp.name) / "a")])
        assert code == 0
        cls.report = json.loads((Path(cls.tmp.name) / "a" / "pattern_report.json").read_text())
        evidence = json.loads((cls.run_dir / "evidence.json").read_text())["warehouses"]
        cls.tag = {f"{w['workspace_id']}/{w['warehouse_id']}": next(e for e in w["epochs"] if e["is_current"])["tags"]["golden_scenario"]
                   for w in evidence}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)
        cls.tmp.cleanup()

    def test_every_demo_warehouse_matches_its_own_row(self):
        missed = {w["warehouse"]: self.tag[w["warehouse"]] for w in self.report["warehouses"] if self.tag[w["warehouse"]] not in w["matched_rows"]}
        self.assertEqual(missed, {})
        self.assertEqual(self.report["warehouse_count"], len(self.tag))

    def test_no_behaviour_mismatch_on_the_demo_fleet(self):
        self.assertEqual(self.report["mismatches"], [])

    def test_report_is_deterministic(self):
        b = Path(self.tmp.name) / "b"
        _quiet(mine_patterns.main, ["--run-dir", str(self.run_dir), "--out", str(b)])
        a = Path(self.tmp.name) / "a"
        for name in ("pattern_report.json", "pattern_report.md"):
            self.assertEqual((a / name).read_bytes(), (b / name).read_bytes(), name)

    def test_a_contradicting_decision_is_a_mismatch(self):
        warehouses, packages = mine_patterns.load_run(self.run_dir)
        key = next(k for k in packages if self.tag[f"{k[0]}/{k[1]}"] == "C07")  # under-sized: never downsized
        size = next(c for c in packages[key]["candidates"] if c["knob"] == "warehouse_size")
        size["recommended_value"] = "X_SMALL"
        catalog = mine_patterns.load_catalog(mine_patterns.DEFAULT_CATALOG)
        report = mine_patterns.mine(warehouses, packages, catalog, None, None)
        self.assertEqual([(m["row"], m["warehouse"]) for m in report["mismatches"]], [("C07", f"{key[0]}/{key[1]}")])

    def test_no_premise_contradiction_on_the_demo_fleet(self):
        self.assertEqual(self.report["premise_contradictions"], [])
        self.assertTrue(all(p["live"]["holds"] for p in self.report["premises"]), self.report["premises"])

    def test_premises_assume_the_current_publisher_settings(self):
        # A premise names the setting it rests on; when that setting changes, this fails so the premise is re-read.
        for p in mine_patterns.load_catalog(mine_patterns.DEFAULT_CATALOG)["premises"]:
            self.assertEqual(mine_patterns.assumed_value(p["assumes"]["name"]), p["assumes"]["value"], p["id"])

    def test_a_contradicted_premise_is_reported_and_unknown_evidence_is_not(self):
        warehouses, packages = mine_patterns.load_run(self.run_dir)
        catalog = mine_patterns.load_catalog(mine_patterns.DEFAULT_CATALOG)
        ref = next(r for r, t in self.tag.items() if t == "C13")  # cold-start-sensitive Pro: restart premise applies
        w = next(x for x in warehouses if f"{x['workspace_id']}/{x['warehouse_id']}" == ref)
        query = next(e for e in w["epochs"] if e["is_current"])["query"]
        query["p95_wait_compute_ms_when_waited_30"] = 900000  # 15-minute starts, not about 4
        found = mine_patterns.mine(warehouses, packages, catalog, None, None)["premise_contradictions"]
        self.assertEqual([(c["premise"], c["warehouse"]) for c in found], [("P-RESTART-PRO", ref)])
        self.assertIn("C13", found[0]["rows"])
        query["p95_wait_compute_ms_when_waited_30"] = None  # no evidence: unknown, never contradicted
        report = mine_patterns.mine(warehouses, packages, catalog, None, None)
        self.assertEqual(report["premise_contradictions"], [])
        restart = next(p for p in report["premises"] if p["premise"] == "P-RESTART-PRO")
        self.assertGreater(restart["live"]["unknown"], next(p for p in self.report["premises"] if p["premise"] == "P-RESTART-PRO")["live"]["unknown"])

    def test_catalog_covers_exactly_the_registry_rows(self):
        rows = set(re.findall(r"^\| ([CA]\d{2}) \|", REGISTRY.read_text(encoding="utf-8"), re.M))
        catalog = {r["id"] for r in mine_patterns.load_catalog(mine_patterns.DEFAULT_CATALOG)["rows"]}
        self.assertEqual(sorted(rows - catalog), [], "registry rows missing from signature-catalog.yaml")
        self.assertEqual(sorted(catalog - rows), [], "catalog rows that are not registry rows")

    def test_make_case_writes_raw_evidence_and_never_overwrites(self):
        cases = Path(self.tmp.name) / "cases"
        ref = next(r for r, t in self.tag.items() if t == "C04")
        args = ["--run-dir", str(self.run_dir), "--warehouse", ref, "--row", "C04", "--slug", "keepalive-live",
                "--description", "probe", "--cases-dir", str(cases)]
        self.assertEqual(_quiet(make_case.main, args)[0], 0)
        folder = cases / "c04-keepalive-live"
        source = json.loads((folder / "source" / "evidence.json").read_text())
        self.assertEqual([f"{w['workspace_id']}/{w['warehouse_id']}" for w in source], [ref])
        self.assertIn("origin: live", (folder / "case.yaml").read_text())
        self.assertEqual(_quiet(make_case.main, args)[0], 2)


class FindRunTests(unittest.TestCase):
    """find_run resolves a flat publisher output, a Hive run folder or a parent holding exactly one run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, *parts):
        folder = self.base.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "evidence.json").write_text("{}")
        return folder

    def test_a_flat_output_folder_is_the_run(self):
        flat = self.touch("mining_out", "2026-10-07", "pub_full")
        self.assertEqual(mine_patterns.find_run(flat), flat)

    def test_a_single_hive_run_below_the_output_dir_is_found(self):
        run = self.touch("out", "run_date=2026-10-07", "run_ts=20261007T120000Z")
        self.assertEqual(mine_patterns.find_run(self.base / "out"), run)

    def test_sibling_runs_are_refused_and_listed_so_the_user_can_pick(self):
        for name in ("pub_full", "pub_autostop", "rz"):
            self.touch("mining_out", "2026-10-07", name)
        with self.assertRaises(SystemExit) as raised:
            mine_patterns.find_run(self.base / "mining_out" / "2026-10-07")
        message = str(raised.exception)
        for name in ("pub_full", "pub_autostop", "rz"):
            self.assertIn(name, message)
        self.assertIn("--run-dir", message)

    def test_two_hive_runs_are_refused_with_both_listed(self):
        self.touch("out", "run_date=2026-10-07", "run_ts=20261007T120000Z")
        self.touch("out", "run_date=2026-10-08", "run_ts=20261008T120000Z")
        with self.assertRaises(SystemExit) as raised:
            mine_patterns.find_run(self.base / "out")
        self.assertIn("run_ts=20261007T120000Z", str(raised.exception))
        self.assertIn("run_ts=20261008T120000Z", str(raised.exception))

    def test_no_run_is_an_error(self):
        (self.base / "empty").mkdir()
        with self.assertRaises(SystemExit):
            mine_patterns.find_run(self.base / "empty")


class ScopedRunTests(unittest.TestCase):
    """A scoped run lacks the candidates of out-of-scope knobs, so the expected behaviour cannot be judged on it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir = Path(cls.tmp.name) / "scoped"
        code, _ = _quiet(publisher_main, ["--demo", "--scope", "current:auto-stop", "--output-dir", str(cls.run_dir),
                                          "--flat-output", "--no-workbook"])
        assert code == 0
        logging.disable(logging.CRITICAL)
        cls.out = Path(cls.tmp.name) / "report"
        code, cls.stdout = _quiet(mine_patterns.main, ["--run-dir", str(cls.run_dir), "--out", str(cls.out)])
        assert code == 0
        cls.report = json.loads((cls.out / "pattern_report.json").read_text())

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)
        cls.tmp.cleanup()

    def test_packages_carry_a_run_scope(self):
        _, packages = mine_patterns.load_run(self.run_dir)
        self.assertTrue(packages and all(p.get("run_scope") for p in packages.values()))

    def test_a_scoped_run_reports_no_false_mismatch(self):
        self.assertEqual(self.report["mismatches"], [])

    def test_the_report_states_that_mismatches_were_not_evaluated(self):
        scope = self.report["run_scope"]
        self.assertTrue(scope["scoped"])
        self.assertFalse(scope["mismatches_evaluated"])
        self.assertEqual(scope["scoped_packages"], self.report["warehouse_count"])
        self.assertEqual(scope["full_scope_packages"], 0)
        md = (self.out / "pattern_report.md").read_text()
        self.assertIn("Scoped run", md)
        self.assertIn("not evaluated", md.split("## Coverage")[0])
        self.assertIn("Not evaluated", md.split("## Behaviour mismatches")[1].split("##")[0])
        self.assertIn("scoped", self.stdout)

    def test_coverage_and_drift_still_come_from_the_evidence(self):
        self.assertTrue(any(row["warehouses"] for row in self.report["coverage"]["rows"]))
        self.assertTrue(self.report["drift"])

    def test_only_scoped_packages_are_skipped_in_a_mixed_run(self):
        warehouses, packages = mine_patterns.load_run(self.run_dir)
        catalog = mine_patterns.load_catalog(mine_patterns.DEFAULT_CATALOG)
        key = next(iter(packages))
        # A full-scope package for one warehouse (run_scope None) that contradicts every size expectation.
        for k, pkg in packages.items():
            if k != key:
                continue
            pkg["run_scope"] = None
            for c in pkg["candidates"]:
                c["status"] = "BLOCKED"
                c["recommended_value"] = None
        report = mine_patterns.mine(warehouses, packages, catalog, None, None)
        self.assertEqual(report["run_scope"]["full_scope_packages"], 1)
        self.assertEqual(report["run_scope"]["scoped_packages"], len(packages) - 1)
        self.assertTrue(report["run_scope"]["mismatches_evaluated"])
        self.assertTrue(report["run_scope"]["mismatches_evaluated_for"] == [f"{key[0]}/{key[1]}"])
        self.assertTrue(report["mismatches"])
        self.assertTrue(all(m["warehouse"] == f"{key[0]}/{key[1]}" for m in report["mismatches"]))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        cli = base / "bin" / "databricks"
        cli.parent.mkdir()
        cli.write_text(FAKE_CLI)
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
        self.cfg = base / "databrickscfg"
        self.cfg.write_text("[PAT]\nhost = https://x.cloud.databricks.com\ntoken = dapi-not-real\n"
                            "[OAUTH]\nhost = https://x.cloud.databricks.com\nauth_type = databricks-cli\n")
        self.env = {"PATH": f"{cli.parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_preflight(self, profile):
        with unittest.mock.patch.dict(os.environ, self.env):
            return _quiet(preflight.main, ["--profile", profile, "--warehouse-id", "abc", "--config-file", str(self.cfg)])

    def test_oauth_profile_passes_every_step(self):
        code, out = self.run_preflight("OAUTH")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("PASS"), 5 + len(preflight.SYSTEM_TABLES))
        self.assertNotIn("not-a-real-token", out)

    def test_pat_profile_fails_closed_with_the_oauth_fix(self):
        code, out = self.run_preflight("PAT")
        self.assertEqual(code, 1)
        self.assertIn("FAIL OAuth profile", out)
        self.assertIn("databricks auth login", out)
        self.assertNotIn("dapi-not-real", out)

    def test_unknown_profile_and_missing_cli_fail_closed(self):
        self.assertEqual(self.run_preflight("NOPE")[0], 1)
        with unittest.mock.patch.dict(os.environ, {"PATH": str(Path(self.tmp.name) / "empty")}):
            code, out = _quiet(preflight.main, ["--profile", "OAUTH", "--warehouse-id", "abc", "--config-file", str(self.cfg)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL databricks CLI", out)


class CliSqlGuardTests(unittest.TestCase):
    """execute() sends only a single read statement: comments are ignored, DML/DDL words outside literals are refused."""

    def refused(self, sql):
        with self.assertRaises(cli_sql.CliError):
            cli_sql.check_read_only(sql)

    def test_plain_select_and_with_pass(self):
        for sql in ("SELECT 1", "  select * from system.compute.warehouses;", "WITH a AS (SELECT 1 AS x) SELECT x FROM a",
                    "SELECT update_time, created_by, `copy` FROM t WHERE note = 'drop table x; -- not a comment'"):
            cli_sql.check_read_only(sql)

    def test_leading_comments_are_ignored(self):
        cli_sql.check_read_only("-- the G5 check\n-- second line\nSELECT 1")
        cli_sql.check_read_only("/* header\n  block */ SELECT 1 /* trailing */ -- more")
        cli_sql.check_read_only("/* outer /* nested */ still comment */ SELECT 1")

    def test_a_comment_cannot_hide_the_statement_kind(self):
        self.refused("-- SELECT 1\nDROP TABLE t")
        self.refused("/* SELECT */ DELETE FROM t")
        self.refused("SELECT 1 /* unterminated")

    def test_a_cte_ending_in_dml_is_refused(self):
        self.refused("WITH a AS (SELECT 1 AS id) INSERT INTO t SELECT id FROM a")
        self.refused("WITH a AS (SELECT 1 AS id) MERGE INTO t USING a ON t.id = a.id WHEN MATCHED THEN DELETE")
        self.refused("with a as (select 1) delete from t")

    def test_every_write_keyword_is_refused_as_a_whole_word(self):
        for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE",
                     "COPY", "OPTIMIZE", "VACUUM"):
            self.refused(f"SELECT 1 FROM t WHERE x IN (SELECT 1) {word.lower()} x")
        self.refused("SELECT 1; DROP TABLE t")

    def test_keywords_inside_literals_and_identifiers_do_not_trip_the_guard(self):
        cli_sql.check_read_only("SELECT 'a '' delete ' AS s, \"vacuum\" AS q, `optimize` FROM t WHERE c = 'x\\' drop'")
        cli_sql.check_read_only("SELECT updated_at, grant_id, insert_count FROM t")

    def test_non_select_starts_are_refused(self):
        for sql in ("", "   ", "SHOW TABLES", "DESCRIBE t", "USE x", "-- only a comment"):
            self.refused(sql)

    def test_every_statement_of_the_real_data_run_doc_passes_the_guard(self):
        for item, sqls in real_data_queries.statements(real_data_queries.DOC.read_text(encoding="utf-8")).items():
            for sql in sqls:
                cli_sql.check_read_only(sql)

    def test_execute_refuses_before_calling_the_cli(self):
        with unittest.mock.patch.object(cli_sql, "run_cli") as run:
            with self.assertRaises(cli_sql.CliError):
                cli_sql.execute("WITH a AS (SELECT 1) DROP TABLE t", "w", "p")
        run.assert_not_called()


class CliRetryTests(unittest.TestCase):
    """429 and transient 5xx answers from the CLI are retried a few times with a short backoff; anything else is not."""

    def completed(self, code, out="", err=""):
        return unittest.mock.Mock(returncode=code, stdout=out, stderr=err)

    def run_with(self, results):
        sleeps = []
        with unittest.mock.patch.object(cli_sql.subprocess, "run", side_effect=results) as run, \
                unittest.mock.patch.object(cli_sql, "_sleep", sleeps.append):
            try:
                out = cli_sql.run_cli(["api", "get", "/x"], "p")
            except cli_sql.CliError as exc:
                out = exc
        return out, run.call_count, sleeps

    def test_429_is_retried_until_it_succeeds(self):
        out, calls, sleeps = self.run_with([self.completed(1, err="Error: 429 Too Many Requests"),
                                            self.completed(1, err="Error: REQUEST_LIMIT_EXCEEDED rate limit"),
                                            self.completed(0, out="{}")])
        self.assertEqual((out, calls, len(sleeps)), ("{}", 3, 2))
        self.assertTrue(all(0 < s <= 10 for s in sleeps) and sleeps == sorted(sleeps))

    def test_transient_5xx_is_retried_and_gives_up_after_three_attempts(self):
        out, calls, sleeps = self.run_with([self.completed(1, err="Error: 503 Service Unavailable")] * 5)
        self.assertIsInstance(out, cli_sql.CliError)
        self.assertIn("503", str(out))
        self.assertEqual(calls, 3)
        self.assertEqual(len(sleeps), 2)

    def test_other_errors_are_not_retried(self):
        for err in ("Error: 403 Forbidden", "Error: PERMISSION_DENIED", "Error: 400 Bad Request: PARSE_SYNTAX_ERROR"):
            out, calls, sleeps = self.run_with([self.completed(1, err=err)] * 3)
            self.assertIsInstance(out, cli_sql.CliError, err)
            self.assertEqual((calls, sleeps), (1, []), err)


class RealDataQueryTests(unittest.TestCase):
    def test_statements_come_from_the_real_data_run_doc(self):
        items = real_data_queries.statements(real_data_queries.DOC.read_text(encoding="utf-8"))
        self.assertEqual(sorted(items), ["G4b", "G5", "S4"])
        self.assertEqual(len(items["G5"]), 2)
        for sqls in items.values():
            for sql in sqls:
                self.assertRegex(sql.lstrip().upper(), r"^(SELECT|WITH)\b")


class RealDataRunnerTests(unittest.TestCase):
    """A failed statement is recorded and the run goes on: one bad query must not lose the other answers."""

    def run_main(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "answers.json"
            code, printed = _quiet(lambda: real_data_queries.main(
                ["--profile", "p", "--warehouse-id", "w", "--out", str(out)], runner=runner))
            return code, printed, json.loads(out.read_text()) if out.exists() else None

    def test_all_statements_pass_exit_zero_with_an_empty_errors_section(self):
        calls = []

        def runner(sql, warehouse_id, profile, config_file=None):
            calls.append(sql)
            return {"columns": ["n"], "rows": [["1"]]}

        code, _, doc = self.run_main(runner)
        self.assertEqual(code, 0)
        self.assertEqual(doc["errors"], {})
        self.assertEqual(sorted(doc["answers"]), ["G4b", "G5", "S4"])
        self.assertEqual(sum(len(v) for v in doc["answers"].values()), len(calls))

    def test_a_failing_statement_is_recorded_and_the_rest_still_run(self):
        calls = []
        secret = "SELECT secret_column FROM hidden.table"

        def runner(sql, warehouse_id, profile, config_file=None):
            calls.append(sql)
            if len(calls) == 2:
                raise real_data_queries.CliError(f"statement FAILED: PARSE_ERROR near {secret}\nsecond line")
            return {"columns": ["n"], "rows": [["1"]]}

        total = sum(len(v) for v in real_data_queries.statements(real_data_queries.DOC.read_text(encoding="utf-8")).values())
        code, printed, doc = self.run_main(runner)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), total, "later statements must still run")
        self.assertEqual(sum(len(v) for v in doc["answers"].values()), total - 1)
        (item, errors), = doc["errors"].items()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_class"], "CliError")
        self.assertNotIn("second line", errors[0]["message"])
        self.assertNotIn("statement", errors[0])
        self.assertIn(item, printed)

    def test_an_unexpected_exception_class_is_recorded_too(self):
        def runner(sql, warehouse_id, profile, config_file=None):
            raise ValueError("bad json")

        code, _, doc = self.run_main(runner)
        self.assertEqual(code, 1)
        self.assertEqual(doc["answers"], {item: [] for item in ("G4b", "G5", "S4")})
        self.assertEqual({e["error_class"] for errs in doc["errors"].values() for e in errs}, {"ValueError"})


if __name__ == "__main__":
    unittest.main()
