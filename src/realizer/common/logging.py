"""The Realizer logger and its CLI setup.

Every module logs through ``LOGGER`` (the ``realizer`` logger) as ``<component>.<event> key=value ...`` with lazy
%-style arguments. Log lines are never canonical output. The Realizer logs to stderr only.
"""
from __future__ import annotations

import logging

LOGGER = logging.getLogger("realizer")


def setup_logging(level: str) -> None:
    """Configure root logging for one CLI run, exactly as the single-file Realizer's ``main`` did.

    *level* is one of DEBUG, INFO, WARNING, ERROR. The format appends a literal ``Z`` to ``asctime``, which renders
    local time; that mislabel is single-file behavior kept for parity.
    """
    logging.basicConfig(level=getattr(logging, level), format="%(asctime)sZ %(levelname)s %(message)s")
