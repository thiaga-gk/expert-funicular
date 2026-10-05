#!/usr/bin/env python3
"""Run the demo fleet (one simulated warehouse per golden scenario) into ./demo_out, or the directory given.

Usage: python scripts/run_demo.py [output_dir] [extra publisher arguments...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from publisher.publisher import main  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:]
    out = args.pop(0) if args and not args[0].startswith("-") else "./demo_out"
    raise SystemExit(main(["--demo", "--output-dir", out, *args]))
