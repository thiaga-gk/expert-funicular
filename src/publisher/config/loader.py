"""Configuration loading: resolve the execution mode a request runs under."""
from __future__ import annotations

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.config.settings import EXECUTION_MODES


def resolve_execution_mode(mode: str) -> str:
    """The execution mode a run uses. Only local execution exists today; other modes (e.g. a Databricks Asset Bundle
    job) resolve here to their adapters when they are built.

    Raises PublisherError (CLI exit status 2) for a mode outside EXECUTION_MODES. The mode is a non-sensitive label.
    """
    if mode not in EXECUTION_MODES:
        raise PublisherError(f"Unsupported execution mode {mode!r}; supported: {', '.join(EXECUTION_MODES)}")
    LOGGER.debug("config.execution_mode mode=%s", mode)
    return mode
