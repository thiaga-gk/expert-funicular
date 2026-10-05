"""Configuration loading: resolve the execution mode a request runs under."""
from __future__ import annotations

from publisher.common.errors import PublisherError
from publisher.config.settings import EXECUTION_MODES


def resolve_execution_mode(mode: str) -> str:
    """The execution mode a run uses. Only local execution exists today; other modes (e.g. a Databricks Asset Bundle
    job) resolve here to their adapters when they are built."""
    if mode not in EXECUTION_MODES:
        raise PublisherError(f"Unsupported execution mode {mode!r}; supported: {', '.join(EXECUTION_MODES)}")
    return mode
