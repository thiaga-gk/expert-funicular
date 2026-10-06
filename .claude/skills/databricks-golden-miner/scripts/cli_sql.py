"""Thin read-only wrapper around the Databricks CLI for the golden-miner scripts.

Runs `databricks ...` as a subprocess with DATABRICKS_TOKEN removed (an ambient PAT is never used, as in
src/publisher/analyzer/collector/workspace_api.py) and executes SELECT statements through the SQL Statement Execution
API (`databricks api post /api/2.0/sql/statements`). Nothing here prints tokens; callers print only step names, states
and error messages returned by Databricks.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence


class CliError(RuntimeError):
    """A CLI call failed; `remedy` is the command or action that fixes it."""

    def __init__(self, message: str, remedy: str = ""):
        super().__init__(message)
        self.remedy = remedy


def run_cli(args: Sequence[str], profile: Optional[str] = None, config_file: Optional[str] = None, timeout: int = 120) -> str:
    """stdout of `databricks <args> [-p profile]`; CliError on a missing CLI, a non-zero exit or a timeout."""
    cmd = ["databricks", *args] + (["-p", profile] if profile else [])
    env = dict(os.environ)
    env.pop("DATABRICKS_TOKEN", None)
    if config_file:
        env["DATABRICKS_CONFIG_FILE"] = str(config_file)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except FileNotFoundError as exc:
        raise CliError("Databricks CLI is not installed or not on PATH",
                       "install it: https://docs.databricks.com/aws/en/dev-tools/cli/install (e.g. brew install databricks)") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"`databricks {' '.join(args[:3])}` timed out after {timeout}s") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise CliError(f"`databricks {' '.join(args[:3])}` failed: {detail[-1] if detail else 'exit ' + str(done.returncode)}")
    return done.stdout


def execute(statement: str, warehouse_id: str, profile: str, config_file: Optional[str] = None,
            poll_seconds: float = 5.0, max_wait_seconds: float = 600.0) -> Dict[str, Any]:
    """Run one read-only SELECT on `warehouse_id`; returns {"columns": [...], "rows": [[...]]}.

    Raises CliError when the statement fails, is cancelled or does not finish within max_wait_seconds.
    """
    if not statement.lstrip().upper().startswith(("SELECT", "WITH")):
        raise CliError("only SELECT/WITH statements are allowed")
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
