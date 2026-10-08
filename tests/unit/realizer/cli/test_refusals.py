"""Realizer refusals, exit status and ``--version`` (src/realizer/common/errors.py, src/realizer/cli/main.py).

A run the Realizer refuses (``RealizerError``: an input it does not accept, an unreadable file, a corrupt ledger) prints
one ``realizer: <message>`` line on stderr, writes nothing for an input refusal and returns exit status 2. A defect (any
other exception) is logged by type only and re-raised, so it keeps its traceback and is not mapped to a status. The
messages name files, fields and ids, never input values.
"""
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.helpers import ROOT, canonical, fixture_package, tree  # noqa: E402
from publisher.cli import main as publisher_main  # noqa: E402
from publisher.config.versions import PUBLISHER_VERSION  # noqa: E402
from realizer import realizer as facade  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.cli import realize as cli_realize  # noqa: E402
from realizer.cli.main import EXIT_REFUSED, main  # noqa: E402
from realizer.common import logging as rlogging  # noqa: E402
from realizer.common.errors import InputError, LedgerError, RealizerError  # noqa: E402
from realizer.config import settings  # noqa: E402
from realizer.config.loader import read_json_input, validate_package, validate_replay_through  # noqa: E402
from realizer.packager import ledger  # noqa: E402

PACKAGE = canonical.PACKAGES / "inplace-size-canary.json"
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (DEBUG|INFO|WARNING|ERROR) [a-z_]+\.")


