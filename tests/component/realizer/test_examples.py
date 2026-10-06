"""The Publisher to Realizer examples under examples/realizer/ keep the identities and outcomes their README documents.

Each example is the Publisher package fixture (Reporter JSON Contract 3.1.0) plus one normalized observation. The test realizes it into a temporary
directory and compares the written files and the snapshot with the README, so the README and the example cannot drift.
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
            "RPKG": "RPKG-976E6C1C6E88CE76321758ED",
            "RSNAP": "RSNAP-370D181E943A40CCB9809E31",
            "REVID": "REVID-37F7F275591908182339F44B",
            "RERUN": "RERUN-E859EB99CB326CE38DB20043",
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
            "RPKG": "RPKG-976E6C1C6E88CE76321758ED",
            "RSNAP": "RSNAP-91309FCF9B7A30CDA765B0F7",
            "REVID": "REVID-0054A9DB9BEB251DB6B705DA",
            "RERUN": "RERUN-3908769AAF62FB3768C5C200",
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

    def test_example_observations_are_the_demo_observations_of_the_fixture_package(self):
        package = json.loads(PACKAGE.read_text(encoding="utf-8"))
        for variant, modified in (("exact", False), ("modified", True)):
            with self.subTest(example=variant):
                shipped = json.loads((EXAMPLES / variant / "observation.json").read_text(encoding="utf-8"))
                self.assertEqual(shipped, demo_observation(package, modified=modified))

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


if __name__ == "__main__":
    unittest.main()
