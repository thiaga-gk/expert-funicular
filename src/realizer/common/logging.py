"""The Realizer logger and its CLI setup.

Every module logs through ``LOGGER`` (the ``realizer`` logger) as ``<component>.<event> key=value ...`` with lazy
%-style arguments. Log lines are never canonical output and never carry credentials, SQL text or data values. The
Realizer logs to stderr; a CLI run in a run folder also writes its log to ``<run folder>/realizer.log``
(``attach_log_file``).
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

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
    handler = _Stderr()
    handler.setFormatter(_formatter())
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


class _Stderr(logging.StreamHandler):
    """The stderr handler; it also keeps the run's lines until a log file is attached (``attach_log_file`` writes them
    there first), then stops keeping them."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.early: Optional[List[logging.LogRecord]] = []

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        if self.early is not None:
            self.early.append(record)


def _formatter() -> logging.Formatter:
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime  # the literal "Z" must be true: render UTC, not local time
    return formatter


def attach_log_file(path: Path) -> Path:
    """Also write the ``realizer`` logger's lines to *path* (appended, same format as stderr), starting with the lines
    logged since ``setup_logging``; return *path*. Called when the run folder exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_formatter())
    for stderr in [h for h in LOGGER.handlers if isinstance(h, _Stderr)]:
        for record in stderr.early or ():
            handler.handle(record)
        stderr.early = None
    LOGGER.addHandler(handler)
    return path


def detach_log_files() -> None:
    """Close and remove the run log file handlers (``attach_log_file``), so the logger never outlives the run's folder;
    the stderr handler stays."""
    for handler in list(LOGGER.handlers):
        if isinstance(handler, logging.FileHandler):
            LOGGER.removeHandler(handler)
            handler.close()