class LoggerRestoringCase(unittest.TestCase):
    """``main`` configures the ``realizer`` logger (stderr, no propagation); a test must not leave that behind."""

    def setUp(self):
        saved = (list(rlogging.LOGGER.handlers), rlogging.LOGGER.level, rlogging.LOGGER.propagate)
        self.addCleanup(self.restore, saved)

    @staticmethod
    def restore(saved):
        for handler in rlogging.LOGGER.handlers:
            handler.close()
        rlogging.LOGGER.handlers, rlogging.LOGGER.level, rlogging.LOGGER.propagate = saved

    def run_main(self, argv):
        """``(exit status, stdout, stderr)`` of ``main(argv)``; a raised exception propagates."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class ErrorClassTests(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(InputError, RealizerError) and issubclass(InputError, ValueError))
        self.assertTrue(issubclass(LedgerError, RealizerError))
        self.assertFalse(issubclass(LedgerError, ValueError))
        self.assertTrue(issubclass(RealizerError, Exception))

    def test_exit_status_constant(self):
        self.assertEqual(EXIT_REFUSED, 2)


class RefusalTests(LoggerRestoringCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.root = tempfile.TemporaryDirectory()
        cls.base = Path(cls.root.name)
        (cls.base / "dir").mkdir()
        (cls.base / "invalid.json").write_text("{not json", encoding="utf-8")
        (cls.base / "binary.json").write_bytes(b"\xff\xfe\x00\x01")
        (cls.base / "v2.json").write_text(json.dumps({**cls.pkg, "contract_version": "2.0.0"}), encoding="utf-8")
        cls.observation = {**demo_observation(cls.pkg, modified=True), "observation_format_version": "1.0.0"}
        (cls.base / "bad-observation.json").write_text(json.dumps(cls.observation), encoding="utf-8")
        cls.mismatched = {**demo_observation(cls.pkg, modified=True), "warehouse_id": "some-other-warehouse"}
        (cls.base / "wrong-warehouse.json").write_text(json.dumps(cls.mismatched), encoding="utf-8")
        (cls.base / "policy-invalid.json").write_text("[1, 2", encoding="utf-8")
        (cls.base / "policy-unknown.json").write_text(json.dumps({"minimum_validation_days": 3}), encoding="utf-8")
        (cls.base / "policy-range.json").write_text(json.dumps({"minimum_price_coverage_pct": 101}), encoding="utf-8")
        (cls.base / "policy-list.json").write_text(json.dumps([1]), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.root.cleanup()

    def refuse(self, argv, message_pattern, *, out=None):
        """Run the CLI on *argv*; assert exit 2, nothing on stdout, a single ``realizer: ...`` message (the last stderr line,
        after the run's log lines) matching *message_pattern* and, unless *out* is given, that nothing was written."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out" if out is None else out
            code, stdout, stderr = self.run_main([*argv, "--output-dir", str(target)])
            self.assertEqual(code, 2, stderr)
            self.assertEqual(stdout, "")
            lines = stderr.splitlines()
            self.assertEqual([line for line in lines if line.startswith("realizer: ")], lines[-1:], stderr)
            self.assertTrue(all(LOG_LINE.match(line) for line in lines[:-1]), "everything before the message is a log line")
            self.assertRegex(lines[-1], rf"^realizer: {message_pattern}$")
            if out is None:
                self.assertEqual(tree(Path(tmp)), {}, "an input refusal writes nothing")
        return stderr

    def test_missing_package_file(self):
        stderr = self.refuse(["--package", str(self.base / "missing.json")], r"package file not found: .*missing\.json")
        self.assertNotIn("Traceback", stderr)

    def test_package_path_is_a_directory(self):
        self.refuse(["--package", str(self.base / "dir")], r"package path is a directory, not a JSON file: .*dir")

    def test_package_is_invalid_json(self):
        self.refuse(["--package", str(self.base / "invalid.json")], r"package file is not valid JSON: .*invalid\.json \(line 1\)")

    def test_package_is_not_utf8_text(self):
        self.refuse(["--package", str(self.base / "binary.json")], r"package file is not UTF-8 text: .*binary\.json")

    def test_a_2x_package_is_refused_with_the_supported_contract(self):
        self.refuse(["--package", str(self.base / "v2.json")], r"unsupported Publisher contract: 2\.0\.0 \(this Realizer reads 3\.2\.0\)")

    def test_package_that_is_not_an_object(self):
        path = self.base / "list.json"
        path.write_text("[]", encoding="utf-8")
        self.refuse(["--package", str(path)], r"a RecommendationPackage must be a JSON object")

    def test_malformed_observation_is_refused_before_anything_is_written(self):
        self.refuse(["--package", str(PACKAGE), "--observation", str(self.base / "bad-observation.json")],
                    r"unsupported observation format: 1\.0\.0 \(this Realizer reads 3\.2\.0\)")

    def test_observation_of_another_warehouse_is_refused(self):
        self.refuse(["--package", str(PACKAGE), "--observation", str(self.base / "wrong-warehouse.json")],
                    r"observation is for .*/some-other-warehouse, the package for .*/.*")

    def test_observation_file_problems(self):
        for name, pattern in (("missing.json", r"observation file not found: .*missing\.json"),
                              ("invalid.json", r"observation file is not valid JSON: .*invalid\.json \(line 1\)"),
                              ("dir", r"observation path is a directory, not a JSON file: .*dir")):
            with self.subTest(file=name):
                self.refuse(["--package", str(PACKAGE), "--observation", str(self.base / name)], pattern)

    def test_replay_cut_that_is_not_a_time(self):
        for value in ("yesterday", "2026-13-45T00:00:00Z", "", "12"):
            with self.subTest(value=value):
                self.refuse(["--package", str(PACKAGE), "--demo", "--replay-through", value],
                            r"replay cut \(--replay-through\) is not an ISO-8601 time")

    def test_replay_cut_before_the_package_was_generated(self):
        self.refuse(["--package", str(PACKAGE), "--demo", "--replay-through", "2000-01-01T00:00:00Z"],
                    r"replay cut 2000-01-01T00:00:00Z is before the package was generated \(2026-10-01T12:00:00Z\): nothing to project")

    def test_replay_cut_at_the_generation_time_or_later_is_accepted(self):
        for cut in ("2026-10-01T12:00:00Z", "2100-01-01T00:00:00Z"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as out:
                code, stdout, stderr = self.run_main(["--package", str(PACKAGE), "--demo", "--replay-through", cut, "--output-dir", out])
                self.assertEqual(code, 0, stderr)
                self.assertTrue(all(LOG_LINE.match(line) for line in stderr.splitlines()), "a successful run writes only log lines to stderr")
                self.assertTrue(Path(stdout.strip()).is_file())

    def test_bad_policy_files(self):
        for name, pattern in (("missing.json", r"policy file not found: .*missing\.json"),
                              ("policy-invalid.json", r"policy file is not valid JSON: .*policy-invalid\.json \(line 1\)"),
                              ("policy-list.json", r"a policy file must be a JSON object"),
                              ("policy-unknown.json", r"unknown policy setting\(s\): minimum_validation_days"),
                              ("policy-range.json", r"policy setting minimum_price_coverage_pct is out of range")):
            with self.subTest(file=name):
                self.refuse(["--package", str(PACKAGE), "--demo", "--policy", str(self.base / name)], pattern)

    def test_a_refusal_is_logged_by_type_and_the_message_never_carries_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, stderr = self.run_main(["--package", str(self.base / "v2.json"), "--output-dir", tmp, "--log-level", "INFO"])
        self.assertEqual(code, 2)
        lines = stderr.splitlines()
        self.assertTrue(any(re.search(r"Z ERROR realizer\.refused exit=2 error_type=InputError$", line) for line in lines), stderr)
        self.assertTrue(any(line.startswith("realizer: unsupported Publisher contract") for line in lines))
        self.assertNotIn(self.pkg["warehouse"]["warehouse_id"], "".join(line for line in lines if "refused" in line))

    def test_corrupt_ledger_line_is_refused_naming_the_file_and_line_and_the_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, stdout, stderr = self.run_main(["--package", str(PACKAGE), "--demo", "--output-dir", str(out), "--flat-output"])
            self.assertEqual(code, 0, stderr)
            path = next((out / "realization" / "ledger").glob("*.jsonl"))
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join([lines[0], "{broken", *lines[1:]]) + "\n", encoding="utf-8")
            before = tree(out)
            code, stdout, stderr = self.run_main(["--package", str(PACKAGE), "--demo", "--output-dir", str(out), "--flat-output"])
            self.assertEqual((code, stdout), (2, ""))
            self.assertEqual(stderr.splitlines()[-1], f"realizer: ledger {path.name} line 2 is not a valid ledger entry (JSONDecodeError)")
            self.assertEqual(tree(out), before, "a corrupt ledger is refused before anything is appended or written")

    def test_defect_is_not_mapped_to_an_exit_status(self):
        for stage, exc in (("governor", RuntimeError("boom with value 12345")), ("reporter", KeyError("k"))):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                module = getattr(facade, stage)
                with mock.patch.object(module, "run", side_effect=exc), self.assertRaises(type(exc)):
                    self.run_main(["--package", str(PACKAGE), "--demo", "--output-dir", tmp, "--log-level", "INFO"])

    def test_defect_is_logged_by_type_only_and_prints_no_realizer_line(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(facade.governor, "run", side_effect=RuntimeError("secret-value-777")), \
                contextlib.redirect_stderr(err), self.assertRaises(RuntimeError):
            main(["--package", str(PACKAGE), "--demo", "--output-dir", tmp])
        text = err.getvalue()
        self.assertRegex(text, r"Z ERROR realizer\.crashed error_type=RuntimeError\n")
        self.assertNotIn("secret-value-777", text)
        self.assertNotIn("realizer: ", text.replace("realizer.", ""))

    def test_a_refusal_raised_below_the_cli_is_still_mapped(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli_realize, "realize_package", side_effect=LedgerError("ledger X.jsonl line 3 is not a valid ledger entry (KeyError)")):
            code, _, stderr = self.run_main(["--package", str(PACKAGE), "--demo", "--output-dir", tmp])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.splitlines()[-1], "realizer: ledger X.jsonl line 3 is not a valid ledger entry (KeyError)")

    def test_subprocess_exit_status_and_stderr(self):
        env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, "-m", "realizer", "--package", str(self.base / "missing.json"), "--output-dir", tmp + "/o"],
                                  capture_output=True, text=True, env=env, cwd=tmp)
            self.assertEqual(done.returncode, 2)
            self.assertEqual(done.stdout, "")
            self.assertEqual(done.stderr.splitlines()[-1], f"realizer: package file not found: {self.base / 'missing.json'}")
            self.assertNotIn("Traceback", done.stderr)
            self.assertFalse((Path(tmp) / "o").exists())


