"""Read-only SQL execution against a Databricks SQL warehouse or all-purpose cluster.

The client only ever runs the collector's SELECT statements (no DDL/DML, no apply API). Logs carry the query label,
row count and elapsed time; the statement text and the access token are never logged. ``SqlClient`` is the shared
read-only client (``workflow_shared.databricks.access``) bound to the publisher's identity (``PUBLISHER_ACCESS``).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from publisher.analyzer.collector.workspace_api import PUBLISHER_ACCESS, AuthContext
from publisher.common.errors import PublisherError
from workflow_shared.databricks import access


class SqlClient(access.SqlClient):
    """The shared read-only SQL client with the publisher's logger, user agent and ``PublisherError``.

    Use as a context manager: entering opens the connection with the session pinned to UTC and verifies it (a
    PublisherError otherwise); leaving closes it. ``query`` returns rows as dicts and logs the label, never the statement.
    """
    def __init__(self, auth: AuthContext):
        super().__init__(auth, PUBLISHER_ACCESS)


def with_ingestion_cutoff(statement: str, cutoff: Optional[str]) -> str:
    """Pin a billing query to records ingested on or before `cutoff` (YYYY-MM-DD), so a re-collection reproduces an
    earlier run after late corrections; without a cutoff the predicate is a no-op.

    Raises PublisherError for a cutoff that is not an ISO date, so a malformed value can never reach the SQL text."""
    if cutoff is None:
        return statement.replace("__INGESTION_CUTOFF__", "CAST(NULL AS DATE)")
    try:
        day = dt.date.fromisoformat(str(cutoff))
    except ValueError as exc:
        raise PublisherError(f"--billing-ingested-through must be a YYYY-MM-DD date, got {cutoff!r}") from exc
    return statement.replace("__INGESTION_CUTOFF__", f"DATE'{day.isoformat()}'")
