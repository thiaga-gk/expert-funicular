"""``realizer`` console entry point (also ``python -m realizer``).

The Realizer has one command, so the arguments go straight to ``realizer.cli.realize.run``. The exit status is the
single-file CLI's: 0 on success, argparse's 2 for bad arguments; any other failure is an uncaught exception (a traceback
on stderr, status 1).
"""
from __future__ import annotations

from typing import Optional, Sequence

from realizer.cli import realize


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the Realizer and return the process exit status. *argv* defaults to ``sys.argv[1:]``."""
    return realize.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
