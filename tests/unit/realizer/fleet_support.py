"""Publisher run folders for the fleet-run tests: a real Publisher demo run, and minimal run folders of given packages."""
import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from tests.unit.realizer.helpers import ROOT

DEMO_WAREHOUSES = ("wh-demo-006", "wh-demo-013", "wh-demo-039")


@functools.lru_cache(maxsize=1)
def publisher_demo_root() -> Path:
    """A root holding one real Publisher demo run (``python -m publisher --demo``) of ``DEMO_WAREHOUSES``, made once per
    process and removed at exit. Read-only for the tests."""
    root = Path(tempfile.mkdtemp(prefix="realizer-fleet-publisher-"))
    atexit.register(shutil.rmtree, root, True)
    argv = [sys.executable, "-m", "publisher", "--demo", "--output-dir", str(root)]
    for warehouse in DEMO_WAREHOUSES:
        argv += ["--warehouse", warehouse]
    subprocess.run(argv, check=True, capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=str(root))
    return root


def run_folder(root: Path, packages: Sequence[Mapping[str, Any]], name: str = "run_date=2026-10-15/run_ts=20261015T120000Z") -> Path:
    """A minimal Publisher run folder under *root* holding *packages* and a manifest listing them."""
    folder = root / name
    (folder / "packages").mkdir(parents=True)
    entries = []
    for package in packages:
        warehouse = package["warehouse"]
        path = f"packages/{warehouse['workspace_id']}__{warehouse['warehouse_id']}.json"
        (folder / path).write_text(json.dumps(package), encoding="utf-8")
        entries.append({"path": path, "workspace_id": warehouse["workspace_id"], "warehouse_id": warehouse["warehouse_id"],
                        "recommendation_package_id": package["recommendation_package_id"]})
    (folder / "manifest.json").write_text(json.dumps({"package_count": len(entries), "packages": entries}), encoding="utf-8")
    return folder
