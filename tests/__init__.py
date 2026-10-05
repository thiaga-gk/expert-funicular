"""Test package: puts the repository root and ``src/`` on ``sys.path`` so tests run without an install."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
