"""Realizer CLI adapter (src/realizer/cli): option table, dependency direction, behavior.

The CLI parses its own arguments, loads the package, the observation and the policy (in that order), configures logging,
calls the facade and prints the written package path (or the snapshot with ``--print-json``). It never imports a
capability package. The option table (defaults, values, help text, argparse errors) and, for the demo, replay,
observation-file and policy modes, the exit status, stdout and output tree are stored in tests/fixtures/realizer/expectations/. A refused input exits
with status 2 (tests/unit/realizer/cli/test_refusals.py); a defect is not mapped to a status.
"""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_expect as expect  # noqa: E402
from tests.unit.realizer.helpers import ROOT, canonical, compact_tree, fixture_package, tree  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.cli import realize as cli_realize  # noqa: E402
from realizer.cli.main import main  # noqa: E402
from realizer.common import logging as rlogging  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.realizer import realize  # noqa: E402

CAPABILITIES = ("analyzer", "governor", "reconciler", "estimator", "reviewer", "packager", "reporter")
CLI_DIR = ROOT / "src" / "realizer" / "cli"
PACKAGE = canonical.PACKAGES / "inplace-size-canary.json"


def imported_modules(path):
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


class DependencyDirectionTests(unittest.TestCase):
    def test_cli_reaches_the_core_only_through_the_realizer_facade(self):
        paths = sorted(CLI_DIR.glob("*.py"))
        self.assertEqual([p.name for p in paths], ["__init__.py", "main.py", "realize.py"])
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("realizer"):
                    parts = node.module.split(".")
                    if len(parts) > 1:
                        self.assertNotIn(parts[1], CAPABILITIES, f"{path.name} imports {node.module}")
                    else:  # ``from realizer import <capability>``
                        for alias in node.names:
                            self.assertNotIn(alias.name, CAPABILITIES, f"{path.name} imports {alias.name}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if parts[0] == "realizer" and len(parts) > 1:
                            self.assertNotIn(parts[1], CAPABILITIES, f"{path.name} imports {alias.name}")

    def test_cli_imports_only_the_facade_contracts_config_and_common(self):
        allowed = {"realizer.realizer", "realizer.cli", "realizer.contracts", "realizer.config", "realizer.common"}
        for path in sorted(CLI_DIR.glob("*.py")):
            for module in imported_modules(path):
                if module.startswith("realizer"):
                    self.assertTrue(any(module == a or module.startswith(a + ".") for a in allowed), f"{path.name} imports {module}")

    def test_the_facade_does_not_import_the_cli_at_load(self):
        source = (ROOT / "src" / "realizer" / "realizer.py").read_text(encoding="utf-8")
        top_level = [n for n in ast.parse(source).body if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in top_level:
            self.assertFalse(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("realizer.cli"))


class OptionParityTests(unittest.TestCase):
    def test_parse_args_defaults_and_values(self):
        for index, argv in enumerate((["--package", "p.json"],
                                      ["--package", "p", "--observation", "o", "--output-dir", "x", "--policy", "pol", "--demo", "--demo-modified",
                                       "--replay-through", "2026-10-01T00:00:00Z", "--print-json", "--log-level", "DEBUG"])):
            expect.check(self, f"argv {index}", vars(cli_realize.parse_args(argv)))
        self.assertEqual(cli_realize.parse_args(["--package", "p"]).output_dir, "./warehouse_optimizer_out")

    def test_help_text_and_errors(self):
        def outcome(argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["realizer"]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.dict(os.environ, {"COLUMNS": "100"}), self.assertRaises(SystemExit) as raised:
                cli_realize.parse_args(argv)
            return raised.exception.code, out.getvalue(), err.getvalue()
        for index, argv in enumerate((["--help"], [], ["--package", "p", "--log-level", "LOUD"], ["--package", "p", "--no-such-flag"])):
            expect.check(self, f"argv {index}", outcome(argv))


class BehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.root = tempfile.TemporaryDirectory()
        cls.observation = Path(cls.root.name) / "observation.json"
        cls.observation.write_text(json.dumps(demo_observation(cls.pkg, modified=True)), encoding="utf-8")
        cls.policy = Path(cls.root.name) / "policy.json"
        cls.policy.write_text(json.dumps({"minimum_days": 3}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.root.cleanup()

    def setUp(self):
        saved = (list(rlogging.LOGGER.handlers), rlogging.LOGGER.level, rlogging.LOGGER.propagate)  # main() configures the logger

        def restore():
            for handler in rlogging.LOGGER.handlers:
                handler.close()
            rlogging.LOGGER.handlers, rlogging.LOGGER.level, rlogging.LOGGER.propagate = saved
        self.addCleanup(restore)

    def run_cli(self, entry, argv, out):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = entry(argv + ["--output-dir", str(out)])
            except Exception as exc:  # noqa: BLE001 - compared
                code = f"{type(exc).__name__}: {exc}".replace(str(out), "<out>")
        return code, stdout.getvalue().replace(str(out), "<out>")

    def check_run(self, label, argv, runs=1):
        """Run the CLI *runs* times into one directory; the exit status, stdout and output tree of the last run are the
        stored expectation (the same for every run). Returns ``((code, stdout), tree)``."""
        with tempfile.TemporaryDirectory() as out:
            results = [self.run_cli(main, argv, Path(out)) for _ in range(runs)]
            run_tree = tree(Path(out))
        expect.check(self, label, (results[-1], compact_tree(run_tree)))
        return results[-1], run_tree

    def test_demo_mode(self):
        (code, stdout), files_ = self.check_run("demo", ["--package", str(PACKAGE), "--demo"])
        self.assertEqual(code, 0)
        self.assertRegex(stdout, r"^<out>/realization/packages/RPKG-[0-9A-F]{24}\.json\n$")
        self.assertTrue(any(k.startswith("realization/snapshots/RSNAP-") for k in files_))

    def test_demo_modified_and_print_json(self):
        (code, stdout), _ = self.check_run("demo modified", ["--package", str(PACKAGE), "--demo-modified", "--print-json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["contract_version"], rc.CONTRACT_VERSION)

    def test_replay_modes(self):
        base = ["--package", str(PACKAGE), "--demo"]
        results = []
        with tempfile.TemporaryDirectory() as out:
            for argv in (base, base, [*base, "--replay-through", "2000-01-01T00:00:00Z"], [*base, "--replay-through", "2100-01-01T00:00:00Z"],
                         ["--package", str(PACKAGE), "--replay-through", "2100-01-01T00:00:00Z"]):
                results.append((argv[2:], self.run_cli(main, argv, Path(out)), compact_tree(tree(Path(out)))))
        expect.check(self, "sequence", [(argv[:1] + argv[1:], result, files_) for argv, result, files_ in results])

    def test_observation_file_and_policy_override(self):
        (code, _), files_ = self.check_run("observation file and policy", ["--package", str(PACKAGE), "--observation", str(self.observation), "--policy", str(self.policy)])
        self.assertEqual(code, 0)
        self.assertTrue(any(k.startswith("realization/ledger/") for k in files_))

    def test_lineage_only_run_without_an_observation(self):
        (code, _), files_ = self.check_run("lineage only", ["--package", str(PACKAGE)])
        self.assertEqual(code, 0)
        self.assertFalse(any(k.startswith("realization/evidence/") and v for k, v in files_.items()))

    def test_refused_inputs_exit_with_status_2_and_a_message_not_a_traceback(self):
        bad = Path(self.root.name) / "bad.json"
        bad.write_text(json.dumps({"recommendation_package_id": "PKG-1"}), encoding="utf-8")
        for index, argv in enumerate((["--package", str(bad)], ["--package", str(Path(self.root.name) / "missing.json")],
                                      ["--package", str(PACKAGE), "--observation", str(bad), "--demo-modified", "--policy", str(Path(self.root.name) / "nope.json")])):
            with self.subTest(argv=argv[1:]):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(argv + ["--output-dir", str(Path(self.root.name) / "o1")])
                self.assertEqual((code, out.getvalue()), (2, ""))
                message = err.getvalue().splitlines()[-1]
                self.assertTrue(message.startswith("realizer: "), message)
                expect.check(self, f"case {index}", message.replace(self.root.name, "<tmp>"))
        self.assertFalse((Path(self.root.name) / "o1").exists(), "an input refusal writes nothing")

    def test_a_defect_is_not_mapped_to_an_exit_status(self):
        with mock.patch.object(cli_realize, "realize", side_effect=RuntimeError("boom")), tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            main(["--package", str(PACKAGE), "--demo", "--output-dir", tmp])

    def test_load_order_is_package_then_observation_then_policy(self):
        order = []
        real = cli_realize
        with mock.patch.object(real, "load_package", lambda p: (order.append("package"), json.loads(PACKAGE.read_text()))[1]), \
                mock.patch.object(real, "load_observation", lambda p: (order.append("observation"), json.loads(self.observation.read_text()))[1]), \
                mock.patch.object(real, "load_policy_mapping", lambda p: (order.append("policy"), None)[1]), \
                tempfile.TemporaryDirectory() as tmp:
            self.run_cli(main, ["--package", "p", "--observation", "o"], Path(tmp))
        self.assertEqual(order, ["package", "observation", "policy"])

    def test_logging_is_configured_from_the_flag(self):
        with mock.patch.object(cli_realize, "setup_logging") as setup, tempfile.TemporaryDirectory() as tmp:
            self.run_cli(main, ["--package", str(PACKAGE), "--log-level", "WARNING"], Path(tmp))
        setup.assert_called_once_with("WARNING")


class EntryPointTests(unittest.TestCase):
    def test_python_dash_m_and_the_wrapper_script_run_the_demo(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        for command in ([sys.executable, "-m", "realizer"], [sys.executable, str(ROOT / "scripts" / "run_realizer.py")]):
            with self.subTest(command=command[1:]), tempfile.TemporaryDirectory() as tmp:
                done = subprocess.run([*command, "--package", str(PACKAGE), "--demo", "--output-dir", tmp], capture_output=True, text=True, env=env, cwd=tmp)
                self.assertEqual(done.returncode, 0, done.stderr)
                path = Path(done.stdout.strip())
                self.assertTrue(path.is_file(), done.stdout)
                self.assertEqual(path.parent, Path(tmp) / "realization" / "packages")
                with tempfile.TemporaryDirectory() as ref:
                    package = fixture_package()
                    snapshot = realize(package, demo_observation(package), Path(ref))
                    self.assertEqual(path.name, f"{snapshot.realization_package_id}.json")

    def test_bad_arguments_exit_with_status_2(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-m", "realizer"], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 2)
        self.assertIn("--package", done.stderr)

    def test_console_script_is_declared(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('realizer = "realizer.cli.main:main"', text)
        self.assertIn('publisher = "publisher.cli.main:main"', text)
        module, _, function = "realizer.cli.main:main".partition(":")
        self.assertIs(getattr(__import__(module, fromlist=[function]), function), main)


if __name__ == "__main__":
    unittest.main()
