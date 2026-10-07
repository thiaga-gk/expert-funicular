"""Several lineages of one warehouse realized into one output directory, from real Publisher packages: the stored
packages and observations of the end-to-end goldens, the wh-demo-039 auto-stop change measured in its simulated world,
and readers of the files the runs leave (realization packages, the state index). Shared by the lineage tests
(``tests/unit/realizer/facade/test_lineages.py``) and the golden scenarios (``tests/golden/realizer/test_golden_realization.py``).
"""
import copy
import json
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from tests.unit.realizer.helpers import canonical  # noqa: F401  (src/realizer on the path, never a shadowing copy)
from tests import realizer_world
from tests.regression.realizer import invariants
from tests.golden.realizer import e2e
from realizer.analyzer.projector import project_observation
from realizer.contracts.realization import make_rpkg_id
from realizer.realizer import realize

# Two cost-profile packages of wh-demo-039 from one epoch and one generation time (auto-stop 365 to 15 and 365 to 210).
AGGRESSIVE, BALANCED = "ge06-profile-aggressive-modified", "ge07-profile-balanced-exact"
# The owner's change in wh-demo-039's simulated world: auto-stop 210 from 2026-10-08 14:32.
CHANGE = [{"at": "2026-10-08T14:32:00Z", "set": {"auto_stop": 210}}]


def stored_run(case: str) -> Tuple[Dict, Dict]:
    """A golden case's stored package and observation."""
    base = e2e.case_dir(case) / "expected"
    return (json.loads((base / "publisher" / "package.json").read_text(encoding="utf-8")),
            json.loads((base / "realizer" / "observation.json").read_text(encoding="utf-8")))


def measured(package: Mapping) -> Dict:
    """The observation of the auto-stop change in wh-demo-039's simulated world, measured against *package*."""
    spec = e2e.world_spec(39, CHANGE)
    return project_observation(package, realizer_world.measure(package, spec, 39))


def realize_all(runs: Sequence[Tuple[Mapping, Optional[Mapping]]], out: Path) -> None:
    """Realize each (package, observation) into *out*, in order; after each run the state holds the ledger invariants
    (``tests/regression/realizer/invariants.py``), the ledgers before it being a prefix of those after it."""
    check = invariants.RunChecker(str(out), before=invariants.ledgers(invariants.read_state(out)))
    for package, observation in runs:
        realize(copy.deepcopy(package), copy.deepcopy(observation), out)
        check(package, out)


def rpkg_file(out: Path, package: Mapping) -> Dict:
    """The realization package written for *package*'s lineage."""
    return json.loads((out / "realization" / "packages" / f"{make_rpkg_id(package['recommendation_package_id'])}.json").read_text(encoding="utf-8"))


def state_index(out: Path) -> Dict:
    """The warehouse's state index (one warehouse per directory)."""
    (path,) = sorted((out / "realization" / "state").glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def final_files(out: Path) -> Dict[str, str]:
    """The files a reader uses: the ledgers, the realization packages and the state index (snapshots are history)."""
    return {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in sorted((out / "realization").rglob("*"))
            if p.suffix in {".json", ".jsonl"} and p.parent.name in {"ledger", "packages", "state"}}
