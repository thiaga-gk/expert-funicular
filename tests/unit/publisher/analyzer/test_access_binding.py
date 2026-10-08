"""The publisher's binding of the shared Databricks access layer (src/publisher/analyzer/collector).

Pins the product identity the publisher injects: the user agent is exactly ``sql-warehouse-optimizer/<version>`` on
REST and on the SQL connector, log lines go to the ``publisher`` logger and refusals are ``PublisherError``. A live run
is the only path that exercises this, and the ref comparison never runs live, so these tests guard it.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector import sql_executor, workspace_api  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.config import defaults  # noqa: E402
from publisher.config.versions import PUBLISHER_VERSION  # noqa: E402
from tests.unit.workflow_shared.test_access import FakeResponse, fake_connector  # noqa: E402
from workflow_shared.databricks import access, vocabulary  # noqa: E402

USER_AGENT = f"sql-warehouse-optimizer/{PUBLISHER_VERSION}"


class PublisherBindingTests(unittest.TestCase):
    def test_the_binding_injects_the_publisher_identity(self):
        self.assertEqual(workspace_api.PUBLISHER_ACCESS.user_agent, USER_AGENT)
        self.assertIs(workspace_api.PUBLISHER_ACCESS.logger, workspace_api.LOGGER)
        self.assertEqual(workspace_api.PUBLISHER_ACCESS.logger.name, "publisher")
        self.assertIs(workspace_api.PUBLISHER_ACCESS.error_type, PublisherError)
        self.assertIs(workspace_api.AuthContext, access.AuthContext)

    def test_rest_user_agent_and_logs(self):
        seen = {}
        def urlopen(req, timeout):
            seen["req"] = req
            return FakeResponse("{}")
        with patch("urllib.request.urlopen", urlopen), self.assertLogs("publisher", "DEBUG") as logs:
            workspace_api._api_get("h", "t", "/api/2.0/sql/config/warehouses")
        self.assertEqual(seen["req"].get_header("User-agent"), USER_AGENT)
        self.assertEqual(seen["req"].get_method(), "GET")
        self.assertEqual(logs.output[0], "DEBUG:publisher:api.get start path=/api/2.0/sql/config/warehouses")

    def test_sql_client_user_agent_logs_and_error(self):
        auth = workspace_api.AuthContext("P", "h", "/sql/1.0/warehouses/x", "token", None, "warehouse", "x")
        calls, modules = fake_connector("Etc/UTC")
        with patch.dict(sys.modules, modules), self.assertLogs("publisher", "INFO") as logs:
            with sql_executor.SqlClient(auth) as client:
                self.assertIsInstance(client, access.SqlClient)
        self.assertEqual(calls["connect"]["user_agent_entry"], USER_AGENT)
        self.assertIn("INFO:publisher:sql.session_ready time_zone_utc=true", logs.output)
        calls, modules = fake_connector("GMT+1")
        with patch.dict(sys.modules, modules), self.assertRaises(PublisherError):
            with sql_executor.SqlClient(auth):
                pass

    def test_missing_connector_is_a_publisher_error(self):
        auth = workspace_api.AuthContext("P", "h", "/p", "token", None, "warehouse", "x")
        with patch.dict(sys.modules, {"databricks": None, "databricks.sql": None}), \
                self.assertRaisesRegex(PublisherError, "databricks-sql-connector"):
            with sql_executor.SqlClient(auth):
                pass

    def test_profile_refusals_are_publisher_errors_logged_to_publisher(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".databrickscfg"
            cfg.write_text("[PAT]\nhost = https://h\ntoken = secret\n[OK]\nhost = https://h\n")
            with self.assertRaisesRegex(PublisherError, "^Profile PAT contains a PAT token. Use an OAuth U2M Databricks CLI profile.$"):
                workspace_api._resolve_cli_token("PAT", str(cfg))
            with self.assertRaisesRegex(PublisherError, "Databricks CLI profile not found: NONE"):
                workspace_api._load_profile_values("NONE", str(cfg))  # the golden-miner preflight catches PublisherError
            class R:
                stdout = '{"access_token":"tok"}'
            with patch("publisher.analyzer.collector.workspace_api.subprocess.run", return_value=R()) as run, \
                    self.assertLogs("publisher", "INFO") as logs:
                self.assertEqual(workspace_api._resolve_cli_token("OK", str(cfg)), ("h", "tok", None))
            run.assert_called_once()
        self.assertEqual(logs.output, ["INFO:publisher:auth.resolve profile=OK host=h"])

    def test_size_order_is_the_shared_vocabulary(self):
        self.assertIs(defaults.SIZE_ORDER, vocabulary.SIZE_ORDER)


if __name__ == "__main__":
    unittest.main()
