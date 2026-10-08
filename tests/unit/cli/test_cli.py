"""CLI adapter (src/publisher/cli): option parity with the single-file CLI, dependency direction, exit codes."""
import argparse
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.cli import publish as cli_publish  # noqa: E402
from publisher.cli.main import main  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402
from publisher.contracts.request import MODE_DEMO, PublisherRequest  # noqa: E402
from publisher.publisher import build_context  # noqa: E402

ENV_DEFAULTS = ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_SQL_WAREHOUSE_ID", "DATABRICKS_SERVERLESS_WAREHOUSE_ID",
                "DATABRICKS_ALL_PURPOSE_CLUSTER_ID", "DATABRICKS_WORKSPACE_ID", "DATABRICKS_CONFIG_FILE", "DATABRICKS_REGION",
                "AWS_PRICE_REGISTRY", "WAREHOUSE_BATCH_SIZE", "COMPATIBILITY_API_MODE", "PUBLISHER_LOG_LEVEL", "PUBLISHER_EXECUTION_MODE")
CAPABILITIES = ("analyzer", "governor", "optimizer", "reviewer", "packager", "reporter")


def option_table():
    """The CLI parser's options as plain data (environment-variable defaults unset, repository path normalized)."""
    captured = {}
    original = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        captured["parser"] = self
        return original(self, args, namespace)

    env = {k: v for k, v in os.environ.items() if k not in ENV_DEFAULTS}
    with patch.dict(os.environ, env, clear=True), patch.object(argparse.ArgumentParser, "parse_args", grab):
        cli_publish.parse_args(["--demo"])
    table = []
    for a in captured["parser"]._actions:
        if not a.option_strings or a.dest == "help":
            continue
        default = "<repo>/pricing/aws_ec2_on_demand.json" if a.dest == "aws_price_registry" else a.default
        table.append({"flags": a.option_strings, "dest": a.dest, "default": default, "choices": list(a.choices) if a.choices else None,
                      "type": getattr(a.type, "__name__", None), "action": type(a).__name__, "help": a.help})
    return table


class OptionParityTests(unittest.TestCase):
    def test_every_single_file_option_is_unchanged_and_only_new_options_follow(self):
        before = json.loads((ROOT / "tests" / "fixtures" / "expected" / "cli" / "options.json").read_text())["options"]
        table = option_table()
        versions = [o for o in table if o["flags"] == ["--version"]]
        self.assertEqual([(o["action"], o["dest"], o["default"]) for o in versions], [("_VersionAction", "version", "==SUPPRESS==")])
        after = [o for o in table if o["flags"] != ["--version"]]  # argparse's version action: not one of the single-file options
        self.assertEqual(after[:len(before)], before)
        added = after[len(before):]
        self.assertEqual([o["flags"] for o in added], [["--mode"], ["--cost-profile"], ["--flat-output"], ["--scope"], ["--scope-file"], ["--warehouse"]])
        self.assertEqual((added[0]["choices"], added[0]["default"]), (["local"], "local"))
        self.assertEqual((added[1]["action"], added[1]["default"]), ("_AppendAction", []))
        self.assertEqual([(o["dest"], o["default"]) for o in added[2:]], [("flat_output", False), ("scope", []), ("scope_file", None), ("warehouse", [])])


