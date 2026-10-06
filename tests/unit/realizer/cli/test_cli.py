"""Realizer CLI adapter (src/realizer/cli): option parity with the single-file CLI, dependency direction, behavior.

The CLI parses its own arguments, loads the package, the observation and the policy (in that order), configures logging,
calls the facade and prints the written package path (or the snapshot with ``--print-json``). It never imports a
capability package. For the demo, replay, observation-file and policy modes its exit status, stdout and output tree are the
single-file CLI's.
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

from tests.unit.realizer.parity import ROOT, canonical, engine, fixture_package, oengine, tree  # noqa: E402
from realizer.cli import realize as cli_realize  # noqa: E402
from realizer.cli.main import main  # noqa: E402

CAPABILITIES = ("analyzer", "governor", "reconciler", "estimator", "reviewer", "packager", "reporter")
CLI_DIR = ROOT / "src" / "realizer" / "cli"
PACKAGE = next(canonical.PACKAGES.glob("PKG-*.json"))


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
    def test_parse_args_defaults_and_values_equal_the_single_file_cli(self):
        for argv in (["--package", "p.json"],
                     ["--package", "p", "--observation", "o", "--output-dir", "x", "--policy", "pol", "--demo", "--demo-modified",
                      "--replay-through", "2026-10-01T00:00:00Z", "--print-json", "--log-level", "DEBUG"]):
            self.assertEqual(vars(cli_realize.parse_args(argv)), vars(oengine.parse_args(argv)))
        self.assertEqual(cli_realize.parse_args(["--package", "p"]).output_dir, "./warehouse_optimizer_out")

    def test_help_text_and_errors_are_the_single_file_ones(self):
        def outcome(module, argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                module.parse_args(argv)
            return raised.exception.code, out.getvalue(), err.getvalue()
        for argv in (["--help"], [], ["--package", "p", "--log-level", "LOUD"], ["--package", "p", "--no-such-flag"]):
            self.assertEqual(outcome(cli_realize, argv), outcome(oengine, argv), argv)

    def test_the_facade_still_resolves_parse_args_and_main(self):
        self.assertIs(engine.parse_args, cli_realize.parse_args)
        self.assertIs(engine.main, main)


class BehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.root = tempfile.TemporaryDirectory()
        cls.observation = Path(cls.root.name) / "observation.json"
        cls.observation.write_text(json.dumps(oengine.demo_observation(cls.pkg, modified=True)), encoding="utf-8")
        cls.policy = Path(cls.root.name) / "policy.json"
        cls.policy.write_text(json.dumps({"minimum_validation_days": 3}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.root.cleanup()

    def run_cli(self, entry, argv, out):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = entry(argv + ["--output-dir", str(out)])
            except Exception as exc:  # noqa: BLE001 - compared
                code = f"{type(exc).__name__}: {exc}".replace(str(out), "<out>")
        return code, stdout.getvalue().replace(str(out), "<out>")

    def assert_same(self, argv, runs=1):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            results = []
            for _ in range(runs):
                results.append((self.run_cli(oengine.main, argv, Path(a)), self.run_cli(main, argv, Path(b))))
            for old, new in results:
                self.assertEqual(new, old, argv)
            self.assertEqual(tree(Path(b)), tree(Path(a)), argv)
            return results[-1][1], tree(Path(b))

    def test_demo_mode(self):
        (code, stdout), files_ = self.assert_same(["--package", str(PACKAGE), "--demo"])
        self.assertEqual(code, 0)
        self.assertRegex(stdout, r"^<out>/realization/packages/RPKG-[0-9A-F]{24}\.json\n$")
        self.assertTrue(any(k.startswith("realization/snapshots/RSNAP-") for k in files_))

    def test_demo_modified_and_print_json(self):
        (code, stdout), _ = self.assert_same(["--package", str(PACKAGE), "--demo-modified", "--print-json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["contract_version"], oengine.rc.CONTRACT_VERSION)

    def test_replay_modes(self):
        base = ["--package", str(PACKAGE), "--demo"]
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for argv in (base, base, [*base, "--replay-through", "2000-01-01T00:00:00Z"], [*base, "--replay-through", "2100-01-01T00:00:00Z"],
                         ["--package", str(PACKAGE), "--replay-through", "2100-01-01T00:00:00Z"]):
                with self.subTest(argv=argv[2:]):
                    self.assertEqual(self.run_cli(main, argv, Path(b)), self.run_cli(oengine.main, argv, Path(a)))
                    self.assertEqual(tree(Path(b)), tree(Path(a)))

    def test_observation_file_and_policy_override(self):
        (code, _), files_ = self.assert_same(["--package", str(PACKAGE), "--observation", str(self.observation), "--policy", str(self.policy)])
        self.assertEqual(code, 0)
        self.assertTrue(any(k.startswith("realization/ledger/") for k in files_))

    def test_lineage_only_run_without_an_observation(self):
        (code, _), files_ = self.assert_same(["--package", str(PACKAGE)])
        self.assertEqual(code, 0)
        self.assertFalse(any(k.startswith("realization/evidence/") and v for k, v in files_.items()))

    def test_failures_are_not_mapped_to_an_exit_status(self):
        bad = Path(self.root.name) / "bad.json"
        bad.write_text(json.dumps({"recommendation_package_id": "PKG-1"}), encoding="utf-8")
        for argv in (["--package", str(bad)], ["--package", str(Path(self.root.name) / "missing.json")],
                     ["--package", str(PACKAGE), "--observation", str(bad), "--demo-modified", "--policy", str(Path(self.root.name) / "nope.json")]):
            with self.subTest(argv=argv[1:]):
                new = self.run_cli(main, argv, Path(self.root.name) / "o1")
                old = self.run_cli(oengine.main, argv, Path(self.root.name) / "o2")
                self.assertEqual(new[0].replace("o1", "o2"), old[0].replace("o1", "o2"))
                self.assertIsInstance(new[0], str)

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
                    snapshot = oengine.realize(package, oengine.demo_observation(package), Path(ref))
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
