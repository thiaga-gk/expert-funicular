"""The history and the projections in separate directories (``RealizationRequest.state_dir``).

The ledgers, evidence, regeneration requests, source package copies, policies and the state index are one history and
live in the state directory; a run writes only its realization packages and snapshots under its output directory. Runs
into fresh output directories that share one state therefore continue one history instead of forking it, and decide
exactly what runs into one directory decide (``state_dir=None`` is that single-folder layout).
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.helpers import fixture_package  # noqa: F401  (src/realizer on the path)
from tests.unit.realizer.lineage_support import AGGRESSIVE, BALANCED, stored_run
from realizer.analyzer.collector.collector import demo_observation
from realizer.contracts.request import RealizationRequest
from realizer.realizer import realize, realize_request

STATE_FOLDERS = {"ledger", "evidence", "regeneration", "sources", "policies", "state"}
PROJECTION_FOLDERS = {"packages", "snapshots"}


def files(root: Path):
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def folders(root: Path):
    return {p.relative_to(root / "realization").parts[0] for p in (root / "realization").rglob("*") if p.is_file()} if (root / "realization").exists() else set()


class StateDirTests(unittest.TestCase):
    def test_the_history_goes_to_the_state_dir_and_the_projections_to_the_output_dir(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            run, state, flat = Path(tmp, "run"), Path(tmp, "state"), Path(tmp, "flat")
            report = realize_request(RealizationRequest(package=package, observation=demo_observation(package), output_dir=run, state_dir=state))
            realize(package, demo_observation(package), flat)
            self.assertEqual(folders(run), PROJECTION_FOLDERS)
            self.assertTrue(folders(state) <= STATE_FOLDERS)
            self.assertTrue({"ledger", "evidence", "sources", "state"} <= folders(state))
            # The same files, split between the two roots.
            self.assertEqual({**files(run), **files(state)}, files(flat))
            self.assertTrue(all(str(p).startswith(str(run)) or str(p).startswith(str(state)) for p in report.files))
            self.assertTrue(any(str(p).startswith(str(state)) for p in report.files), "the state index is in the state dir")

    def test_runs_into_fresh_output_dirs_continue_one_history(self):
        aggressive, balanced = stored_run(AGGRESSIVE), stored_run(BALANCED)
        with tempfile.TemporaryDirectory() as tmp:
            state, flat = Path(tmp, "state"), Path(tmp, "flat")
            for day, (package, observation) in enumerate((aggressive, balanced)):
                realize_request(RealizationRequest(package=copy.deepcopy(package), observation=copy.deepcopy(observation),
                                                   output_dir=Path(tmp, f"run{day}"), state_dir=state))
                realize(copy.deepcopy(package), copy.deepcopy(observation), flat)
            index = json.loads(next((state / "realization" / "state").glob("*.json")).read_text(encoding="utf-8"))
            flat_index = json.loads(next((flat / "realization" / "state").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(index, flat_index)
            # One change, one claim: the balanced lineage owns it; the aggressive one, re-projected in the second run's
            # folder, is observable only.
            claims = {r["recommendation_package_id"]: r["recommendation_attributed_realized_savings"] for r in index["lineages"]}
            self.assertEqual(claims, {balanced[0]["recommendation_package_id"]: "1069.73", aggressive[0]["recommendation_package_id"]: "0.00"})
            second = files(Path(tmp, "run1"))
            self.assertEqual(len([k for k in second if k.startswith("realization/packages/")]), 2)
            for name, text in second.items():
                if name.startswith("realization/packages/"):
                    self.assertEqual(text, (flat / name).read_text(encoding="utf-8"))

    def test_state_dir_none_is_the_single_folder_layout(self):
        request = RealizationRequest(package={}, observation=None, output_dir=Path("x"))
        self.assertEqual(request.state_root, Path("x"))
        self.assertEqual(RealizationRequest(package={}, observation=None, output_dir=Path("x"), state_dir=Path("s")).state_root, Path("s"))


if __name__ == "__main__":
    unittest.main()
