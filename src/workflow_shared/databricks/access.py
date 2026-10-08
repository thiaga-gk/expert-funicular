"""Read-only Databricks access: OAuth from a CLI profile, REST GET and a UTC-pinned SQL client.

Only GET requests are issued; there is no mutation or apply API. Authentication is an OAuth U2M token minted by the
Databricks CLI for a profile; a profile holding a static PAT is rejected. Tokens, Authorization headers, query
parameters and SQL text are never logged; logs carry the REST path, status, query label, row count, elapsed time and
exception class only. The calling product injects its identity through ``AccessConfig``: the logger every line goes
to, the user agent sent to Databricks and the exception type every refusal raises.
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type


UTC_TIMEZONE_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Z", "+00:00", "GMT", "Etc/GMT"})


@dataclass(frozen=True)
class AccessConfig:
    """The calling product's identity: ``logger`` for every log line, ``user_agent`` for REST and the SQL connector,
    ``error_type`` (built from one message string) for every refusal."""
    logger: logging.Logger
    user_agent: str
    error_type: Type[Exception]


@dataclass(frozen=True)
class AuthContext:
    """Resolved local OAuth authentication context; token is never reported or logged.

    ``http_path`` is the SQL connector path for ``execution_kind`` ("warehouse" or "cluster") and ``execution_id``.
    ``workspace_id`` is None when it could not be resolved (a warehouse target does not need it to connect).
    """
    profile: str
    host: str
    http_path: str
    access_token: str = field(repr=False)
    token_expiry: Optional[str]
    execution_kind: str
    execution_id: str
    workspace_id: Optional[str] = None


def load_profile_values(config: AccessConfig, profile: str, config_file: Optional[str] = None) -> Dict[str, str]:
    """Raw key/values of a Databricks CLI profile (config_file, DATABRICKS_CONFIG_FILE or ~/.databrickscfg).

    May contain secrets, so callers read only the keys they need and nothing here is logged. Raises
    ``config.error_type`` when the file or profile is missing ("DEFAULT" reads the file's defaults section).
    """
    path = Path(config_file or os.getenv("DATABRICKS_CONFIG_FILE") or Path.home() / ".databrickscfg")
    if not path.exists():
        raise config.error_type(f"Databricks CLI config not found: {path}")
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except (configparser.Error, UnicodeDecodeError) as exc:
        # The parser's message quotes a line of the file, which may hold a secret: only the file and the error class.
        raise config.error_type(f"Databricks CLI config could not be parsed: {path} ({type(exc).__name__})") from None
    if profile == "DEFAULT":
        section: Mapping[str, str] = parser.defaults()
    elif parser.has_section(profile):
        section = parser[profile]
    else:
        raise config.error_type(f"Databricks CLI profile not found: {profile} in {path}")
    return {str(k): str(v) for k, v in section.items()}


def load_profile_host(config: AccessConfig, profile: str, config_file: Optional[str] = None) -> str:
    """Workspace host of the profile without scheme or trailing slash; ``config.error_type`` when the profile has none."""
    section = load_profile_values(config, profile, config_file)
    host = str(section.get("host") or "").strip()
    if not host:
        raise config.error_type(f"Databricks host is missing from CLI profile: {profile}")
    return host.replace("https://", "").replace("http://", "").rstrip("/")


def resolve_cli_token(config: AccessConfig, profile: str, config_file: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    """Mint an OAuth access token through ``databricks auth token`` and return ``(host, token, expiry)``.

    Fails closed (``config.error_type``) when the profile carries a static PAT, the CLI is missing or fails, or it
    returns no token. DATABRICKS_TOKEN is removed from the child environment so an ambient PAT can never be used.
    """
    values = load_profile_values(config, profile, config_file)
    if values.get("token"):
        raise config.error_type(f"Profile {profile} contains a PAT token. Use an OAuth U2M Databricks CLI profile.")
    host = load_profile_host(config, profile, config_file)
    cmd = ["databricks", "auth", "token", profile, "-o", "json"]
    clean_env = dict(os.environ)
    clean_env.pop("DATABRICKS_TOKEN", None)
    if config_file:
        clean_env["DATABRICKS_CONFIG_FILE"] = str(config_file)
    config.logger.info("auth.resolve profile=%s host=%s", profile, host)
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=clean_env)
    except FileNotFoundError as exc:
        raise config.error_type("Databricks CLI is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise config.error_type(f"Unable to resolve OAuth token for profile {profile}: {detail}") from exc
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        # The CLI's output may carry a token: nothing of it is quoted.
        raise config.error_type(f"Databricks CLI returned output that is not a JSON object for profile {profile}")
    token = str(payload.get("access_token") or "")
    if not token:
        raise config.error_type(f"Databricks CLI returned no access token for profile {profile}")
    return host, token, str(payload.get("expiry") or "") or None


def api_get(config: AccessConfig, host: str, token: str, path: str, params: Optional[Mapping[str, Any]] = None, *, return_headers: bool = False) -> Any:
    """GET ``path`` with the bearer token and return the parsed JSON (plus response headers when asked).

    Network, HTTP and JSON errors are logged by class and re-raised unchanged; the caller decides whether to fall back.
    Only the path is logged: query parameters and the Authorization header are not.
    """
    url = f"https://{host.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": config.user_agent}, method="GET")
    started = time.monotonic()
    config.logger.info("api.get start path=%s", path)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body.strip() else {}
            config.logger.info("api.get done path=%s status=%s elapsed_ms=%d", path, getattr(response, "status", 200), int((time.monotonic()-started)*1000))
            return (payload, dict(response.headers.items())) if return_headers else payload
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        config.logger.warning("api.get failed path=%s error=%s", path, type(exc).__name__)
        raise


def current_workspace_id(config: AccessConfig, host: str, token: str) -> Optional[str]:
    """Workspace ID from the ``x-databricks-org-id`` header of a SCIM ``Me`` call, or None when it cannot be read.

    Best effort by design: any failure yields None (logged as a warning) and the caller decides what that means.
    """
    try:
        _payload, headers = api_get(config, host, token, "/api/2.0/preview/scim/v2/Me", return_headers=True)
        for key, value in headers.items():
            if key.lower() == "x-databricks-org-id" and str(value).strip():
                return str(value).strip()
    except Exception as exc:
        config.logger.warning("auth.workspace_id_unavailable reason=WORKSPACE_ID_LOOKUP_FAILED error=%s", type(exc).__name__)
        return None
    config.logger.warning("auth.workspace_id_unavailable reason=ORG_ID_HEADER_MISSING")
    return None


class SqlClient:
    """Thin read-only Databricks SQL connector wrapper with labeled query logging.

    Use as a context manager: entering opens the connection with the session pinned to UTC and verifies it; leaving
    closes it. ``query`` returns rows as plain dicts keyed by column name and logs the label, never the statement.
    """
    def __init__(self, auth: AuthContext, config: AccessConfig):
        self.auth = auth
        self.config = config
        self._connection = None

    def __enter__(self) -> "SqlClient":
        """Connect with the OAuth token from ``auth`` and fail closed unless the session time zone is UTC.

        Raises ``config.error_type`` when the connector is not installed or the verified session time zone is not UTC
        (the connection is closed first), so no query runs against a misaligned clock.
        """
        try:
            from databricks import sql  # type: ignore
        except ImportError as exc:
            raise self.config.error_type('Install dependency: python -m pip install "databricks-sql-connector>=4.2.6"') from exc
        # Billing usage_date and every analysis window are UTC; DATE()/CURRENT_DATE() follow the session time zone,
        # so the session is pinned to UTC and verified before any other query runs.
        self._connection = sql.connect(
            server_hostname=self.auth.host,
            http_path=self.auth.http_path,
            access_token=self.auth.access_token,
            user_agent_entry=self.config.user_agent,
            session_configuration={"TIMEZONE": "UTC"},
        )
        rows = self.query("SELECT current_timezone() AS tz", label="session_timezone")
        timezone = str(rows[0].get("tz") if rows else "")
        if timezone not in UTC_TIMEZONE_NAMES:
            self.__exit__(None, None, None)
            raise self.config.error_type(f"SQL session time zone is {timezone or 'unknown'}, not UTC; collector dates would not align with UTC billing dates.")
        self.config.logger.info("sql.session_ready time_zone_utc=true")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Close the connection if one was opened; never suppresses an exception."""
        if self._connection is not None:
            self._connection.close()
            self.config.logger.debug("sql.closed")

    def query(self, statement: str, *, label: str = "collector_sql") -> List[Dict[str, Any]]:
        """Execute one statement and return all rows as dicts; ``label`` is the only identifier that is logged.

        Requires an open connection (use inside ``with``). Rows are fully fetched in memory, so callers run aggregate
        queries.
        """
        assert self._connection is not None
        started=time.monotonic()
        self.config.logger.info("sql.start label=%s", label)
        with self._connection.cursor() as cursor:
            cursor.execute(statement)
            cols = [d[0] for d in cursor.description]
            rows=[dict(zip(cols, row)) for row in cursor.fetchall()]
        self.config.logger.info("sql.done label=%s rows=%d elapsed_ms=%d", label, len(rows), int((time.monotonic()-started)*1000))
        return rows
