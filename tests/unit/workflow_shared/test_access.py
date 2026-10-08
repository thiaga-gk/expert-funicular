"""Shared read-only Databricks access (src/workflow_shared/databricks/access.py) under an injected product identity.

A neutral AccessConfig (its own logger, user agent and error type) shows that nothing product-specific is hard-coded:
the OAuth-only rule, the scrubbed child environment, GET-only REST and the UTC-pinned SQL session.
"""
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from workflow_shared.databricks import access  # noqa: E402
from workflow_shared.databricks.vocabulary import SIZE_ORDER  # noqa: E402


class ToolError(Exception):
    pass


CONFIG = access.AccessConfig(logger=logging.getLogger("workflow_shared.test"), user_agent="test-tool/9.9.9", error_type=ToolError)


def fake_connector(timezone):
    calls = {}

    class Cursor:
        description = [("tz",)]
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, statement): calls.setdefault("statements", []).append(statement)
        def fetchall(self): return [(timezone,)]

    class Connection:
        def cursor(self): return Cursor()
        def close(self): calls["closed"] = True

    def connect(**kwargs):
        calls["connect"] = kwargs
        return Connection()

    sql_mod = types.ModuleType("databricks.sql"); sql_mod.connect = connect
    pkg = types.ModuleType("databricks"); pkg.sql = sql_mod
    return calls, {"databricks": pkg, "databricks.sql": sql_mod}


class FakeResponse:
    status = 200
    def __init__(self, body, headers=None):
        self._body, self.headers = body, types.SimpleNamespace(items=lambda: list((headers or {}).items()))
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body.encode("utf-8")