class InputGateUnitTests(unittest.TestCase):
    def test_read_json_input_names_the_label_and_never_the_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text('{"cost": 12345, ', encoding="utf-8")
            with self.assertRaises(InputError) as caught:
                read_json_input(path, "thing")
            self.assertEqual(str(caught.exception), f"thing file is not valid JSON: {path} (line 1)")
            self.assertNotIn("12345", str(caught.exception))
            path.write_text('{"ok": 1}', encoding="utf-8")
            self.assertEqual(read_json_input(str(path), "thing"), {"ok": 1})

    def test_validate_replay_through(self):
        package = fixture_package()
        self.assertIsNone(validate_replay_through(None, package))
        self.assertEqual(validate_replay_through("2026-10-02T00:00:00+00:00", package), "2026-10-02T00:00:00Z")
        self.assertEqual(validate_replay_through("2026-10-01T12:00:00Z", package), "2026-10-01T12:00:00Z")
        for bad in ("not a time", "", "2026-02-30T00:00:00Z"):
            with self.subTest(value=bad), self.assertRaises(InputError):
                validate_replay_through(bad, package)
        with self.assertRaisesRegex(InputError, "before the package was generated"):
            validate_replay_through("2026-10-01T11:59:59Z", package)
        # a package without a generation time has no start to compare with
        self.assertEqual(validate_replay_through("2000-01-01T00:00:00Z", {"provenance": {}}), "2000-01-01T00:00:00Z")

    def test_facade_refuses_a_replay_cut_before_anything_is_written(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InputError):
                facade.realize(package, demo_observation(package), Path(tmp), replay_through="2000-01-01T00:00:00Z")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_the_loader_gates_raise_value_errors_for_callers_that_catch_them(self):
        for call in (lambda: validate_package({"contract_version": "1.0.0"}), lambda: validate_package([])):
            with self.assertRaises(ValueError):
                call()


