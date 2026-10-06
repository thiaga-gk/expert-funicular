#!/usr/bin/env python3
"""Preflight for a live golden-mining run: Databricks CLI access is a hard prerequisite (read-only, fail closed).

Usage:
  python .claude/skills/databricks-golden-miner/scripts/preflight.py --profile <oauth-profile> --warehouse-id <id>
      [--config-file <path>] [--min-cli-version 0.218.0] [--skip-table-probes]

Checks, in order, and stops at the first failure with the command that fixes it:
  1. the `databricks` CLI is on PATH at or above --min-cli-version
  2. the profile exists in the CLI config and holds no static PAT (`token`), the publisher's OAuth-only rule
  3. `databricks auth token` mints an OAuth token for the profile (the token is never printed)
  4. `databricks current-user me` answers (the identity is printed, nothing else)
  5. `databricks warehouses get <id>` sees the warehouse that will run the collector SQL
  6. a `SELECT 1 ... LIMIT 1` probe through the Statement Execution API reads each system table the collector uses
     (starts the warehouse if it is stopped)
Exit 0 when everything passes, 1 on the first failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_sql import CliError, execute, run_cli  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

MIN_CLI_VERSION = "0.218.0"
SYSTEM_TABLES = ("system.compute.warehouses", "system.compute.warehouse_events", "system.query.history",
                 "system.billing.usage", "system.billing.list_prices")


def _version(text: str) -> Optional[Tuple[int, ...]]:
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def check_cli(min_version: str) -> str:
    out = run_cli(["--version"]).strip()
    have, need = _version(out), _version(min_version)
    if have is None or have < need:
        raise CliError(f"CLI version {out or 'unknown'} is older than {min_version}",
                       "upgrade it: brew upgrade databricks (or https://docs.databricks.com/aws/en/dev-tools/cli/install)")
    return out


def check_profile(profile: str, config_file: Optional[str]) -> str:
    try:
        from publisher.analyzer.collector.workspace_api import _load_profile_values
        from publisher.common.errors import PublisherError
    except ImportError as exc:  # the publisher is the source of the OAuth-only rule
        raise CliError("cannot import the publisher", "pip install -e . from the repository root") from exc
    try:
        values = _load_profile_values(profile, config_file)
    except PublisherError as exc:
        raise CliError(str(exc), f"databricks auth login --host https://<workspace-host> --profile {profile}") from exc
    if values.get("token"):
        raise CliError(f"profile {profile} holds a static PAT; the publisher accepts OAuth U2M profiles only",
                       f"remove `token` from the profile and run: databricks auth login --host {values.get('host', '<host>')} --profile {profile}")
    if not values.get("host"):
        raise CliError(f"profile {profile} has no host", f"databricks auth login --host https://<workspace-host> --profile {profile}")
    return values["host"]


def check_token(profile: str, config_file: Optional[str]) -> str:
    try:
        payload = json.loads(run_cli(["auth", "token", profile, "-o", "json"], None, config_file))
    except CliError as exc:
        raise CliError(str(exc), f"databricks auth login --profile {profile}") from exc
    if not payload.get("access_token"):
        raise CliError("the CLI returned no access token", f"databricks auth login --profile {profile}")
    return f"OAuth token minted (expires {payload.get('expiry', 'unknown')})"


def check_me(profile: str, config_file: Optional[str]) -> str:
    me = json.loads(run_cli(["current-user", "me", "-o", "json"], profile, config_file))
    return f"signed in as {me.get('userName', 'unknown')}"


def check_warehouse(warehouse_id: str, profile: str, config_file: Optional[str]) -> str:
    try:
        wh = json.loads(run_cli(["warehouses", "get", warehouse_id, "-o", "json"], profile, config_file))
    except CliError as exc:
        raise CliError(str(exc), "check the warehouse id (databricks warehouses list) and your CAN USE permission on it") from exc
    return f"{wh.get('name', warehouse_id)} ({wh.get('warehouse_type', '?')}, {wh.get('state', '?')})"


def check_table(table: str, warehouse_id: str, profile: str, config_file: Optional[str]) -> str:
    try:
        execute(f"SELECT 1 AS ok FROM {table} LIMIT 1", warehouse_id, profile, config_file)
    except CliError as exc:
        raise CliError(f"{table}: {exc}",
                       f"ask an account admin to enable the system schema and grant USE SCHEMA/SELECT on {table.rsplit('.', 1)[0]}") from exc
    return "readable"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Databricks CLI preflight for the golden miner (read-only)")
    ap.add_argument("--profile", default=os.getenv("DATABRICKS_CONFIG_PROFILE", "DEFAULT"))
    ap.add_argument("--warehouse-id", required=True, help="SQL warehouse that runs the collector SQL")
    ap.add_argument("--config-file")
    ap.add_argument("--min-cli-version", default=MIN_CLI_VERSION)
    ap.add_argument("--skip-table-probes", action="store_true", help="skip step 6 (no warehouse start)")
    a = ap.parse_args(argv)
    steps: List[Tuple[str, Callable[[], str]]] = [
        ("databricks CLI", lambda: check_cli(a.min_cli_version)),
        ("OAuth profile", lambda: check_profile(a.profile, a.config_file)),
        ("OAuth token", lambda: check_token(a.profile, a.config_file)),
        ("identity", lambda: check_me(a.profile, a.config_file)),
        ("execution warehouse", lambda: check_warehouse(a.warehouse_id, a.profile, a.config_file)),
    ]
    if not a.skip_table_probes:
        steps += [(t, (lambda t=t: check_table(t, a.warehouse_id, a.profile, a.config_file))) for t in SYSTEM_TABLES]
    for name, step in steps:
        try:
            print(f"PASS {name}: {step()}")
        except CliError as exc:
            print(f"FAIL {name}: {exc}")
            if exc.remedy:
                print(f"     fix: {exc.remedy}")
            return 1
    print("preflight: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