class ProfileTests(unittest.TestCase):
    def config_file(self, td, text):
        path = Path(td) / ".databrickscfg"
        path.write_text(text)
        return str(path)

    def test_profile_host_drops_scheme_and_trailing_slash(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config_file(td, "[P]\nhost = https://dbc.example.cloud.databricks.com/\n")
            self.assertEqual(access.load_profile_host(CONFIG, "P", cfg), "dbc.example.cloud.databricks.com")

    def test_missing_profile_raises_the_injected_error(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config_file(td, "[P]\nhost = h\n")
            with self.assertRaisesRegex(ToolError, "Databricks CLI profile not found: Q"):
                access.load_profile_values(CONFIG, "Q", cfg)

    def test_pat_profile_is_rejected_with_the_oauth_message(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config_file(td, "[PAT]\nhost = https://h\ntoken = secret\n")
            with patch("subprocess.run") as run, self.assertRaises(ToolError) as ctx:
                access.resolve_cli_token(CONFIG, "PAT", cfg)
        self.assertEqual(str(ctx.exception), "Profile PAT contains a PAT token. Use an OAuth U2M Databricks CLI profile.")
        run.assert_not_called()

    def test_cli_token_runs_without_an_ambient_pat_and_logs_no_token(self):
        class R:
            stdout = '{"access_token":"short-lived","expiry":"2026-10-01T20:00:00Z"}'
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config_file(td, "[P]\nhost = https://h.example\n")
            with patch.dict(os.environ, {"DATABRICKS_TOKEN": "ambient-pat"}), patch("subprocess.run", return_value=R()) as run, \
                    self.assertLogs("workflow_shared.test", "INFO") as logs:
                out = access.resolve_cli_token(CONFIG, "P", cfg)
        self.assertEqual(out, ("h.example", "short-lived", "2026-10-01T20:00:00Z"))
        self.assertEqual(run.call_args.args[0], ["databricks", "auth", "token", "P", "-o", "json"])
        self.assertNotIn("DATABRICKS_TOKEN", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["env"]["DATABRICKS_CONFIG_FILE"], cfg)
        self.assertEqual(logs.output, ["INFO:workflow_shared.test:auth.resolve profile=P host=h.example"])

    def test_malformed_cli_output_raises_the_injected_error_in_one_line(self):
        for stdout in ("not json at all\nwith a second line", "[]", '"a string"', ""):
            class R:
                pass
            R.stdout = stdout
            with self.subTest(stdout=stdout[:12]), tempfile.TemporaryDirectory() as td:
                cfg = self.config_file(td, "[P]\nhost = https://h\n")
                with patch("subprocess.run", return_value=R()), self.assertRaises(ToolError) as ctx:
                    access.resolve_cli_token(CONFIG, "P", cfg)
                self.assertNotIn("\n", str(ctx.exception))
                self.assertIn("profile P", str(ctx.exception))
                self.assertNotIn("second line", str(ctx.exception))

    def test_a_config_file_that_does_not_parse_raises_the_injected_error_in_one_line(self):
        for text in ("host = no section header\n", "[P]\nhost = a\nhost = b\n", "[P\nhost = x\n"):
            with self.subTest(text=text[:12]), tempfile.TemporaryDirectory() as td:
                cfg = self.config_file(td, text)
                with patch("subprocess.run") as run, self.assertRaises(ToolError) as ctx:
                    access.resolve_cli_token(CONFIG, "P", cfg)
                self.assertNotIn("\n", str(ctx.exception))
                self.assertIn("could not be parsed", str(ctx.exception))
                self.assertNotIn("no section header", str(ctx.exception))
                run.assert_not_called()

    def test_the_token_is_not_in_the_repr_of_the_auth_context(self):
        auth = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "very-secret-token", None, "warehouse", "x")
        self.assertNotIn("very-secret-token", repr(auth))
        self.assertEqual(auth.access_token, "very-secret-token")

    def test_missing_cli_raises_the_injected_error(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config_file(td, "[P]\nhost = https://h\n")
            with patch("subprocess.run", side_effect=FileNotFoundError()), self.assertRaisesRegex(ToolError, "not installed"):
                access.resolve_cli_token(CONFIG, "P", cfg)


class RestTests(unittest.TestCase):
    def test_get_only_with_the_injected_user_agent_and_no_params_or_token_in_logs(self):
        seen = {}
        def urlopen(req, timeout):
            seen["req"], seen["timeout"] = req, timeout
            return FakeResponse('{"ok": true}')
        with patch("urllib.request.urlopen", urlopen), self.assertLogs("workflow_shared.test", "INFO") as logs:
            out = access.api_get(CONFIG, "h.example/", "secret-token", "/api/2.1/clusters/get", {"cluster_id": "c-1", "skip": None})
        req = seen["req"]
        self.assertEqual(out, {"ok": True})
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, "https://h.example/api/2.1/clusters/get?cluster_id=c-1")
        self.assertEqual(req.get_header("User-agent"), "test-tool/9.9.9")
        self.assertEqual(seen["timeout"], 30)
        text = "\n".join(logs.output)
        self.assertIn("api.get start path=/api/2.1/clusters/get", text)
        self.assertRegex(text, r"api.get done path=/api/2.1/clusters/get status=200 elapsed_ms=\d+")
        for secret in ("secret-token", "c-1", "Bearer"):
            self.assertNotIn(secret, text)

    def test_failure_is_logged_by_class_and_reraised(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")), \
                self.assertLogs("workflow_shared.test", "WARNING") as logs, self.assertRaises(urllib.error.URLError):
            access.api_get(CONFIG, "h", "t", "/p")
        self.assertEqual(logs.output, ["WARNING:workflow_shared.test:api.get failed path=/p error=URLError"])

    def test_workspace_id_comes_from_the_org_id_header(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse("{}", {"X-Databricks-Org-Id": " 42 "})):
            self.assertEqual(access.current_workspace_id(CONFIG, "h", "t"), "42")
        with patch("urllib.request.urlopen", return_value=FakeResponse("{}")), self.assertLogs("workflow_shared.test", "WARNING") as logs:
            self.assertIsNone(access.current_workspace_id(CONFIG, "h", "t"))
        self.assertIn("reason=ORG_ID_HEADER_MISSING", logs.output[-1])


class SqlClientTests(unittest.TestCase):
    auth = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "token", None, "warehouse", "x")

    def test_session_is_pinned_to_utc_with_the_injected_user_agent(self):
        calls, modules = fake_connector("UTC")
        with patch.dict(sys.modules, modules), self.assertLogs("workflow_shared.test", "DEBUG") as logs:
            with access.SqlClient(self.auth, CONFIG) as client:
                rows = client.query("SELECT 'secret sql' AS tz", label="probe")
        self.assertEqual(rows, [{"tz": "UTC"}])
        self.assertEqual(calls["connect"]["user_agent_entry"], "test-tool/9.9.9")
        self.assertEqual(calls["connect"]["session_configuration"], {"TIMEZONE": "UTC"})
        self.assertEqual(calls["connect"]["access_token"], "token")
        text = "\n".join(logs.output)
        self.assertIn("sql.session_ready time_zone_utc=true", text)
        self.assertRegex(text, r"sql.done label=probe rows=1 elapsed_ms=\d+")
        self.assertIn("sql.closed", text)
        self.assertNotIn("secret sql", text)
        self.assertTrue(calls["closed"])

    def test_non_utc_session_closes_and_raises_the_injected_error(self):
        calls, modules = fake_connector("America/Los_Angeles")
        with patch.dict(sys.modules, modules), self.assertRaisesRegex(ToolError, "SQL session time zone is America/Los_Angeles, not UTC"):
            with access.SqlClient(self.auth, CONFIG):
                pass
        self.assertTrue(calls["closed"])


class TokenRefreshTests(unittest.TestCase):
    NOW = access.dt.datetime(2026, 10, 8, 12, 0, tzinfo=access.dt.timezone.utc)

    def test_expiry_within_the_margin_reads_cli_nanoseconds_and_offsets(self):
        self.assertTrue(access.token_expires_within("2026-10-08T05:03:00.123456789-07:00", now=self.NOW))
        self.assertFalse(access.token_expires_within("2026-10-08T12:30:00Z", now=self.NOW))
        self.assertTrue(access.token_expires_within("2026-10-08T11:00:00", now=self.NOW))

    def test_a_missing_or_unreadable_expiry_never_triggers_a_refresh(self):
        for value in (None, "", "tomorrow"):
            self.assertFalse(access.token_expires_within(value, now=self.NOW))

    def test_an_expiring_token_is_minted_again_and_the_connection_reopened(self):
        calls, modules = fake_connector("UTC")
        connects = []
        connect = modules["databricks.sql"].connect
        modules["databricks.sql"].connect = lambda **kw: (connects.append(kw["access_token"]), connect(**kw))[1]
        first = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "old", "2026-10-08T12:03:00Z", "warehouse", "x")
        fresh = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "new", "2026-10-08T13:00:00Z", "warehouse", "x")
        # Valid when the connection opens, within the margin when the first statement starts (a long collection).
        clock = iter([False, False, True, False, False, False])
        with patch.dict(sys.modules, modules), patch.object(access, "token_expires_within", lambda *a, **k: next(clock)), \
                self.assertLogs("workflow_shared.test", "INFO") as logs:
            with access.SqlClient(first, CONFIG, refresh=lambda: fresh) as client:
                client.query("SELECT 1 AS tz", label="first")
                client.query("SELECT 1 AS tz", label="second")
        self.assertEqual(connects, ["old", "new"])
        self.assertIn("sql.token_refresh label=first", "\n".join(logs.output))
        self.assertNotIn("new", "\n".join(logs.output).replace("sql.token_refresh", ""))

    def test_a_token_already_expiring_at_connect_is_minted_before_connecting(self):
        calls, modules = fake_connector("UTC")
        expired = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "old", "2000-01-01T00:00:00Z", "warehouse", "x")
        fresh = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "new", "2999-01-01T00:00:00Z", "warehouse", "x")
        with patch.dict(sys.modules, modules):
            with access.SqlClient(expired, CONFIG, refresh=lambda: fresh) as client:
                client.query("SELECT 1 AS tz", label="only")
        self.assertEqual(calls["connect"]["access_token"], "new")

    def test_without_refresh_the_token_is_used_as_given(self):
        calls, modules = fake_connector("UTC")
        expiring = access.AuthContext("P", "h", "/sql/1.0/warehouses/x", "old", "2000-01-01T00:00:00Z", "warehouse", "x")
        with patch.dict(sys.modules, modules):
            with access.SqlClient(expiring, CONFIG) as client:
                client.query("SELECT 1 AS tz", label="only")
        self.assertEqual(calls["connect"]["access_token"], "old")


class VocabularyTests(unittest.TestCase):
    def test_size_order_is_the_ten_sizes_smallest_first(self):
        self.assertEqual(SIZE_ORDER, ["2X_SMALL", "X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE",
                                      "2X_LARGE", "3X_LARGE", "4X_LARGE", "5X_LARGE"])


if __name__ == "__main__":
    unittest.main()
