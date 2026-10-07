"""Backtest collection: past auto-stop changes read from system tables, kept as evidence, reported.

``collect_backtest`` mirrors ``collect_measurement`` for a fleet: pass 1 reads every selected warehouse's history through
the bridge (configuration epochs, billed daily cost, settled billing day), the pure planner
(``realizer.analyzer.backtest``) finds the past changes and their windows, pass 2 reads those windows in rounds (one
epoch and one pair of guard periods per warehouse in each statement). The result is backtest evidence
(``realizer.contracts.backtest``), validated and written before the report is computed from it, so ``--from-evidence``
reproduces a live result offline. ``backtest_demo`` runs the same path on a simulated fleet of past changes.

Files: ``<output-dir>/realization/backtest/evidence-<as of>.json`` and ``backtest-<as of>.json``. Logs carry counts and
labels, never SQL text, owners, tags or values.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.backtest import backtest_report, collection_rounds, plan_backtest
from realizer.analyzer.collector import bridge
from realizer.common.errors import InputError
from realizer.common.logging import LOGGER
from realizer.common.utils import utc_iso, write_json
from realizer.config.loader import read_json_input
from realizer.config.settings import DEFAULT_AWS_PRICE_REGISTRY, SUPPORTED_WAREHOUSE_TYPES
from realizer.contracts.backtest import BACKTEST_EVIDENCE_FORMAT_VERSION, backtest_evidence_schema

_SELECTOR = re.compile(r"^(?:([A-Za-z0-9_-]+)/)?([A-Za-z0-9_-]+)$")

# The backtest demo: demo-fleet warehouses whose auto-stop changed in the past, at a fixed analysis time. Lowered
# (Pro, Pro Large, Serverless), raised, a never-stop warehouse given a value, and a size change made together with the
# auto-stop change (not measured: it cannot isolate auto-stop).
DEMO_ANALYSIS_TIME = "2026-10-01T12:00:00+00:00"
DEMO_REGION = "us-east-1"
DEMO_CHANGES: Tuple[Dict[str, Any], ...] = (
    {"scenario": "C01", "after": {"auto_stop": 35}, "change_day": "2026-07-01", "change_minute": 8 * 60 + 45},
    {"scenario": "C02", "after": {"auto_stop": 5}, "change_day": "2026-08-20", "change_minute": 16 * 60 + 5},
    {"scenario": "C05", "after": {"auto_stop": 10}, "change_day": "2026-07-15", "change_minute": 9 * 60 + 10},
    {"scenario": "C13", "after": {"auto_stop": 10}, "change_day": "2026-08-01", "change_minute": 14 * 60 + 32},
    {"scenario": "C39", "after": {"size": "X_SMALL", "auto_stop": 15}, "change_day": "2026-08-12", "change_minute": 10 * 60},
    {"scenario": "C49", "after": {"auto_stop": 20}, "change_day": "2026-06-10", "change_minute": 11 * 60},
)


def parse_selectors(values: Sequence[str]) -> List[Tuple[Optional[str], str]]:
    """``--warehouse`` values (``<warehouse_id>`` or ``<workspace_id>/<warehouse_id>``) as ``(workspace or None,
    warehouse)``; a malformed one is refused without echoing it."""
    out = []
    for v in values:
        m = _SELECTOR.fullmatch(str(v).strip())
        if m is None:
            raise InputError("--warehouse must be <warehouse_id> or <workspace_id>/<warehouse_id>")
        out.append((m.group(1), m.group(2)))
    return sorted(set(out), key=lambda x: (x[0] or "", x[1]))


def validate_backtest_evidence(evidence: Any) -> None:
    """Accept *evidence* only in backtest evidence format ``BACKTEST_EVIDENCE_FORMAT_VERSION`` (its JSON Schema); raise
    ``InputError`` naming the failing location, never a value."""
    import jsonschema  # a gate that cannot run must not pass; jsonschema is a declared dependency

    if not isinstance(evidence, Mapping):
        raise InputError("backtest evidence must be a JSON object")
    version = evidence.get("backtest_evidence_format_version")
    if version != BACKTEST_EVIDENCE_FORMAT_VERSION:
        raise InputError(f"unsupported backtest evidence format (this Realizer reads {BACKTEST_EVIDENCE_FORMAT_VERSION})")
    validator = jsonschema.Draft202012Validator(backtest_evidence_schema(), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    error = next(iter(sorted(validator.iter_errors(dict(evidence)), key=lambda e: list(e.absolute_path))), None)
    if error is not None:
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise InputError(f"backtest evidence fails format {BACKTEST_EVIDENCE_FORMAT_VERSION} at {location} ({error.validator})")


def collect_backtest(client: Any, *, as_of: str, source: str, history_first_day: str, selectors: Sequence[Tuple[Optional[str], str]] = (),
                     execution_warehouse_id: Optional[str] = None, source_region: Optional[str] = None,
                     aws_registry: Optional[Mapping[str, Any]] = None, ingestion_cutoff: Optional[str] = None) -> Dict[str, Any]:
    """Backtest evidence at *as_of*, read through *client* (live system tables, or a simulated fleet): pass 1 for the
    selected warehouses, the plan, pass 2 in rounds. Only warehouses with an auto-stop change are kept. Validated
    against the backtest evidence schema before it is returned."""
    as_of = utc_iso(as_of)
    assumptions = bridge.assumptions(None)
    fleet = bridge.collect_fleet_history(client, as_of=as_of, settle_hours=assumptions["billing_settle_hours"],
                                         ingestion_cutoff=ingestion_cutoff, selectors=selectors)
    evidence: Dict[str, Any] = {
        "backtest_evidence_format_version": BACKTEST_EVIDENCE_FORMAT_VERSION, "source": source, "as_of_utc": as_of,
        "history_first_day": history_first_day, "source_region": source_region,
        "selectors": [f"{ws}/{wh}" if ws else wh for ws, wh in selectors], "execution_warehouse_id": execution_warehouse_id,
        "warehouses_read": len(fleet), "warehouses": [], "assumptions": assumptions,
    }
    evidence["warehouses"] = [{**w, "changes": []} for w in fleet]
    changes = plan_backtest(evidence, supported_types=SUPPORTED_WAREHOUSE_TYPES)
    keep = {(c.workspace_id, c.warehouse_id) for c in changes}
    evidence["warehouses"] = [w for w in evidence["warehouses"] if (w["workspace_id"], w["warehouse_id"]) in keep]
    by_warehouse = {(w["workspace_id"], w["warehouse_id"]): w for w in evidence["warehouses"]}
    rounds = collection_rounds(changes)
    for round_changes in rounds:
        asked = [c.windows() for c in round_changes]
        pre = bridge.collect_epoch_windows(client, [dict(w["pre"], workspace_id=c.workspace_id, warehouse_id=c.warehouse_id)
                                                    for c, w in zip(round_changes, asked) if w["pre"]], as_of=as_of)
        post = bridge.collect_epoch_windows(client, [dict(w["post"], workspace_id=c.workspace_id, warehouse_id=c.warehouse_id)
                                                     for c, w in zip(round_changes, asked)], as_of=as_of)
        guards = bridge.collect_guard_periods(client, [{"workspace_id": c.workspace_id, "warehouse_id": c.warehouse_id, "epoch_start": c.change_start,
                                                        "previous": [c.pre["first_day"], c.pre["last_day"]], "current": [c.post["first_day"], c.post["last_day"]]}
                                                       for c in round_changes if c.pre is not None])
        for c in round_changes:
            key = (c.workspace_id, c.warehouse_id)
            config = c.change_epoch
            by_warehouse[key]["changes"].append({
                "change_epoch_start": c.change_start,
                "windows": {"pre": pre.get((*key, "pre")), "post": post.get((*key, "post"))},
                "guard": guards.get(key),
                "aws": bridge.aws_price(config["warehouse_type"], config["warehouse_size"], source_region, aws_registry),
            })
    validate_backtest_evidence(evidence)
    LOGGER.info("backtest.collected warehouses_read=%d warehouses=%d changes=%d measured=%d rounds=%d", evidence["warehouses_read"],
                len(evidence["warehouses"]), len(changes), sum(1 for c in changes if c.post is not None), len(rounds))
    return evidence


def load_backtest_evidence(path: str | Path) -> Dict[str, Any]:
    """The backtest evidence file at *path*, checked against its format (``InputError`` otherwise)."""
    evidence = read_json_input(path, "backtest evidence")
    validate_backtest_evidence(evidence)
    return evidence


def backtest_paths(output_dir: Path, as_of: str) -> Tuple[Path, Path]:
    """``(evidence, report)`` files of a backtest at *as_of* under ``<output-dir>/realization/backtest/``."""
    stamp = utc_iso(as_of).replace("-", "").replace(":", "")
    base = Path(output_dir) / "realization" / "backtest"
    return base / f"evidence-{stamp}.json", base / f"backtest-{stamp}.json"


def _report(evidence: Mapping[str, Any], output_dir: Path) -> Tuple[Dict[str, Any], Path]:
    report = backtest_report(evidence, supported_types=SUPPORTED_WAREHOUSE_TYPES)
    path = backtest_paths(output_dir, evidence["as_of_utc"])[1]
    write_json(path, report)
    LOGGER.info("backtest.reported changes=%d skipped=%d", len(report["changes"]), len(report["skipped"]))
    return report, path


def _write_and_report(evidence: Mapping[str, Any], output_dir: Path) -> Tuple[Dict[str, Any], Path]:
    write_json(backtest_paths(output_dir, evidence["as_of_utc"])[0], evidence)
    return _report(evidence, output_dir)


def backtest_live(output_dir: Path, *, profile: str, sql_warehouse_id: str, warehouses: Sequence[str] = (), config_file: Optional[str] = None,
                  source_region: Optional[str] = None, aws_price_registry: Optional[str] = None, as_of: Optional[str] = None,
                  ingestion_cutoff: Optional[str] = None) -> Tuple[Dict[str, Any], Path]:
    """Backtest the past auto-stop changes of the selected warehouses (all by default) from system tables (read-only
    OAuth, on another SQL warehouse). The collector warehouse is refused when a selector names it, before any
    credential is read, and is not measured otherwise. The evidence is written first, then the report; returns
    ``(report, report path)``."""
    selectors = parse_selectors(warehouses)
    registry = bridge.load_registry(aws_price_registry or str(DEFAULT_AWS_PRICE_REGISTRY))
    auth = bridge.resolve_live_access(profile, sql_warehouse_id, [wh for _, wh in selectors], config_file)
    now = dt.datetime.now(dt.timezone.utc)
    as_of = utc_iso(as_of or now)
    with bridge.LiveSqlClient(auth) as client:
        evidence = collect_backtest(client, as_of=as_of, source="LIVE_SYSTEM_TABLES", history_first_day=bridge.history_first_day(now.date()),
                                    selectors=selectors, execution_warehouse_id=str(sql_warehouse_id), source_region=source_region,
                                    aws_registry=registry, ingestion_cutoff=ingestion_cutoff)
    return _write_and_report(evidence, output_dir)


def backtest_from_evidence(path: str | Path, output_dir: Path) -> Tuple[Dict[str, Any], Path]:
    """The report a backtest evidence file states, written under *output_dir*; returns ``(report, report path)``."""
    return _report(load_backtest_evidence(path), output_dir)


def backtest_demo(output_dir: Path) -> Tuple[Dict[str, Any], Path]:
    """The backtest of a deterministic simulated fleet of past changes (``DEMO_CHANGES`` at ``DEMO_ANALYSIS_TIME``, AWS
    modeled for ``DEMO_REGION``) through the live path; evidence written first. Never a measurement of a real warehouse."""
    client = bridge.demo_fleet_client(DEMO_CHANGES, DEMO_ANALYSIS_TIME)
    evidence = collect_backtest(client, as_of=DEMO_ANALYSIS_TIME, source="DEMO_SIMULATION",
                                history_first_day=bridge.history_first_day(dt.date.fromisoformat(DEMO_ANALYSIS_TIME[:10])),
                                source_region=DEMO_REGION, aws_registry=bridge.load_registry(str(DEFAULT_AWS_PRICE_REGISTRY)))
    return _write_and_report(evidence, output_dir)
