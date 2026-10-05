"""The publisher logger and its run-log setup (stderr plus <output-dir>/publisher.log)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from publisher.config.versions import PUBLISHER_VERSION


LOGGER = logging.getLogger("publisher")


def setup_logging(output_dir: Path, level: str = "INFO") -> Path:
    output_dir.mkdir(parents=True,exist_ok=True)
    log_path=output_dir/"publisher.log"
    logger=logging.getLogger("publisher")
    logger.setLevel(getattr(logging,str(level).upper(),logging.INFO))
    logger.handlers.clear()
    fmt=logging.Formatter("%(asctime)sZ %(levelname)s %(message)s",datefmt="%Y-%m-%dT%H:%M:%S")
    sh=logging.StreamHandler(sys.stderr); sh.setFormatter(fmt); logger.addHandler(sh)
    fh=logging.FileHandler(log_path,encoding="utf-8"); fh.setFormatter(fmt); logger.addHandler(fh)
    logger.propagate=False
    logger.info("publisher.start version=%s",PUBLISHER_VERSION)
    return log_path
