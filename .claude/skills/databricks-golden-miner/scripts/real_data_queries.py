#!/usr/bin/env python3
"""Run the read-only real-data-run queries and save their aggregates (Databricks CLI, Statement Execution API).

Usage:
  python .claude/skills/databricks-golden-miner/scripts/real_data_queries.py --profile <oauth-profile>
      --warehouse-id <id> --out mining_out/real_data_answers.json [--config-file <path>] [--list]

The SQL has one home, docs/proposals/publisher/real-data-run.md: every section whose heading names a tracker item, e.g.
"## SQL SKU names, tiers and prices (tracker G5)", contributes its ```sql block (several statements split on `;`).
The output is {"source": <doc path>, "answers": {<item>: [{"statement": <sql>, "columns": [...], "rows": [...]}]},
"errors": {<item>: [{"statement_number": <1-based>, "error_class": <exception class>, "message": <first line>}]}}.
A failed statement is recorded under `errors` (class and first message line only, never the SQL text), the remaining
statements still run, and the answers file is written with what succeeded. --list prints the items and statements
without running anything. Exit 0 when every statement succeeded, 1 when any failed (after writing the file).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_sql import CliError, execute  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
DOC = REPO / "docs" / "proposals" / "publisher" / "real-data-run.md"
_SECTION = re.compile(r"^## .*\(tracker (?P<item>[A-Z]\d+[a-z]?)\)\s*$", re.M)
_SQL = re.compile(r"```sql\n(?P<sql>.*?)```", re.S)


def statements(text: str) -> Dict[str, List[str]]:
    """Tracker item -> SELECT statements of its section, in document order."""
    out: Dict[str, List[str]] = {}
    heads = list(_SECTION.finditer(text))
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        body = body.split("\n## ", 1)[0]
        sqls = [s.strip() for m in _SQL.finditer(body) for s in m.group("sql").split(";") if s.strip()]
        if sqls:
            out[h.group("item")] = sqls
    return out


def main(argv: Optional[List[str]] = None, runner: Callable[..., Dict[str, Any]] = execute) -> int:
    ap = argparse.ArgumentParser(description="Run the real-data-run aggregate queries (read-only)")
    ap.add_argument("--profile")
    ap.add_argument("--warehouse-id")
    ap.add_argument("--config-file")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    items = statements(DOC.read_text(encoding="utf-8"))
    if a.list:
        for item, sqls in items.items():
            print(f"{item}: {len(sqls)} statement(s)")
        return 0
    if not (a.profile and a.warehouse_id and a.out):
        ap.error("--profile, --warehouse-id and --out are required unless --list")
    answers: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, List[Dict[str, Any]]] = {}
    for item, sqls in items.items():
        answers[item] = []
        for number, sql in enumerate(sqls, 1):
            try:
                res = runner(sql, a.warehouse_id, a.profile, a.config_file)
            except Exception as exc:  # noqa: BLE001 - any failure is recorded; the other answers must survive
                lines = str(exc).strip().splitlines()
                errors.setdefault(item, []).append({"statement_number": number, "error_class": type(exc).__name__,
                                                    "message": lines[0] if lines else ""})
                print(f"FAIL {item} (statement {number}): {type(exc).__name__}: {lines[0] if lines else ''}")
                continue
            answers[item].append({"statement": sql, **res})
            print(f"PASS {item}: {len(res['rows'])} row(s)")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"source": str(DOC.relative_to(REPO)), "answers": answers, "errors": errors}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"real-data answers: {a.out}" + (f" | {sum(len(v) for v in errors.values())} statement(s) failed, see `errors`" if errors else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