class LedgerErrorTests(unittest.TestCase):
    def good_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            facade.realize(fixture_package(), demo_observation(fixture_package()), Path(tmp))
            path = next((Path(tmp) / "realization" / "ledger").glob("*.jsonl"))
            return path.read_text(encoding="utf-8").splitlines()

    def test_each_unreadable_line_names_the_file_and_line(self):
        good = self.good_lines()
        first = json.loads(good[0])
        cases = {
            "not json": ("not json", "JSONDecodeError"),
            "not an object": ("[1]", "TypeError"),
            "unknown event type": (json.dumps({**first, "event_type": "NOPE"}), "ValueError"),
            "missing event type": (json.dumps({k: v for k, v in first.items() if k != "event_type"}), "KeyError"),
            "missing field": (json.dumps({k: v for k, v in first.items() if k != "payload"}), "TypeError"),
            "unexpected field": (json.dumps({**first, "extra": 1}), "TypeError"),
            "failed entry validation": (json.dumps({**first, "ledger_entry_id": "BAD"}), "ValueError"),
        }
        for label, (line, kind) in cases.items():
            for position in (1, 2):
                with self.subTest(case=label, line=position), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "RPKG-X.jsonl"
                    body = [line] if position == 1 else [good[0], line]
                    path.write_text("\n".join(body) + "\n", encoding="utf-8")
                    with self.assertRaises(LedgerError) as caught:
                        ledger.load_ledger(path)
                    self.assertEqual(str(caught.exception), f"ledger RPKG-X.jsonl line {position} is not a valid ledger entry ({kind})")
                    self.assertIsNotNone(caught.exception.__cause__, "chained from the original error")

    def test_blank_lines_are_skipped_and_do_not_shift_the_reported_line_number(self):
        good = self.good_lines()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "L.jsonl"
            path.write_text(f"\n  \n{good[0]}\n\nnot json\n", encoding="utf-8")
            with self.assertRaisesRegex(LedgerError, r"line 5 "):
                ledger.load_ledger(path)

    def test_a_good_ledger_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "L.jsonl"
            path.write_text("\n".join(self.good_lines()) + "\n", encoding="utf-8")
            self.assertGreater(len(ledger.load_ledger(path)), 0)

    def test_the_message_does_not_carry_line_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "L.jsonl"
            path.write_text('{"secret": "hunter2"\n', encoding="utf-8")
            with self.assertRaises(LedgerError) as caught:
                ledger.load_ledger(path)
            self.assertNotIn("hunter2", str(caught.exception))


class VersionTests(LoggerRestoringCase):
    def test_realizer_version_exits_0_and_names_the_versions(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(err.getvalue(), "")
        text = out.getvalue()
        self.assertEqual(" ".join(text.split()), cli_realize.version_text())  # argparse wraps the line
        self.assertIn(f"realizer {settings.REALIZER_VERSION}", text)
        self.assertIn(f"release {settings.PRODUCT_VERSION}", text)
        self.assertIn(PUBLISHER_VERSION, text)
        self.assertIn("Reporter JSON Contract 3.2.0", text)

    def test_version_needs_no_other_argument(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli_realize.parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)

    def test_publisher_version_exits_0_and_names_the_versions(self):
        from publisher.contracts import reporter_contract as rc
        from publisher.contracts import workbook_contract as wc
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            publisher_main.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith(f"publisher {PUBLISHER_VERSION} "), text)
        self.assertIn(rc.CONTRACT_VERSION, text)
        self.assertIn(wc.WORKBOOK_CONTRACT_VERSION, text)

    def test_both_console_scripts_report_their_version(self):
        env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}
        for module, prefix in (("realizer", f"realizer {settings.REALIZER_VERSION} "), ("publisher", f"publisher {PUBLISHER_VERSION} ")):
            with self.subTest(module=module):
                done = subprocess.run([sys.executable, "-m", module, "--version"], capture_output=True, text=True, env=env)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue(done.stdout.startswith(prefix), done.stdout)

    def test_product_version_is_the_publishers_release(self):
        self.assertEqual(settings.PRODUCT_VERSION, PUBLISHER_VERSION)


if __name__ == "__main__":
    unittest.main()