class DependencyDirectionTests(unittest.TestCase):
    def test_cli_reaches_the_core_only_through_the_publisher_facade(self):
        for path in sorted((ROOT / "src" / "publisher" / "cli").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("publisher."):
                    package = node.module.split(".")[1]
                    self.assertNotIn(package, CAPABILITIES, f"{path.name} imports {node.module}")


class BehaviourTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_publish_subcommand_and_bare_options_are_the_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.run_cli(["publish", "--demo", "--demo-count", "2", "--no-workbook", "--log-level", "ERROR", "--flat-output", "--output-dir", f"{tmp}/a"])
            b = self.run_cli(["--demo", "--demo-count", "2", "--no-workbook", "--log-level", "ERROR", "--flat-output", "--output-dir", f"{tmp}/b"])
            self.assertEqual((a[0], b[0]), (0, 0))
            for name in sorted(p.name for p in Path(f"{tmp}/a/packages").glob("*.json")):
                self.assertEqual(Path(f"{tmp}/a/packages/{name}").read_text(), Path(f"{tmp}/b/packages/{name}").read_text())

    def test_a_refused_run_prints_its_message_and_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(["--demo", "--demo-count", "999", "--log-level", "ERROR", "--output-dir", tmp])
        self.assertEqual(code, 2)
        self.assertIn("--demo-count must be between 1 and", err)

    def test_an_invalid_cost_profile_or_an_unknown_warehouse_refuses_the_run(self):
        for flags, message in ((["--cost-profile", "reckless"], "Unknown cost profile"),
                               (["--cost-profile", "aggressive=wh-typo"], "not among the collected warehouses")):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp:
                code, _, err = self.run_cli(["--demo", "--demo-count", "2", "--no-workbook", "--log-level", "ERROR", "--output-dir", tmp, *flags])
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_a_cost_profile_flag_reaches_every_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self.run_cli(["--demo", "--demo-count", "2", "--no-workbook", "--log-level", "ERROR", "--flat-output", "--output-dir", tmp,
                                       "--cost-profile", "conservative"])
            self.assertEqual(code, 0)
            profiles = {json.loads(f.read_text())["cost_profile"]["source_detail"] for f in Path(f"{tmp}/packages").glob("*.json")}
        self.assertEqual(profiles, {"--cost-profile conservative"})

    def test_a_refused_run_is_recorded_in_publisher_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[]")
            code, _, err = self.run_cli(["--from-evidence", str(bad), "--flat-output", "--output-dir", f"{tmp}/out"])
            log = Path(f"{tmp}/out/publisher.log").read_text()
        self.assertEqual(code, 2)
        self.assertIn(" ERROR publisher.refused exit=2 error=", log)
        self.assertIn("evidence_format_version", log)

    def test_an_unexpected_crash_is_logged_by_type_only_and_reraised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cli_publish, "publish", side_effect=RuntimeError("owner@example.com")):
                with self.assertRaises(RuntimeError):
                    self.run_cli(["--demo", "--flat-output", "--output-dir", tmp])
            log = Path(f"{tmp}/publisher.log").read_text()
        self.assertIn(" ERROR publisher.crashed error_type=RuntimeError", log)
        self.assertNotIn("owner@example.com", log)

    def test_flat_output_refuses_a_folder_that_holds_a_previous_run(self):
        for leftover in ("packages/old.json", "manifest.json"):
            with self.subTest(leftover=leftover), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / leftover
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}")
                code, out, err = self.run_cli(["--demo", "--demo-count", "1", "--no-workbook", "--flat-output", "--output-dir", tmp])
                self.assertEqual(code, 2)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertIn("--flat-output", err)
                self.assertIn("choose a new folder", err)
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), sorted({Path(leftover).parts[0]}))  # nothing written: no log, no evidence

    def test_flat_output_accepts_an_empty_folder_and_one_with_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.txt").write_text("keep")
            (Path(tmp) / "packages").mkdir()  # empty packages/ is not a previous run
            code, _, _ = self.run_cli(["--demo", "--demo-count", "1", "--no-workbook", "--log-level", "ERROR", "--flat-output", "--output-dir", tmp])
            self.assertEqual(code, 0)

    def test_the_default_layout_never_refuses_a_populated_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manifest.json").write_text("{}")
            code, _, _ = self.run_cli(["--demo", "--demo-count", "1", "--no-workbook", "--log-level", "ERROR", "--output-dir", tmp])
            self.assertEqual(code, 0)

    def test_unknown_execution_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--demo", "--mode", "dab"])
        with self.assertRaises(PublisherError):
            build_context(PublisherRequest(mode=MODE_DEMO, output_dir=Path("."), execution_mode="dab"))


if __name__ == "__main__":
    unittest.main()
