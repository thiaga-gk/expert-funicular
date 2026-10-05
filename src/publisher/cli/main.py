"""``publisher`` console entry point (also ``python -m publisher``).

``publisher publish [options]`` runs the publisher; without a subcommand the options go to ``publish``, so
``publisher --demo`` keeps working. A refused run (PublisherError) prints its message to stderr and exits 2.
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, Optional, Sequence

from publisher.cli import publish
from publisher.common.errors import PublisherError

COMMANDS: Dict[str, Callable[[Optional[Sequence[str]]], int]] = {"publish": publish.run}
EXIT_REFUSED = 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = COMMANDS.get(args[0]) if args else None
    try:
        return command(args[1:]) if command else publish.run(args)
    except PublisherError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
