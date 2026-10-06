"""Subprocess entry point of ``test_subprocess_determinism``.

``python -m tests.regression.realizer.dump_canonical <scenario>...`` runs the scenarios in this one
process and prints ``{"meta": ..., "scenarios": {name: {relative path: text}}}`` as sorted JSON. ``meta`` records the
process environment that the test varies (hash seed and time zone), so that the test can check the variation took effect.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests.regression.realizer import canonical  # noqa: E402


def meta() -> dict:
    return {
        "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
        "TZ": os.environ.get("TZ"),
        "hash_probe": hash("realizer-determinism-probe"),
        "utc_offset_seconds": time.localtime(0).tm_gmtoff,
    }


def main(argv) -> int:
    names = argv
    engine = canonical.modular_engine()
    scenarios = {name: canonical.produce(engine, name) for name in names}
    json.dump({"meta": meta(), "scenarios": scenarios}, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
