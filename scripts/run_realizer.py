#!/usr/bin/env python3
"""Run the Realizer CLI from a checkout without installing it: same arguments as ``python -m realizer``.

Usage: python scripts/run_realizer.py --package <PKG.json> [--observation <file> | --demo] [options]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realizer.cli.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
