"""``realizer`` console entry point (also ``python -m realizer``).

The Realizer has one command (a realize run, or a backtest with ``--backtest``), so the arguments go straight to
``realizer.cli.realize.run``. Exit status: 0 on success; 2 for a refused run (a ``RealizerError``: an input the Realizer
does not accept, an unreadable file or ledger), whose message goes to stderr, and for bad arguments (argparse). Any other exception is a defect: it is logged by type only
(its message may carry data values) and re-raised for the traceback (status 1).
"""
from __future__ import annotations

import sys
from typing import Optional, Sequence

from realizer.cli import realize
from realizer.common.errors import RealizerError
from realizer.common.logging import LOGGER, detach_log_files

EXIT_REFUSED = 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the Realizer and return the process exit status. *argv* defaults to ``sys.argv[1:]``."""
    try:
        return realize.run(argv)
    except RealizerError as exc:
        if LOGGER.handlers:
            LOGGER.error("realizer.refused exit=%d error_type=%s", EXIT_REFUSED, type(exc).__name__)
        print(f"realizer: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        if LOGGER.handlers:
            LOGGER.error("realizer.crashed error_type=%s", type(exc).__name__)
        raise
    finally:
        detach_log_files()


if __name__ == "__main__":
    raise SystemExit(main())
