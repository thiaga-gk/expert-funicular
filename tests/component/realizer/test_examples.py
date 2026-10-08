"""The Publisher to Realizer examples under examples/realizer/ keep the identities and outcomes their README documents.

Each example is the Publisher package fixture (Reporter JSON Contract 3.2.0) plus one normalized observation, a fixed
synthetic fixture (``demo_observation``; ``realizer --demo`` measures a simulated world instead). The test realizes it
into a temporary directory and compares the written files and the snapshot with the README, so the README and the
example cannot drift.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402
canonical.modular()  # src/realizer, never a shadowing copy
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.realizer import realize  # noqa: E402

EXAMPLES = ROOT / "examples" / "realizer"
PACKAGE = canonical.PACKAGES / "inplace-size-canary.json"
README = EXAMPLES / "README.md"

EXPECTED = {
    "exact": {
        "ids": {
            "RPKG": "RPKG-69DDE3286B752C8E1F1FC6BE",
            "RSNAP": "RSNAP-40A8D117D142B32D086CC059",
            "REVID": "REVID-37F7F275591908182339F44B",
            "RERUN": "RERUN-42B47BD394035774313246E5",
        },
        "outcome": {
            "implementation_conformance": "EXACT",
            "validation_state": "PASSED",
            "maturity": "REPRESENTATIVE",
            "realization_state": "REALIZED",
            "denominator_source": "PUBLISHER_CANDIDATE",
        },
    },
    "modified": {
        "ids": {
            "RPKG": "RPKG-69DDE3286B752C8E1F1FC6BE",
            "RSNAP": "RSNAP-C34B4923918BFDD8CBDE55E9",
            "REVID": "REVID-0054A9DB9BEB251DB6B705DA",
            "RERUN": "RERUN-F3F81701C87ADC1868C6887A",
        },
        "outcome": {
            "implementation_conformance": "MODIFIED",
            "validation_state": "PASSED",
            "maturity": "REPRESENTATIVE",
            "realization_state": "REALIZED",
            "denominator_source": "REESTIMATED_APPLIED_SCOPE",
        },
    },
}
FOLDERS = {"RPKG": "packages", "RSNAP": "snapshots", "REVID": "evidence", "RERUN": "regeneration"}


def realize_example(variant: str, out: Path):
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    observation = json.loads((EXAMPLES / variant / "observation.json").read_text(encoding="utf-8"))
    return package, observation, realize(package, observation, out)


class RealizerExampleTests(unittest.TestCase):

    def test_each_example_realizes_to_the_documented_identities_and_outcome(self):
        for variant, expected in EXPECTED.items():
            with self.subTest(example=variant), tempfile.TemporaryDirectory() as td:
                out = Path(td)
                _, _, snapshot = realize_example(variant, out)
                realization = out / "realization"
                for kind, identity in expected["ids"].items():
                    self.assertTrue((realization / FOLDERS[kind] / f"{identity}.json").is_file(), identity)
                    self.assertEqual(len(list((realization / FOLDERS[kind]).glob("*.json"))), 1, FOLDERS[kind])
                snap = snapshot.to_dict()
                self.assertEqual(snap["realization_package_id"], expected["ids"]["RPKG"])
                self.assertEqual(snap["snapshot_id"], expected["ids"]["RSNAP"])
                outcome = expected["outcome"]
                self.assertEqual(snap["application"]["implementation_conformance"], outcome["implementation_conformance"])
                self.assertEqual(snap["current_state"]["validation_state"], outcome["validation_state"])
                self.assertEqual(snap["maturity"]["state"], outcome["maturity"])
                self.assertEqual(snap["current_state"]["realization_state"], outcome["realization_state"])
                self.assertEqual(snap["economics"]["denominator_source"], outcome["denominator_source"])

    def test_example_observations_are_the_synthetic_fixture_observations_of_the_package(self):
        """The examples are fixed synthetic observations (``demo_observation``), never what ``--demo`` measures."""
        package = json.loads(PACKAGE.read_text(encoding="utf-8"))
        for variant, modified in (("exact", False), ("modified", True)):
            with self.subTest(example=variant):
                shipped = json.loads((EXAMPLES / variant / "observation.json").read_text(encoding="utf-8"))
                self.assertEqual(shipped, demo_observation(package, modified=modified))

    def test_the_example_approvals_file_is_valid_and_confirms_the_demo_change(self):
        """``approvals.json`` loads against the approvals schema and names the package's size step: a replay of the demo's
        measurement evidence with it has origin CONFIRMED and leaves no approval unmatched."""
        from realizer.analyzer.collector import demo_world
        from realizer.analyzer.projector import project_observation
        from realizer.config.loader import load_approvals
        from realizer.contracts.approvals import warn_unmatched
        package = json.loads(PACKAGE.read_text(encoding="utf-8"))
        approvals = load_approvals(EXAMPLES / "approvals.json")
        self.assertEqual([a["recommendation_package_id"] for a in approvals["approvals"]], [package["recommendation_package_id"]])
        observation = project_observation(package, demo_world.simulated_evidence(package), approvals)
        self.assertEqual((observation["origin_attribution"], observation["intent_state"], observation["intent_reference"]), ("CONFIRMED", "ACCEPTED", "CHG-1042"))
        with self.assertNoLogs("realizer", level="WARNING"):
            warn_unmatched(approvals)

    def test_the_readme_documents_the_expected_identities_and_outcomes(self):
        text = README.read_text(encoding="utf-8")
        for variant, expected in EXPECTED.items():
            section = re.search(rf"^### {variant}\b.*?(?=^### |\Z)", text, re.S | re.M)
            self.assertIsNotNone(section, variant)
            for identity in expected["ids"].values():
                self.assertIn(identity, section.group(0), f"{variant}: {identity}")
            for field, value in expected["outcome"].items():
                self.assertRegex(section.group(0), rf"{field}\s*=\s*{value}\b", f"{variant}: {field}")
        for command in ("--observation examples/realizer/exact/observation.json", "--observation examples/realizer/modified/observation.json", "--demo-modified"):
            self.assertIn(command, text)
        self.assertTrue(PACKAGE.relative_to(ROOT).as_posix() in text)
        self.assertIn("--approvals examples/realizer/approvals.json", text)


if __name__ == "__main__":
    unittest.main()
