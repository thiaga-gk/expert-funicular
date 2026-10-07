"""``realizer --demo``: the package's demo-fleet warehouse measured in its simulated world through the live path.

The demo writes its measurement evidence where a live run does (``realization/measurements/``), so ``--from-evidence``
replays it to the same realization package; ``--as-of`` sets the measurement time; a package outside the demo fleet is
refused with status 2 before anything is written.
"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.analyzer.test_demo_world import rehashed
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer.cli.main import EXIT_REFUSED, main  # noqa: E402

PRO = world.PACKAGES / "autostop-scoped-pro.json"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([*argv, "--log-level", "ERROR"])
    return code, out.getvalue(), err.getvalue()


class DemoCliTests(unittest.TestCase):
    def test_the_demo_writes_its_evidence_and_the_replay_gives_the_same_realization(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo, replay = Path(tmp) / "demo", Path(tmp) / "replay"
            code, stdout, err = run_cli("--package", str(PRO), "--demo", "--output-dir", str(demo))
            self.assertEqual(code, 0, err)
            evidence = sorted((demo / "realization" / "measurements").glob("*.json"))
            self.assertEqual([p.name for p in evidence], ["2234567890123456__wh-demo-013__20261107T120000Z.json"])
            self.assertEqual(json.loads(evidence[0].read_text(encoding="utf-8"))["source"], "DEMO_SIMULATION")
            code, replayed, err = run_cli("--package", str(PRO), "--from-evidence", str(evidence[0]), "--output-dir", str(replay))
            self.assertEqual(code, 0, err)
            rpkg = Path(stdout.strip())
            self.assertEqual(rpkg.name, Path(replayed.strip()).name)
            self.assertEqual(rpkg.read_text(encoding="utf-8"), Path(replayed.strip()).read_text(encoding="utf-8"))
            snapshot = json.loads(next((demo / "realization" / "snapshots").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(snapshot["application"]["implementation_conformance"], "EXACT")
            self.assertEqual(snapshot["current_state"]["realization_state"], "REALIZED")

    def test_as_of_sets_the_measurement_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_cli("--package", str(PRO), "--demo", "--as-of", "2026-10-20T12:00:00Z", "--output-dir", tmp)
            self.assertEqual(code, 0, err)
            self.assertEqual([p.name for p in (Path(tmp) / "realization" / "measurements").glob("*.json")],
                             ["2234567890123456__wh-demo-013__20261020T120000Z.json"])

    def test_the_modified_demo_applies_a_modified_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, err = run_cli("--package", str(world.PACKAGES / "inplace-size-canary.json"), "--demo-modified", "--print-json",
                                        "--output-dir", tmp)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(stdout)["application"]["implementation_conformance"], "MODIFIED")

    def test_a_package_outside_the_demo_fleet_is_refused_and_nothing_is_written(self):
        package = copy.deepcopy(world.package("autostop-scoped-pro"))
        package["warehouse"]["warehouse_id"] = "wh-prod-013"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.json"
            path.write_text(json.dumps(rehashed(package)), encoding="utf-8")
            code, stdout, err = run_cli("--package", str(path), "--demo", "--output-dir", str(Path(tmp) / "out"))
            self.assertEqual((code, stdout), (EXIT_REFUSED, ""))
            self.assertIn("demo-fleet", err.splitlines()[-1])
            self.assertNotIn("wh-prod-013", err)
            self.assertFalse((Path(tmp) / "out").exists())


if __name__ == "__main__":
    unittest.main()
