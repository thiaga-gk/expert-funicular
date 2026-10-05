#!/usr/bin/env python3
"""Run the publisher CLI from a checkout without installing it: same arguments as ``python -m publisher``."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from publisher.cli.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
