"""The Realizer logger and its CLI setup.

Every module logs through ``LOGGER`` (the ``realizer`` logger) as ``<component>.<event> key=value ...`` with lazy
%-style arguments. Log lines are never canonical output. The Realizer logs to stderr only.
"""
from __future__ import annotations

import logging
import sys
import time

LOGGER = logging.getLogger("realizer")


def setup_logging(level: str) -> None:
    """Configure the ``realizer`` logger for one CLI run: stderr, ``<UTC time>Z LEVEL message``.

    *level* is one of DEBUG, INFO, WARNING, ERROR. Existing handlers are replaced, so repeated calls in one process never
    duplicate lines; the logger does not propagate to the root logger.
    """
    LOGGER.setLevel(getattr(logging, level))
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime  # the literal "Z" must be true: render UTC, not local time
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)
    LOGGER.propagate = False
