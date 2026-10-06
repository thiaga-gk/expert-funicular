"""The publisher logger and its run-log setup (stderr plus <output-dir>/publisher.log).

Convention for every module: ``from publisher.common.logging import LOGGER`` and
``LOGGER.<level>("<component>.<event> key=value ...", args...)`` with a dotted lowercase event, space-separated
key=value pairs and lazy %-style arguments. INFO marks lifecycle milestones and aggregate counts, DEBUG per-item
detail, WARNING a degraded or fallback path, ERROR only just before a failure that carries no message of its own.
Log lines are never canonical output and must not carry credentials, owner emails or tags, or SQL text.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from publisher.config.versions import PUBLISHER_VERSION


LOGGER = logging.getLogger("publisher")


def setup_logging(output_dir: Path, level: str = "INFO") -> Path:
    """Configure the ``publisher`` logger for one run and return the log file path.

    Creates *output_dir* and attaches two handlers with the same format, ``<UTC timestamp>Z LEVEL message``: stderr
    and ``<output_dir>/publisher.log`` (opened in append mode, so reusing an output directory extends the earlier
    log). *level* is a case-insensitive standard level name; an unknown name
    falls back to INFO. Existing handlers are closed and removed first, so repeated calls in one process (tests, a
    second run) never duplicate lines. The logger does not propagate to the root logger. Logs the
    ``publisher.start`` line as its last step.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "publisher.log"
    logger = logging.getLogger("publisher")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime  # the literal "Z" in the format must be true: render UTC, not local time
    for handler in (logging.StreamHandler(sys.stderr), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    logger.info("publisher.start version=%s", PUBLISHER_VERSION)
    return log_path
