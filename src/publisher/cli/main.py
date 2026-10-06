"""``publisher`` console entry point (also ``python -m publisher``).

``publisher publish [options]`` runs the publisher; without a subcommand the options go to ``publish``, so
``publisher --demo`` keeps working. A refused run (PublisherError) prints its message to stderr and exits 2.
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, Optional, Sequence

from publisher.cli import publish
from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER

COMMANDS: Dict[str, Callable[[Optional[Sequence[str]]], int]] = {"publish": publish.run}
EXIT_REFUSED = 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Dispatch to a subcommand (default ``publish``) and return the process exit status.

    0 on success, 2 (EXIT_REFUSED) for a PublisherError, whose message goes to stderr and, once the run log is set
    up, to ``publisher.log`` so the log records why a run stopped. An unexpected exception is logged by type only
    (its message may carry data values) and re-raised for the traceback on stderr. argparse errors exit through
    argparse itself (also status 2). *argv* defaults to ``sys.argv[1:]``.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    command = COMMANDS.get(args[0]) if args else None
    try:
        return command(args[1:]) if command else publish.run(args)
    except PublisherError as exc:
        if LOGGER.handlers:
            LOGGER.error("publisher.refused exit=%d error=%s", EXIT_REFUSED, exc)
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        if LOGGER.handlers:
            LOGGER.error("publisher.crashed error_type=%s", type(exc).__name__)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
