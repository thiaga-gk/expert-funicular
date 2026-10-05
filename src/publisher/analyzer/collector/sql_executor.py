"""Read-only SQL execution against a Databricks SQL warehouse or all-purpose cluster."""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Dict, List, Optional

from publisher.analyzer.collector.workspace_api import AuthContext
from publisher.common.logging import LOGGER
from publisher.config.versions import PUBLISHER_VERSION


UTC_TIMEZONE_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Z", "+00:00", "GMT", "Etc/GMT"})


class SqlClient:
    """Thin read-only Databricks SQL connector wrapper with labeled query logging."""
    def __init__(self, auth: AuthContext):
        self.auth = auth
        self._connection = None

    def __enter__(self) -> "SqlClient":
        try:
            from databricks import sql  # type: ignore
        except ImportError as exc:
            raise SystemExit('Install dependency: python -m pip install "databricks-sql-connector>=4.2.6"') from exc
        # Billing usage_date and every analysis window are UTC; DATE()/CURRENT_DATE() follow the session time zone,
        # so the session is pinned to UTC and verified before any collector query runs.
        self._connection = sql.connect(
            server_hostname=self.auth.host,
            http_path=self.auth.http_path,
            access_token=self.auth.access_token,
            user_agent_entry=f"sql-warehouse-optimizer/{PUBLISHER_VERSION}",
            session_configuration={"TIMEZONE": "UTC"},
        )
        rows = self.query("SELECT current_timezone() AS tz", label="session_timezone")
        timezone = str(rows[0].get("tz") if rows else "")
        if timezone not in UTC_TIMEZONE_NAMES:
            self.__exit__(None, None, None)
            raise SystemExit(f"SQL session time zone is {timezone or 'unknown'}, not UTC; collector dates would not align with UTC billing dates.")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._connection is not None:
            self._connection.close()

    def query(self, statement: str, *, label: str = "collector_sql") -> List[Dict[str, Any]]:
        assert self._connection is not None
        started=time.monotonic()
        LOGGER.info("sql.start label=%s", label)
        with self._connection.cursor() as cursor:
            cursor.execute(statement)
            cols = [d[0] for d in cursor.description]
            rows=[dict(zip(cols, row)) for row in cursor.fetchall()]
        LOGGER.info("sql.done label=%s rows=%d elapsed_ms=%d", label, len(rows), int((time.monotonic()-started)*1000))
        return rows


def with_ingestion_cutoff(statement: str, cutoff: Optional[str]) -> str:
    """Pin a billing query to records ingested on or before `cutoff` (YYYY-MM-DD), so a re-collection reproduces an
    earlier run after late corrections; without a cutoff the predicate is a no-op."""
    if cutoff is None:
        return statement.replace("__INGESTION_CUTOFF__", "CAST(NULL AS DATE)")
    try:
        day = dt.date.fromisoformat(str(cutoff))
    except ValueError as exc:
        raise SystemExit(f"--billing-ingested-through must be a YYYY-MM-DD date, got {cutoff!r}") from exc
    return statement.replace("__INGESTION_CUTOFF__", f"DATE'{day.isoformat()}'")
