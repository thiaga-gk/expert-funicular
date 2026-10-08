"""The origin of a measured change and the approvals file (src/realizer/analyzer/projector.py origin,
src/realizer/contracts/approvals.py, realizer.config.loader.load_approvals).

A change applied after the package was generated, to the knob its step recommends, is INFERRED; an approval naming
the package, the knob and the observed value makes it CONFIRMED (intent ACCEPTED, the reference recorded). Both
qualify for the strict KPI, so a live measurement can be claimable without an approval. A change applied before the
package cannot be its application.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT
from realizer.analyzer.projector import project_observation
from realizer.cli.main import EXIT_REFUSED, main
from realizer.common.errors import InputError
from realizer.config.loader import load_approvals
from realizer.contracts import approvals as approvals_module
from realizer.realizer import realize

PRO = world.package("autostop-scoped-pro")
CHANGED = world.world_spec("C13", {"auto_stop": 10})


def approvals(*items):
    base = {"recommendation_package_id": PRO["recommendation_package_id"], "knob": "auto_stop_minutes", "value": 10, "reference": "CHG-1042"}
    return {"approvals_format_version": "1.0.0", "approvals": [{**base, **i} for i in items]}


class OriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = world.measure(PRO, CHANGED, 13)

    def test_inferred_after_the_package_and_confirmed_by_an_approval(self):
        inferred = project_observation(PRO, self.evidence)
        self.assertEqual((inferred["origin_attribution"], inferred["intent_state"], inferred["intent_reference"]), ("INFERRED", "UNKNOWN", None))
        confirmed = project_observation(PRO, self.evidence, approvals({}))
        self.assertEqual((confirmed["origin_attribution"], confirmed["intent_state"], confirmed["intent_reference"]), ("CONFIRMED", "ACCEPTED", "CHG-1042"))

    def test_an_approval_for_another_value_package_or_knob_does_not_confirm(self):
        for other in ({"value": 15}, {"recommendation_package_id": "PKG-" + "0" * 24}, {"knob": "warehouse_size"}):
            with self.subTest(approval=other):
                self.assertEqual(project_observation(PRO, self.evidence, approvals(other))["origin_attribution"], "INFERRED")

    def test_a_change_before_the_package_is_not_inferred(self):
        before = world.world_spec("C13", {"auto_stop": 10}, change_day="2026-09-20")
        obs = project_observation(PRO, world.measure(PRO, before, 13))
        self.assertEqual(obs["origin_attribution"], "UNKNOWN")
        self.assertLess(obs["applied_at_utc"], PRO["provenance"]["generated_at"].replace("+00:00", "Z"))

    def test_an_inferred_live_measurement_is_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = realize(PRO, project_observation(PRO, self.evidence), Path(tmp)).to_dict()
        self.assertEqual(snapshot["attribution"]["origin_attribution"], "INFERRED")
        self.assertEqual(snapshot["attribution"]["attribution_claim_status"], "CLAIMABLE")
        self.assertEqual(snapshot["attribution"]["hard_disqualifiers"], [])
        self.assertEqual(snapshot["current_state"]["realization_state"], "REALIZED")


class ApprovalsFileTests(unittest.TestCase):
    def test_the_committed_schema_is_the_builders(self):
        from tests.unit.realizer.contracts.test_schema import serialize
        self.assertEqual(serialize(approvals_module.approvals_schema()), (ROOT / "schema" / "realization-approvals.schema.json").read_bytes())
        jsonschema.Draft202012Validator.check_schema(approvals_module.approvals_schema())

    def test_strict_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            for bad, message in ((dict(approvals({}), approvals_format_version="0.1"), "unsupported approvals format"),
                                 (approvals({"approved_by": "someone@example.com"}), "fails format"),
                                 (approvals({"reference": "two words"}), "fails format")):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    with self.assertRaisesRegex(InputError, message):
                        load_approvals(path)
            path.write_text(json.dumps(approvals({})), encoding="utf-8")
            self.assertEqual(load_approvals(path)["approvals"][0]["reference"], "CHG-1042")
        self.assertIsNone(load_approvals(None))

    def test_the_cli_applies_approvals_only_to_a_measured_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text(json.dumps(approvals({})), encoding="utf-8")
            evidence = Path(tmp) / "e.json"
            evidence.write_text(json.dumps(world.measure(PRO, CHANGED, 13)), encoding="utf-8")
            package = str(world.PACKAGES / "autostop-scoped-pro.json")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--package", package, "--demo", "--approvals", str(path), "--log-level", "ERROR"]), EXIT_REFUSED)
            self.assertIn("--approvals applies to a measured change", err.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
                code = main(["--package", package, "--from-evidence", str(evidence), "--approvals", str(path), "--output-dir", str(Path(tmp) / "o"),
                             "--print-json", "--log-level", "ERROR"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["attribution"]["origin_attribution"], "CONFIRMED")


class UnmatchedApprovalsTests(unittest.TestCase):
    """An approval that names no package, knob and value of the run is reported, never silently ignored."""

    @classmethod
    def setUpClass(cls):
        cls.evidence = world.measure(PRO, CHANGED, 13)

    def load(self, tmp, *items):
        path = Path(tmp) / "approvals.json"
        path.write_text(json.dumps(approvals(*items)), encoding="utf-8")
        return load_approvals(path)

    def test_each_unmatched_approval_is_one_warning_without_values_or_references(self):
        other_package = "PKG-" + "0" * 24
        with tempfile.TemporaryDirectory() as tmp:
            loaded = self.load(tmp, {}, {"value": 15, "reference": "CHG-SECRET-1"}, {"recommendation_package_id": other_package, "reference": "CHG-SECRET-2"})
            project_observation(PRO, self.evidence, loaded)
            with self.assertLogs("realizer", level="WARNING") as logs:
                approvals_module.warn_unmatched(loaded)
        lines = sorted(record.getMessage() for record in logs.records)
        self.assertEqual(lines, [
            f"approvals.unmatched index=1 package={PRO['recommendation_package_id']} knob=auto_stop_minutes reason=NO_MATCHING_CHANGE",
            f"approvals.unmatched index=2 package={other_package} knob=auto_stop_minutes reason=PACKAGE_NOT_IN_RUN"])
        self.assertNotIn("CHG-SECRET", "\n".join(lines))

    def test_matched_approvals_log_nothing_and_a_duplicate_counts_as_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = self.load(tmp, {}, {"reference": "CHG-1043"})
            project_observation(PRO, self.evidence, loaded)
            with self.assertNoLogs("realizer", level="WARNING"):
                approvals_module.warn_unmatched(loaded)

    def test_an_approval_nobody_consulted_is_reported_as_not_in_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = self.load(tmp, {})
            with self.assertLogs("realizer", level="WARNING") as logs:
                approvals_module.warn_unmatched(loaded)
        self.assertIn("reason=PACKAGE_NOT_IN_RUN", logs.records[0].getMessage())

    def test_plain_mappings_and_none_are_accepted(self):
        approvals_module.warn_unmatched(None)
        approvals_module.warn_unmatched(approvals({}))

    def test_the_cli_warns_after_the_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text(json.dumps(approvals({}, {"value": 15})), encoding="utf-8")
            evidence = Path(tmp) / "e.json"
            evidence.write_text(json.dumps(self.evidence), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = main(["--package", str(world.PACKAGES / "autostop-scoped-pro.json"), "--from-evidence", str(evidence), "--approvals", str(path),
                             "--output-dir", str(Path(tmp) / "o"), "--print-json"])
            self.assertEqual(code, 0)
            self.assertEqual(err.getvalue().count("approvals.unmatched"), 1)
            self.assertIn("index=1", err.getvalue())
            self.assertIn("reason=NO_MATCHING_CHANGE", err.getvalue())


if __name__ == "__main__":
    unittest.main()
