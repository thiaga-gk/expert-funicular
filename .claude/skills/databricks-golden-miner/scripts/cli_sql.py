"""Thin read-only wrapper around the Databricks CLI for the golden-miner scripts.

Runs `databricks ...` as a subprocess with DATABRICKS_TOKEN removed (an ambient PAT is never used, as in
src/publisher/analyzer/collector/workspace_api.py) and executes SELECT statements through the SQL Statement Execution
API (`databricks api post /api/2.0/sql/statements`). Nothing here prints tokens; callers print only step names, states
and error messages returned by Databricks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence


class CliError(RuntimeError):
    """A CLI call failed; `remedy` is the command or action that fixes it."""

    def __init__(self, message: str, remedy: str = ""):
        super().__init__(message)
        self.remedy = remedy


_sleep = time.sleep
ATTEMPTS = 3  # tries per CLI call when Databricks answers 429 or a transient 5xx
BACKOFF_SECONDS = 2.0  # wait before the second try; doubles before the third
# Best effort: the CLI prints the HTTP status or the Databricks error code in its error output.
_TRANSIENT = re.compile(r"\b(429|500|502|503|504)\b|too many requests|rate.?limit|request_limit_exceeded|temporarily_unavailable|"
                        r"temporarily unavailable|service unavailable|bad gateway|gateway time-?out|internal server error", re.I)

WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE", "COPY",
                  "OPTIMIZE", "VACUUM")


def strip_comments_and_literals(statement: str) -> str:
    """The statement's code: `--` and (nested) `/* */` comments become a space, string literals ('..', "..") and
    backtick identifiers become empty quotes. CliError on an unterminated comment, literal or identifier."""
    out: List[str] = []
    i, n = 0, len(statement)
    while i < n:
        ch = statement[i]
        if statement.startswith("--", i):
            end = statement.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif statement.startswith("/*", i):
            depth, i = 1, i + 2
            while depth and i < n:
                if statement.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif statement.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise CliError("unterminated /* comment in the statement")
            out.append(" ")
        elif ch in "'\"`":
            i += 1
            while i < n and statement[i] != ch:
                i += 2 if statement[i] == "\\" and ch != "`" else 1
                if i < n and statement[i] == ch and i + 1 < n and statement[i + 1] == ch:  # doubled quote escapes itself
                    i += 2
            if i >= n:
                raise CliError("unterminated string literal or identifier in the statement")
            i += 1
            out.append(ch + ch)
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_read_only(statement: str) -> None:
    """CliError unless the statement is one SELECT/WITH query: comments are ignored, and none of WRITE_KEYWORDS may
    appear as a whole word outside string literals and quoted identifiers (so a CTE ending in DML is refused)."""
    code = strip_comments_and_literals(statement).strip().rstrip(";").strip()
    if not re.match(r"(SELECT|WITH)\b", code, re.I):
        raise CliError("only SELECT/WITH statements are allowed")
    if ";" in code:
        raise CliError("only a single statement is allowed")
    hit = re.search(r"\b(" + "|".join(WRITE_KEYWORDS) + r")\b", code, re.I)
    if hit:
        raise CliError(f"statement contains the write keyword {hit.group(1).upper()}; only read queries are allowed")


def run_cli(args: Sequence[str], profile: Optional[str] = None, config_file: Optional[str] = None, timeout: int = 120) -> str:
    """stdout of `databricks <args> [-p profile]`; CliError on a missing CLI, a non-zero exit or a timeout.

    A call that fails with HTTP 429 or a transient 5xx is retried (ATTEMPTS tries, exponential backoff from
    BACKOFF_SECONDS); any other failure is raised at once."""
    cmd = ["databricks", *args] + (["-p", profile] if profile else [])
    env = dict(os.environ)
    env.pop("DATABRICKS_TOKEN", None)
    if config_file:
        env["DATABRICKS_CONFIG_FILE"] = str(config_file)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        except FileNotFoundError as exc:
            raise CliError("Databricks CLI is not installed or not on PATH",
                           "install it: https://docs.databricks.com/aws/en/dev-tools/cli/install (e.g. brew install databricks)") from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError(f"`databricks {' '.join(args[:3])}` timed out after {timeout}s") from exc
        if done.returncode == 0:
            return done.stdout
        text = (done.stderr or done.stdout or "").strip()
        if attempt < ATTEMPTS and _TRANSIENT.search(text):
            _sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
            continue
        detail = text.splitlines()
        raise CliError(f"`databricks {' '.join(args[:3])}` failed: {detail[-1] if detail else 'exit ' + str(done.returncode)}")
    raise AssertionError("unreachable")  # pragma: no cover


def execute(statement: str, warehouse_id: str, profile: str, config_file: Optional[str] = None,
            poll_seconds: float = 5.0, max_wait_seconds: float = 600.0) -> Dict[str, Any]:
    """Run one read-only SELECT on `warehouse_id`; returns {"columns": [...], "rows": [[...]]}.

    Raises CliError when the statement is not a single read query (check_read_only), fails, is cancelled or does not
    finish within max_wait_seconds.
    """
    check_read_only(statement)
    body = {"warehouse_id": warehouse_id, "statement": statement, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE", "format": "JSON_ARRAY"}
    resp = json.loads(run_cli(["api", "post", "/api/2.0/sql/statements", "--json", json.dumps(body)], profile, config_file))
    started = time.monotonic()
    while resp.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        if time.monotonic() - started > max_wait_seconds:
            run_cli(["api", "post", f"/api/2.0/sql/statements/{resp['statement_id']}/cancel"], profile, config_file)
            raise CliError(f"statement did not finish within {max_wait_seconds:.0f}s (cancelled)")
        time.sleep(poll_seconds)
        resp = json.loads(run_cli(["api", "get", f"/api/2.0/sql/statements/{resp['statement_id']}"], profile, config_file))
    state = resp.get("status", {}).get("state")
    if state != "SUCCEEDED":
        err = resp.get("status", {}).get("error", {}) or {}
        raise CliError(f"statement {state}: {err.get('error_code', '')} {err.get('message', '')}".strip())
    columns = [c["name"] for c in (resp.get("manifest", {}).get("schema", {}).get("columns") or [])]
    rows: List[List[Any]] = list((resp.get("result") or {}).get("data_array") or [])
    chunk = (resp.get("result") or {}).get("next_chunk_internal_link")
    while chunk:
        part = json.loads(run_cli(["api", "get", chunk], profile, config_file))
        rows.extend(part.get("data_array") or [])
        chunk = part.get("next_chunk_internal_link")
    return {"columns": columns, "rows": rows}
