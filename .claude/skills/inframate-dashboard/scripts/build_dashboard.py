#!/usr/bin/env python3
"""Build the inframate dashboard: one self-contained HTML page over Publisher and Realizer output.

The page is a projection. Every figure on it is read from a schema-validated RecommendationPackage (Reporter JSON
Contract) or a RealizationPackage (RPKG); the generator only filters, joins, sums within one currency, counts and ages.
It never estimates, prices or annualizes (the Publisher and the Realizer already did), and unknown stays unknown: a
null figure is shown as unavailable, never as 0.

Usage (from the repository root):
  python .claude/skills/inframate-dashboard/scripts/build_dashboard.py                    # real output -> <publisher-root>/dashboard/
  python .claude/skills/inframate-dashboard/scripts/build_dashboard.py --publisher-root ./warehouse_optimizer_out --realizer-root ./realizer_out
  python .claude/skills/inframate-dashboard/scripts/build_dashboard.py --demo             # rebuild the committed demo
  python .claude/skills/inframate-dashboard/scripts/build_dashboard.py --demo --verify    # ... and verify it
  python .claude/skills/inframate-dashboard/scripts/build_dashboard.py --check            # committed demo is current

Refreshing is rebuilding: each build rescans the roots, so new runs appear and the newest run is selected by default.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL.parents[2]
for _p in (ROOT / "src", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

GENERATOR_VERSION = "1.0.0"
TEMPLATE = SKILL / "assets" / "template.html"
VENDOR = SKILL / "assets" / "vendor" / "echarts.min.js"
ECHARTS_VERSION = "5.5.1"
ECHARTS_SHA256 = "e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf"
DEMO_OUT = ROOT / "docs" / "dashboard" / "shared" / "inframate-dashboard.html"
DEMO_RUNS = (  # (Publisher --demo-as-of, Realizer --as-of): each Realizer run lands before the next Publisher run
    ("2026-10-01T12:00:00Z", "2026-10-31T00:00:00Z"),
    ("2026-11-01T12:00:00Z", "2026-11-30T00:00:00Z"),
    ("2026-12-01T12:00:00Z", None),
)
SIZE_WARN, SIZE_FAIL = 4 * 1024 * 1024, 6 * 1024 * 1024
OPEN_STATUSES = ("ACTIONABLE", "TEST_REQUIRED", "REVIEW")
AGE_BUCKETS = ((30, "0-30"), (60, "31-60"), (90, "61-90"), (None, "90+"))
MARKERS = ("<!--@TITLE@-->", "<!--@FONTS@-->", "<!--@DATA@-->", "<!--@VENDOR@-->")
FONTS_DIR = SKILL / "assets" / "vendor" / "fonts"
FONTS = (  # (family, weight, file, sha256): IBM Plex (SIL Open Font License, LICENSE-ibm-plex.txt), embedded so the page works offline
    ("IBM Plex Sans", 400, "ibm-plex-sans-latin-400-normal.woff2", "db71f8a28ad8501544fb4e7668e3c6d0b731760b6f20de3525ebaeba597f1922"),
    ("IBM Plex Sans", 500, "ibm-plex-sans-latin-500-normal.woff2", "5ef914e59b0047a261844d96acabb60c34d3acab6b85ea24198726ce4781fd37"),
    ("IBM Plex Sans", 600, "ibm-plex-sans-latin-600-normal.woff2", "31535a91ce3f6b8ed3ddedadab1e49957e2220263a640df1a3f14f6fdfe15eb6"),
    ("IBM Plex Sans", 700, "ibm-plex-sans-latin-700-normal.woff2", "fff71a83690454ee6ea9014780a6797408918cb90cde1f0f3be65ea28a03c678"),
    ("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal.woff2", "3c5a451f9ec27a354b0c2bcca636c6ec17a651281aabf29f8427e210a1d31e85"),
    ("IBM Plex Mono", 500, "ibm-plex-mono-latin-500-normal.woff2", "756026ff72eb76fd971ac4b7504cec55eef62109d2684c2cad8da32170b80b37"),
)


# --------------------------------------------------------------------------- small helpers

def dec(v: Any) -> Optional[Decimal]:
    """A money or number value (JSON number or decimal string) as Decimal; None stays None."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def money(v: Optional[Decimal]) -> Optional[float]:
    return None if v is None else float(round(v, 2))


def sum_all(values: Sequence[Optional[Decimal]]) -> Optional[Decimal]:
    """The sum when every value is known, else None (the workbook rule: one unknown makes the total unknown)."""
    if not values or any(v is None for v in values):
        return None if values else Decimal(0)
    return sum(values, Decimal(0))


def sum_known(values: Iterable[Optional[Decimal]]) -> Tuple[Decimal, int]:
    known = [v for v in values if v is not None]
    return sum(known, Decimal(0)), len(known)


def canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def parse_time(text: Optional[str]) -> Optional[dt.datetime]:
    if not text:
        return None
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def days_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    ta, tb = parse_time(a), parse_time(b)
    return None if ta is None or tb is None else (tb - ta).days


def age_bucket(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    for limit, label in AGE_BUCKETS:
        if limit is None or days <= limit:
            return label
    return None


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


# --------------------------------------------------------------------------- discovery

def discover_publisher_runs(root: Path) -> Tuple[List[Path], List[Dict[str, str]]]:
    """Complete Publisher run folders under *root* (or *root* itself when it holds a flat run), oldest first by name.

    A run is complete only when ``manifest.json`` exists (it is written last) and holds every package it lists. Realizer
    run folders (their manifest has ``run_manifest_version``) are never read as Publisher runs."""
    from publisher.config.output_layout import RUN_DATE_PREFIX, RUN_TS_PREFIX
    candidates = [root] if (root / "manifest.json").is_file() else sorted(root.glob(f"{RUN_DATE_PREFIX}*/{RUN_TS_PREFIX}*"))
    runs, skipped = [], []
    for folder in candidates:
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file():
            skipped.append({"run": rel(folder, root), "reason": "incomplete: no manifest.json"})
            continue
        manifest = read_json(manifest_path)
        if "run_manifest_version" in manifest:
            continue  # a Realizer run folder sharing the root
        listed = manifest.get("packages") or []
        if any(not (folder / str(p.get("path", ""))).is_file() for p in listed):
            skipped.append({"run": rel(folder, root), "reason": "incomplete: a listed package is missing"})
            continue
        runs.append(folder)
    return runs, skipped


def load_publisher_run(folder: Path, root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """The run's manifest, validated packages and (optional) evidence; or a skip record naming why it was not read."""
    from realizer.config.loader import validate_package
    manifest = read_json(folder / "manifest.json")
    packages = []
    for entry in sorted(manifest.get("packages") or [], key=lambda p: str(p.get("path"))):
        package = read_json(folder / str(entry["path"]))
        try:
            validate_package(package)
        except Exception as exc:  # an unsupported contract version, a schema failure, an edited package
            return None, {"run": rel(folder, root), "reason": f"package {entry.get('warehouse_id')}: {str(exc)[:160]}"}
        packages.append(package)
    evidence = None
    evidence_path = folder / str(manifest.get("evidence_file") or "evidence.json")
    if evidence_path.is_file():
        try:
            evidence = read_json(evidence_path)
        except ValueError:
            evidence = None
    generated = manifest.get("generated_at") or max((p["provenance"]["generated_at"] for p in packages), default=None)
    return {"folder": folder, "manifest": manifest, "packages": packages, "evidence": evidence, "generated_at": generated}, None


def realizer_layout(root: Path) -> Tuple[Optional[Path], List[Path]]:
    """(state ``realization`` folder, RPKG snapshot files) of a Realizer output root, partitioned or flat."""
    if not root.exists():
        return None, []
    state = root / "state" / "realization" if (root / "state" / "realization").is_dir() else (root / "realization" if (root / "realization" / "ledger").is_dir() else None)
    files = sorted(root.glob("run_date=*/run_ts=*/realization/packages/RPKG-*.json"))
    files += sorted((root / "realization" / "packages").glob("RPKG-*.json")) if (root / "realization" / "packages").is_dir() else []
    return state, files


def load_rpkgs(files: Sequence[Path]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Every schema-valid RPKG snapshot, de-duplicated by (RPKG id, as-of, snapshot id)."""
    import jsonschema
    schema = read_json(ROOT / "schema" / "realization-package.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    seen, out, skipped = set(), [], []
    for f in files:
        doc = read_json(f)
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        if errors:
            skipped.append({"run": f.name, "reason": "RPKG fails the realization schema at " + "/".join(map(str, errors[0].absolute_path))})
            continue
        key = (doc["realization_package_id"], doc.get("as_of_utc"), doc.get("snapshot_id"))
        if key not in seen:
            seen.add(key); out.append(doc)
    return out, skipped


def load_ledgers(state: Optional[Path]) -> Dict[str, List[Dict[str, Any]]]:
    if state is None or not (state / "ledger").is_dir():
        return {}
    ledgers = {}
    for f in sorted((state / "ledger").glob("RPKG-*.jsonl")):
        ledgers[f.stem] = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ledgers


# --------------------------------------------------------------------------- projection

def wh_key(ws: str, wh: str) -> str:
    return f"{ws}/{wh}"


def recommended_scenario(p: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    sid = p["recommended_plan"]["scenario_id"]
    return next((s for s in p["scenarios"] if s["scenario_id"] == sid), None)


def plan_range(p: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The recommended scenario's savings (workbook rule: not BLOCKED or INSUFFICIENT_EVIDENCE, and priced)."""
    s = recommended_scenario(p)
    if s is None or s["status"] in ("BLOCKED", "INSUFFICIENT_EVIDENCE"):
        return None
    savings = s["portfolio_economics"]["savings_365d"]
    return savings if savings and savings.get("total") is not None else None


def experiment_high(p: Mapping[str, Any]) -> Optional[Decimal]:
    highs = [dec(s["portfolio_economics"]["savings_365d"]["total"]["high"]) for s in p["scenarios"]
             if s["scenario_id"] != p["recommended_plan"]["scenario_id"] and s["scenario_type"] == "WAREHOUSE_TYPE_MIGRATION"
             and s["status"] not in ("BLOCKED", "INSUFFICIENT_EVIDENCE") and (s.get("compatibility") or {}).get("status") != "INCOMPATIBLE"
             and s["portfolio_economics"]["savings_365d"] and s["portfolio_economics"]["savings_365d"].get("total") is not None]
    return max(highs) if highs else None


def lineage_applies(lineage: Mapping[str, Any], knob: str, value: Any) -> bool:
    app = lineage.get("application") or {}
    return app.get("state") == "APPLIED" and app.get("knob") == knob and canonical(app.get("observed_applied_value")) == canonical(value)


CONFIG_KNOBS = ("warehouse_type", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes", "warehouse_channel")


def config_history(evidence_wh: Optional[Mapping[str, Any]], package: Mapping[str, Any], lineages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The warehouse's configuration periods in the trailing year, oldest first, each with what changed at its start and
    what was observed during it (billed cost, workload, cluster behaviour), as the evidence states it.

    A change is linked to a recommendation when a Realizer lineage observed that knob applied at that value. Without
    replay evidence the history is the package's single ``observed_change`` (no period figures)."""
    def linked(changes):
        for c in changes:
            for l in lineages:
                app = l.get("application") or {}
                if app.get("knob") == c["knob"] and canonical(app.get("observed_applied_value")) == canonical(c["to"]):
                    return {"rpkg": l["realization_package_id"], "state": l["current_state"]["realization_state"],
                            "claim": (l.get("attribution") or {}).get("attribution_claim_status"),
                            "annualized": money(dec((l.get("economics") or {}).get("annualized_realized_savings")))}
        return None

    if not evidence_wh or not evidence_wh.get("epochs"):
        oc = package.get("observed_change")
        if not oc:
            return {"source": "none", "periods": []}
        changes = [{"knob": c["knob"], "from": c.get("previous_value"), "to": c.get("current_value")} for c in oc.get("changes") or []]
        return {"source": "package", "periods": [{"start": oc.get("changed_at"), "end": None, "current": True, "changes": changes, "link": linked(changes)}]}
    analysis = str(evidence_wh.get("analysis_time"))
    epochs = sorted(evidence_wh["epochs"], key=lambda e: str(e.get("epoch_start")))
    periods, previous = [], None
    for e in epochs:
        cfg = {k: (e.get("config") or {}).get(k) for k in CONFIG_KNOBS}
        changes = [] if previous is None else [{"knob": k, "from": previous[k], "to": cfg[k]} for k in CONFIG_KNOBS if canonical(previous[k]) != canonical(cfg[k])]
        cost, q, ev = e.get("cost") or {}, e.get("query") or {}, e.get("events") or {}
        end = e.get("epoch_end")
        periods.append({
            "start": e.get("epoch_start"), "end": end, "current": bool(e.get("is_current")), "config": cfg, "changes": changes,
            "days": days_between(str(e.get("epoch_start")), str(end or analysis)),
            "billed_days": cost.get("cost_days"), "active_days": cost.get("active_cost_days"),
            "list_cost": None if dec(cost.get("unpriced_dbu_quantity")) else money(dec(cost.get("list_cost"))),
            "dbu": None if cost.get("dbu_quantity") is None else round(float(cost["dbu_quantity"]), 1),
            "queries": q.get("epoch_queries"), "p95_ms": q.get("epoch_p95_execution_ms"),
            "queued_pct": q.get("epoch_queued_query_pct"), "spill_pct": q.get("epoch_spill_query_pct"),
            "peak_clusters": ev.get("max_observed_clusters"), "uptime_minutes": ev.get("uptime_minutes"),
            "link": linked(changes) if changes else None,
        })
        previous = cfg
    return {"source": "evidence", "periods": periods}


def trailing_days(evidence_wh: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Billed calendar days of the trailing year in replay evidence (None without evidence)."""
    if not evidence_wh:
        return None
    days = sorted({str(r.get("usage_date"))[:10] for r in evidence_wh.get("daily_cost_365") or [] if r.get("usage_date")})
    if not days:
        return 0
    return days_between(days[0] + "T00:00:00Z", str(evidence_wh.get("analysis_time"))) or 0


def project_realization(r: Mapping[str, Any]) -> Dict[str, Any]:
    from realizer.contracts.report import NEXT_ACTIONS
    e, a, v, att = r.get("economics") or {}, r.get("application") or {}, r.get("validation") or {}, r.get("attribution") or {}
    state = r["current_state"]["realization_state"]
    return {
        "rpkg": r["realization_package_id"], "pkg": r["source_recommendation"]["recommendation_package_id"],
        "generated_at": r["source_recommendation"].get("generated_at_utc"), "as_of": r.get("as_of_utc"),
        "disposition": r["source_recommendation"].get("disposition"), "state": state,
        "validation_state": r["current_state"].get("validation_state"), "maturity": r["current_state"].get("maturity_state"),
        "claim": att.get("attribution_claim_status"), "attribution_state": att.get("attribution_state"),
        "disqualifiers": att.get("hard_disqualifiers") or [], "origin": att.get("origin_attribution"),
        "knob": a.get("knob"), "step": a.get("step"), "step_role": a.get("step_role"), "source_value": a.get("source_value"),
        "recommended_value": a.get("recommended_value"), "observed_value": a.get("observed_applied_value"),
        "application_state": a.get("state"), "conformance": a.get("implementation_conformance"),
        "effective_from": a.get("effective_from_utc"), "effective_to": a.get("effective_to_utc"),
        "plan_progress": a.get("plan_progress"),
        "currency": e.get("currency"), "available": e.get("available"),
        "annualized": money(dec(e.get("annualized_realized_savings"))),
        "to_date": money(dec(e.get("realized_savings_since_application"))),
        "attributed": money(dec(att.get("recommendation_attributed_realized_savings"))),
        "observed_only": money(dec(att.get("validated_observed_realization"))),
        "dbu_realized": money(dec(e.get("dbu_realized"))), "aws_realized": money(dec(e.get("aws_economic_realized"))),
        "expected": {k: money(dec((e.get("expected_range") or {}).get(k))) for k in ("low", "base", "high")} if e.get("expected_range") else None,
        "ratio": None if dec(e.get("realization_ratio")) is None else float(round(dec(e.get("realization_ratio")), 4)),
        "classification": e.get("classification_detail"), "review_required": e.get("review_required"),
        "realized_to_date": e.get("realized_to_date"),
        "measurement": {k: (e.get("measurement") or {}).get(k) for k in ("method", "pre_window", "post_window", "cross_check", "volume_ratio")} if e.get("measurement") else None,
        "cost_change": e.get("cost_change"), "performance": e.get("performance"),
        "window": {"start": v.get("window_start_utc"), "end": v.get("window_end_utc")},
        "guard_pass": v.get("hard_guardrails_pass"),
        "rules": [{k: x.get(k) for k in ("rule_id", "metric", "observed", "previous", "threshold", "operator", "result")} for x in v.get("rules") or []],
        "next_action": NEXT_ACTIONS.get((state, None), ""),
    }


EVIDENCE_FIELDS = ("metric", "value", "unit", "source", "source_field", "observation_window", "epoch_scope", "threshold", "comparator", "interpretation")
INTERNED = {"metric", "unit", "source", "source_field", "observation_window", "epoch_scope", "comparator", "interpretation"}


def _never_applied_and_superseded(r: Mapping[str, Any]) -> bool:
    """A lineage a later package replaced before anything was applied: counted, not shown."""
    return r["current_state"]["realization_state"] == "NOT_APPLIED" and r["source_recommendation"].get("disposition") == "SUPERSEDED"


def evidence_record(rec: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: rec.get(k) for k in EVIDENCE_FIELDS}


LARGE_VALUE = 2000  # characters of JSON; a lean build summarizes larger evidence values (per-value tables)


def summarize(value: Any) -> Any:
    size = len(canonical(value))
    if not isinstance(value, (dict, list)) or size <= LARGE_VALUE:
        return value
    return {"_summary": f"{'table' if isinstance(value, dict) else 'list'} of {len(value)} entries ({size:,} characters); full value in the package JSON"}


def compact_evidence(details: Mapping[str, Any], lean: bool) -> List[str]:
    """Evidence records as rows in EVIDENCE_FIELDS order, their repeated text fields replaced by indexes into one string
    table (the page decodes them); returns the table. A lean build also summarizes large structured values."""
    table: Dict[str, int] = {}

    def intern(v):
        if v is None:
            return None
        if v not in table:
            table[v] = len(table)
        return table[v]

    for d in details.values():
        for w in d["warehouses"]:
            refs = sorted(w["evidence"])
            index = {ref: i for i, ref in enumerate(refs)}
            w["evidence"] = [[ref] + [intern(w["evidence"][ref][f]) if f in INTERNED and isinstance(w["evidence"][ref][f], str)
                                      else (summarize(w["evidence"][ref][f]) if lean and f == "value" else w["evidence"][ref][f]) for f in EVIDENCE_FIELDS]
                             for ref in refs]
            for c in w["candidates"]:
                c["evidence"] = [index[ref] for ref in c["evidence"] if ref in index]
    return sorted(table, key=table.get)


def project_warehouse(p: Mapping[str, Any], lineages: Sequence[Mapping[str, Any]], evidence_wh: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    w, c, cb = p["warehouse"], p["current_config"], p["cost_baseline"]
    rec = recommended_scenario(p)
    plan = plan_range(p)
    env = p["package_economics"].get("estimated_savings_365d")
    candidates = {x["candidate_id"]: x for x in p["candidates"]}
    used_evidence = sorted({ref for x in p["candidates"] for ref in x.get("evidence_refs") or []})

    def cost(b):
        return {"dbu": money(dec(b["dbu_cost"])), "aws": money(dec(b["aws_cost"])), "total": money(dec(b["total_cost"])),
                "availability": b["availability"], "dbu_basis": b["dbu_basis"], "aws_basis": b["aws_basis"]}

    def srange(r):
        return None if r is None else {k: money(dec(r[k])) for k in ("low", "base", "high")}

    def project_steps(plan):
        out = []
        for st in plan or []:
            cand = candidates.get(st.get("candidate_id") or "")
            proj = st.get("projected_economics") or {}
            step_sav = ((proj.get("step_savings_365d") or {}).get("total") or {}) if proj.get("step_savings_365d") else {}
            applied = next((l for l in lineages if st.get("knob") and lineage_applies(l, st["knob"], st.get("recommended_value"))), None)
            out.append({
                "step": st["step"], "type": st["step_type"], "knob": st.get("knob"), "action": st.get("action"),
                "technique": st.get("technique_id"), "candidate": st.get("candidate_id"),
                "current": st.get("current_value"), "target": st.get("recommended_value"),
                "state": st.get("execution_state"), "candidate_status": cand["status"] if cand else None,
                "validation": {k: (st.get("validation") or {}).get(k) for k in ("validation_type", "success_metrics", "remeasure_days", "action_after")},
                "savings_base": money(dec(step_sav.get("base"))) if step_sav else None,
                "applied": applied["current_state"]["realization_state"] if applied else None,
            })
        return out

    scenarios = []
    for s in p["scenarios"]:
        sv = s["portfolio_economics"]["savings_365d"]
        scenarios.append({
            "id": s["scenario_id"], "name": s["scenario_name"], "type": s["scenario_type"], "status": s["status"],
            "recommended": s["scenario_id"] == p["recommended_plan"]["scenario_id"],
            "confidence": (s.get("confidence") or {}).get("level"), "confidence_score": (s.get("confidence") or {}).get("score"),
            "savings": srange(sv["total"]) if sv and sv.get("total") else None,
            "dbu": srange(sv["dbu"]) if sv and sv.get("dbu") else None, "aws": srange(sv["aws"]) if sv and sv.get("aws") else None,
            "compatibility": (s.get("compatibility") or {}).get("status"),
            "method": s["portfolio_economics"].get("method"),
            "steps": project_steps(s.get("execution_plan")),
        })
    steps = project_steps((rec or {}).get("execution_plan"))
    return {
        "key": wh_key(w["workspace_id"], w["warehouse_id"]), "id": w["warehouse_id"], "workspace": w["workspace_id"],
        "name": w["warehouse_name"], "owner": w.get("owner"), "region": w.get("region"),
        "epoch": {"id": w["current_epoch"]["epoch_id"], "start": w["current_epoch"]["start_time"], "age_days": w["current_epoch"].get("age_days")},
        "config": {k: c.get(k) for k in ("warehouse_type", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes", "warehouse_channel")},
        "photon": {k: (c.get("photon") or {}).get(k) for k in ("status", "enabled_usage_pct")},
        "observed_change": p.get("observed_change"),
        "cost_profile": {k: (p.get("cost_profile") or {}).get(k) for k in ("name", "source", "source_detail")},
        "applied_thresholds": (p.get("cost_profile") or {}).get("applied_thresholds"),
        "currency": cb["currency"], "actual": cost(cb["actual_365d"]), "forward": cost(cb["forward_365d"]),
        "trailing_days": trailing_days(evidence_wh),
        "history": config_history(evidence_wh, p, lineages),
        "forecast": {"model": (cb.get("forecast") or {}).get("model"), "confidence": ((cb.get("forecast") or {}).get("confidence") or {}).get("level")},
        "plan": None if plan is None else {"total": srange(plan["total"]), "dbu": srange(plan.get("dbu")) if plan.get("dbu") else None,
                                           "aws": srange(plan.get("aws")) if plan.get("aws") else None},
        "envelope": srange(env),
        "migration_high": money(experiment_high(p)),
        "assessment": p["assessment"]["overall_status"],
        "evidence_quality": {k: p["assessment"].get("evidence_quality", {}).get(k) for k in ("status", "confidence", "source_coverage_pct")},
        "recommended": None if rec is None else {
            "id": rec["scenario_id"], "name": rec["scenario_name"], "type": rec["scenario_type"], "status": rec["status"],
            "confidence": (rec.get("confidence") or {}).get("level"), "confidence_score": (rec.get("confidence") or {}).get("score"),
        },
        "decision": {k: p["recommended_plan"].get(k) for k in ("next_action", "selection_reason_code", "requires_hitl", "decision_summary")},
        "reevaluation": p["recommended_plan"].get("reevaluation"),
        "workload": p.get("workload_profile"),
        "scenarios": scenarios, "steps": steps,
        "candidates": [{
            "id": x["candidate_id"], "technique": x["technique_id"], "knob": x["knob"], "status": x["status"],
            "current": x.get("current_value"), "target": x.get("recommended_value"), "rationale": x.get("rationale"),
            "objectives": x.get("objectives"), "blockers": x.get("blockers") or [], "evidence": x.get("evidence_refs") or [],
            "confidence": (x.get("confidence") or {}).get("level"), "confidence_score": (x.get("confidence") or {}).get("score"),
            "savings": srange(((x.get("independent_economics") or {}).get("savings_365d") or {}).get("total")),
            "estimation_status": (x.get("independent_economics") or {}).get("estimation_status"),
        } for x in p["candidates"]],
        "evidence": {ref: evidence_record(p["evidence"][ref]) for ref in used_evidence if ref in p["evidence"]},
        "realization": [project_realization(l) for l in lineages if not _never_applied_and_superseded(l)],
        "superseded_unapplied": sum(1 for l in lineages if _never_applied_and_superseded(l)),
        "ids": {"package": p["recommendation_package_id"], "hash": p["provenance"].get("package_hash_full_sha256"),
                "fingerprint": p["provenance"].get("evidence_fingerprint"), "contract": p["contract_version"]},
        "versions": {k: v for k, v in sorted(p["provenance"].items()) if k.endswith("_version")},
        "run_scope": p.get("run_scope"),
    }


def run_kpis(warehouses: Sequence[Mapping[str, Any]], packages: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any],
             recs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Executive KPIs per currency (never summed across currencies), with the workbook's availability rules."""
    by_currency: Dict[str, List[Tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for w, p in zip(warehouses, packages):
        by_currency.setdefault(w["currency"], []).append((w, p))
    out: Dict[str, Any] = {}
    for currency, pairs in sorted(by_currency.items()):
        ws = [w for w, _ in pairs]
        actual = {k: sum_all([dec(w["actual"][k]) for w in ws]) for k in ("total", "dbu", "aws")}
        known_total, known_n = sum_known(dec(w["actual"]["total"]) for w in ws)
        forward = sum_all([dec(w["forward"]["total"]) for w in ws])
        plans = [w["plan"] for w in ws if w["plan"] is not None]
        plan = {b: sum((dec(x["total"][b]) for x in plans), Decimal(0)) for b in ("low", "base", "high")}
        plan_dbu = sum((dec(x["dbu"]["base"]) or Decimal(0) for x in plans if x.get("dbu")), Decimal(0))
        plan_aws = sum((dec(x["aws"]["base"]) or Decimal(0) for x in plans if x.get("aws")), Decimal(0))
        bases = [dec(p["package_economics"]["estimated_savings_365d"]["base"]) for _, p in pairs if p["package_economics"].get("estimated_savings_365d")]
        gross = sum((max(b, Decimal(0)) for b in bases), Decimal(0))
        increase = sum((max(-b, Decimal(0)) for b in bases), Decimal(0))
        envelopes = [w["envelope"] for w in ws]
        env_low = None if any(e is None for e in envelopes) else sum((dec(e["low"]) for e in envelopes), Decimal(0))
        env_high = None if any(e is None for e in envelopes) else sum((dec(e["high"]) for e in envelopes), Decimal(0))
        migration = sum((max(dec(w["migration_high"]), Decimal(0)) for w in ws if w["migration_high"] is not None), Decimal(0))
        lineages = [l for w in ws for l in w["realization"] if l["currency"] in (None, currency)]
        claimable = [l for l in lineages if l["claim"] == "CLAIMABLE"]
        realized = sum((dec(l["annualized"]) for l in claimable if l["annualized"] is not None), Decimal(0))
        to_date = sum((dec(l["to_date"]) for l in claimable if l["to_date"] is not None), Decimal(0))
        observable = sum((dec(l["observed_only"]) for l in lineages if l["claim"] == "OBSERVABLE_ONLY" and l["observed_only"] is not None), Decimal(0))
        cur_recs = [r for r in recs if r["currency"] == currency]
        out[currency] = {
            "warehouses": len(ws), "workspaces": len({w["workspace"] for w in ws}),
            "actual": {k: money(v) for k, v in actual.items()},
            "actual_known": {"total": money(known_total), "count": known_n,
                             "dbu": money(sum_known(dec(w["actual"]["dbu"]) for w in ws)[0]), "aws": money(sum_known(dec(w["actual"]["aws"]) for w in ws)[0])},
            "short_history": sum(1 for w in ws if w["trailing_days"] is not None and w["trailing_days"] < 365),
            "history_unknown": sum(1 for w in ws if w["trailing_days"] is None),
            "forward": money(forward), "forward_known": {"total": money(sum_known(dec(w["forward"]["total"]) for w in ws)[0]),
                                                         "count": sum_known(dec(w["forward"]["total"]) for w in ws)[1]},
            "identified": {b: money(v) for b, v in plan.items()}, "identified_dbu": money(plan_dbu), "identified_aws": money(plan_aws),
            "identified_priced": len(plans),
            "gross_savings": money(gross), "cost_increase": money(increase), "net": money(gross - increase),
            "envelope": {"low": money(env_low), "high": money(env_high)}, "migration_upside": money(migration),
            "realized": money(realized), "realized_to_date": money(to_date), "observable_only": money(observable),
            "realized_lineages": len([l for l in claimable if l["annualized"] is not None]),
            "validating": sum(1 for l in lineages if l["state"] in ("APPLIED", "VALIDATING")),
            "open": sum(1 for r in cur_recs if r["status"] == "OPEN"), "blocked": sum(1 for r in cur_recs if r["status"] == "BLOCKED"),
            "open_value": money(sum((dec(r["savings_base"]) for r in cur_recs if r["status"] == "OPEN" and r["savings_base"] is not None), Decimal(0))),
            "aging": {label: sum(1 for r in cur_recs if r["status"] == "OPEN" and r["age_bucket"] == label) for _, label in AGE_BUCKETS},
            "confidence": {lvl: sum(1 for w in ws if (w["recommended"] or {}).get("confidence") == lvl) for lvl in ("HIGH", "MEDIUM", "LOW")},
            "profiles": {name: sum(1 for w in ws if w["cost_profile"]["name"] == name) for name in sorted({w["cost_profile"]["name"] for w in ws if w["cost_profile"]["name"]})},
        }
    return {"by_currency": out, "excluded": len(manifest.get("excluded_warehouses") or []),
            "excluded_list": manifest.get("excluded_warehouses") or [], "selection": manifest.get("warehouse_selection"),
            "run_scope": manifest.get("run_scope")}


def recommendations(warehouses: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One row per CANDIDATE step of each recommended plan, with its lifecycle status."""
    rows = []
    for w in warehouses:
        for st in w["steps"]:
            if st["type"] != "CANDIDATE" or not st["knob"]:
                continue
            applied_lineage = next((l for l in w["realization"] if l["knob"] == st["knob"] and l["application_state"] == "APPLIED"
                                    and canonical(l["observed_value"]) == canonical(st["target"])), None)
            if applied_lineage:
                status = {"REALIZED": "REALIZED", "UNDER_REALIZED": "UNDER_REALIZED", "VALIDATION_FAILED": "FAILED",
                          "MONITORING": "MONITORING"}.get(applied_lineage["state"], "VALIDATING")
            elif st["candidate_status"] in OPEN_STATUSES:
                status = "OPEN"
            elif st["candidate_status"] == "BLOCKED":
                status = "BLOCKED"
            else:
                continue
            rows.append({
                "warehouse": w["key"], "name": w["name"], "owner": w["owner"], "workspace": w["workspace"], "currency": w["currency"],
                "technique": st["technique"], "knob": st["knob"], "current": st["current"], "target": st["target"],
                "step": st["step"], "state": st["state"], "candidate_status": st["candidate_status"], "status": status,
                "savings_base": st["savings_base"], "aging_key": canonical([w["key"], st["technique"], st["target"]]),
                "lineage_state": applied_lineage["state"] if applied_lineage else None,
            })
    return rows


def apply_aging(runs: Sequence[Dict[str, Any]]) -> None:
    """Age each open recommendation from the first run of its unbroken streak (same warehouse, technique and target)."""
    streak_start: Dict[str, str] = {}
    for run in runs:
        current = {}
        for r in run["recs"]:
            if r["status"] in ("OPEN", "BLOCKED"):
                first = streak_start.get(r["aging_key"], run["generated_at"])
                current[r["aging_key"]] = first
                r["first_seen"] = first
                r["age_days"] = days_between(first, run["generated_at"])
                r["age_bucket"] = age_bucket(r["age_days"])
            else:
                r["first_seen"] = r["age_days"] = r["age_bucket"] = None
        streak_start = current


def insights(kpi: Mapping[str, Any], warehouses: Sequence[Mapping[str, Any]], currency: str) -> List[Dict[str, str]]:
    """Fixed-rule call-outs over this run's numbers; each states its own figures, none is a forecast."""
    out = []
    ws = [w for w in warehouses if w["currency"] == currency]
    bases = sorted(((dec(w["plan"]["total"]["base"]), w["name"]) for w in ws if w["plan"] and dec(w["plan"]["total"]["base"]) and dec(w["plan"]["total"]["base"]) > 0),
                   key=lambda x: (-x[0], x[1]))
    total = sum((b for b, _ in bases), Decimal(0))
    if len(bases) >= 4 and total > 0:
        share = sum((b for b, _ in bases[:3]), Decimal(0)) / total * 100
        out.append({"kind": "concentration", "text": f"Three warehouses hold {share:.0f}% of the identified savings (base): {', '.join(n for _, n in bases[:3])}."})
    if bases:
        out.append({"kind": "top", "text": f"The largest single opportunity is {bases[0][1]}: {currency} {bases[0][0]:,.0f} a year (base)."})
    old = kpi["aging"]["61-90"] + kpi["aging"]["90+"]
    if old:
        out.append({"kind": "aging", "text": f"{old} open recommendation{'s are' if old != 1 else ' is'} older than 60 days."})
    if kpi["realized_lineages"]:
        out.append({"kind": "realized", "text": f"{kpi['realized_lineages']} applied change{'s' if kpi['realized_lineages'] != 1 else ''} realize {currency} {kpi['realized']:,.0f} a year (claimable run-rate)."})
    missing = kpi["warehouses"] - kpi["actual_known"]["count"]
    if missing:
        out.append({"kind": "coverage", "text": f"{missing} of {kpi['warehouses']} warehouses lack a complete actual (DBU unpriced or AWS not modeled), so the fleet total is unavailable; the known subset is shown."})
    if kpi["cost_increase"]:
        adds = sum(1 for w in ws if w["envelope"] and dec(w["envelope"]["base"]) is not None and dec(w["envelope"]["base"]) < 0)
        out.append({"kind": "capacity", "text": f"{adds} recommended plan{'s add' if adds != 1 else ' adds'} capacity for performance: {currency} {kpi['cost_increase']:,.0f} a year of cost increase, netted out of the headline."})
    return out[:6]


def daily_series(evidence: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Per warehouse: daily DBU list cost (the only daily cost series in the evidence) and its config epochs."""
    out = {}
    for w in (evidence or {}).get("warehouses") or []:
        by_day: Dict[str, Decimal] = {}
        for r in w.get("daily_cost_365") or []:
            day = str(r.get("usage_date"))[:10]
            cost = dec(r.get("list_cost"))
            if cost is not None:
                by_day[day] = by_day.get(day, Decimal(0)) + cost
        days = sorted(by_day)
        epochs = [{"start": str(e.get("epoch_start"))[:10], "end": (str(e.get("epoch_end"))[:10] if e.get("epoch_end") else None),
                   "label": f"{(e.get('config') or {}).get('warehouse_type')} {(e.get('config') or {}).get('warehouse_size')} · auto-stop {(e.get('config') or {}).get('auto_stop_minutes')} · {(e.get('config') or {}).get('min_clusters')}–{(e.get('config') or {}).get('max_clusters')} clusters"}
                  for e in sorted(w.get("epochs") or [], key=lambda e: str(e.get("epoch_start")))]
        out[wh_key(str(w.get("workspace_id")), str(w.get("warehouse_id")))] = {
            "days": days, "cost": [float(round(by_day[d], 2)) for d in days], "epochs": epochs, "currency": w.get("currency")}
    return out


def blocker_meanings() -> Dict[str, str]:
    text = (ROOT / "docs" / "reference" / "publisher" / "blocker-codes.md").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"^\| `([A-Z0-9_]+)` \| ([^|]+) \|", text, re.M):
        out.setdefault(m.group(1), m.group(2).strip())
    return out


def lean_package(p: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in p.items() if k != "evidence"}


def build_dataset(publisher_root: Path, realizer_root: Optional[Path], *, detail_runs: int, raw: str, demo: bool) -> Dict[str, Any]:
    runs_found, skipped = discover_publisher_runs(publisher_root)
    loaded = []
    for folder in runs_found:
        run, skip = load_publisher_run(folder, publisher_root)
        if run is None:
            skipped.append(skip)
        else:
            loaded.append(run)
    loaded.sort(key=lambda r: (str(r["generated_at"]), rel(r["folder"], publisher_root)))
    state, rpkg_files = realizer_layout(realizer_root) if realizer_root else (None, [])
    rpkgs, rskipped = load_rpkgs(rpkg_files)
    skipped += rskipped
    ledgers = load_ledgers(state)
    blockers = blocker_meanings()

    runs = []
    for i, run in enumerate(loaded):
        cutoff = loaded[i + 1]["generated_at"] if i + 1 < len(loaded) else None
        latest: Dict[str, Mapping[str, Any]] = {}
        for r in rpkgs:
            if cutoff is not None and str(r.get("as_of_utc")) >= str(cutoff):
                continue
            prior = latest.get(r["realization_package_id"])
            if prior is None or (str(r.get("as_of_utc")), r.get("snapshot_id")) > (str(prior.get("as_of_utc")), prior.get("snapshot_id")):
                latest[r["realization_package_id"]] = r
        by_wh: Dict[str, List[Mapping[str, Any]]] = {}
        for r in sorted(latest.values(), key=lambda r: (str(r["source_recommendation"].get("generated_at_utc")), r["realization_package_id"])):
            by_wh.setdefault(wh_key(r["warehouse"]["workspace_id"], r["warehouse"]["warehouse_id"]), []).append(r)
        evidence_by_wh = {wh_key(str(w.get("workspace_id")), str(w.get("warehouse_id"))): w for w in (run["evidence"] or {}).get("warehouses") or []}
        packages = sorted(run["packages"], key=lambda p: (p["warehouse"]["workspace_id"], p["warehouse"]["warehouse_id"]))
        warehouses = [project_warehouse(p, by_wh.get(wh_key(p["warehouse"]["workspace_id"], p["warehouse"]["warehouse_id"]), []),
                                        evidence_by_wh.get(wh_key(p["warehouse"]["workspace_id"], p["warehouse"]["warehouse_id"]))) for p in packages]
        for w in warehouses:
            for l in w["realization"]:
                l["timeline"] = [{"event": e["event_type"], "effective": e["effective_at_utc"], "recorded": e["recorded_at_utc"]}
                                 for e in ledgers.get(l["rpkg"], []) if cutoff is None or str(e["recorded_at_utc"]) < str(cutoff)]
        runs.append({"generated_at": run["generated_at"], "manifest": run["manifest"], "packages": packages, "warehouses": warehouses,
                     "recs": recommendations(warehouses), "evidence": run["evidence"], "folder": run["folder"]})
    apply_aging(runs)

    out_runs, details = [], {}
    for i, run in enumerate(runs):
        kpis = run_kpis(run["warehouses"], run["packages"], run["manifest"], run["recs"])
        key = str(run["generated_at"])
        entry = {"key": key, "generated_at": run["generated_at"], "label": key[:10],
                 "publisher_version": run["manifest"].get("publisher_version"), "contract": run["manifest"].get("reporter_contract_version"),
                 "workbook": run["manifest"].get("workbook_status"), "kpis": kpis, "detail": i >= len(runs) - detail_runs,
                 "insights": {c: insights(k, run["warehouses"], c) for c, k in kpis["by_currency"].items()}}
        if not demo:
            entry["folder"] = rel(run["folder"], publisher_root)
        out_runs.append(entry)
        if entry["detail"]:
            details[key] = {"warehouses": run["warehouses"], "recommendations": run["recs"]}
    newest = runs[-1] if runs else None
    raw_docs = {}
    if newest is not None and raw != "none":
        for p in newest["packages"]:
            raw_docs[wh_key(p["warehouse"]["workspace_id"], p["warehouse"]["warehouse_id"])] = p if raw == "full" else lean_package(p)
    used_blockers = sorted({b for d in details.values() for w in d["warehouses"] for c in w["candidates"] for b in c["blockers"]}
                           | {b for d in details.values() for w in d["warehouses"] for b in ((w.get("reevaluation") or {}).get("blocking_codes") or [])})
    strings = compact_evidence(details, lean=raw != "full")
    return {
        "strings": strings, "evidence_fields": list(EVIDENCE_FIELDS),
        "meta": {"generator": GENERATOR_VERSION, "demo": demo, "echarts": ECHARTS_VERSION, "raw": raw, "detail_runs": detail_runs,
                 "publisher_root": None if demo else str(publisher_root), "realizer_root": None if demo or realizer_root is None else str(realizer_root),
                 "realizer_found": bool(rpkgs), "rpkg_snapshots": len(rpkgs)},
        "runs": out_runs, "details": details, "skipped": sorted(skipped, key=lambda s: (s["run"], s["reason"])),
        "daily": daily_series(newest["evidence"]) if newest else {}, "daily_run": out_runs[-1]["key"] if out_runs else None,
        "raw": raw_docs, "blockers": {b: blockers.get(b, "") for b in used_blockers},
    }


# --------------------------------------------------------------------------- rendering

def embed(data: Any) -> str:
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def vendor_js() -> str:
    raw = VENDOR.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != ECHARTS_SHA256:
        raise SystemExit(f"{VENDOR}: sha256 {digest} is not the pinned ECharts {ECHARTS_VERSION} ({ECHARTS_SHA256})")
    return raw.decode("utf-8").replace("</script", "<\\/script").replace("<!--", "<\\!--")


def font_faces() -> str:
    import base64
    faces = []
    for family, weight, name, digest in FONTS:
        raw = (FONTS_DIR / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SystemExit(f"{name}: sha256 differs from the pinned font")
        faces.append(f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};font-display:swap;"
                     f"src:url(data:font/woff2;base64,{base64.b64encode(raw).decode('ascii')}) format('woff2')}}")
    return '<style id="vendor-fonts">' + "\n".join(faces) + "</style>"


def render(dataset: Mapping[str, Any], title: str) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    for marker in MARKERS:
        if html.count(marker) != 1:
            raise SystemExit(f"template marker {marker} must appear exactly once")
    html = html.replace("<!--@TITLE@-->", title).replace("<!--@FONTS@-->", font_faces())
    html = html.replace("<!--@VENDOR@-->", f'<script id="vendor-echarts" data-version="{ECHARTS_VERSION}">{vendor_js()}</script>')
    return html.replace("<!--@DATA@-->", f'<script type="application/json" id="dashboard-data">{embed(dataset)}</script>')


# --------------------------------------------------------------------------- verify

WORKBOOK_METRICS = {  # workbook Executive Summary metric -> dataset KPI path
    "Warehouses assessed": ("warehouses",), "Actual trailing 365d": ("actual", "total"), "Forward 365d baseline": ("forward",),
    "Recommended annual savings": ("gross_savings",), "Recommended annual cost increase": ("cost_increase",),
    "Recommended net annual impact": ("net",), "Plan savings low": ("identified", "low"), "Plan savings base": ("identified", "base"),
    "Plan savings high": ("identified", "high"),
}


def workbook_values(path: Path) -> Dict[str, Any]:
    import openpyxl
    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True)["Executive Summary"]
    values = {}
    for row in sheet.iter_rows(min_col=1, max_col=3, values_only=True):
        if row and row[1] in WORKBOOK_METRICS:
            values[row[1]] = row[2]
    return values


def verify(html: str, dataset: Mapping[str, Any], publisher_root: Optional[Path]) -> List[str]:
    problems = []
    vendor = re.search(r'<script id="vendor-echarts"[^>]*>.*?</script>', html, re.S)
    if not vendor:
        problems.append("vendored ECharts block missing")
    page = html.replace(vendor.group(0), "") if vendor else html
    page_no_data = re.sub(r'<script type="application/json" id="dashboard-data">.*?</script>', "", page, flags=re.S)
    if re.search(r'<(script|link|img|iframe)[^>]+(src|href)\s*=\s*["\']?(https?:)?//', page_no_data, re.I):
        problems.append("external resource referenced (the page must be self-contained)")
    if "{{" in page_no_data or "<!--@" in page_no_data:
        problems.append("unresolved template placeholder")
    ids = re.findall(r'\sid="([^"]+)"', page_no_data)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate element ids: {dupes}")
    size = len(html.encode("utf-8"))
    if size > SIZE_FAIL and dataset["meta"]["demo"]:
        problems.append(f"the demo page is {size / 1048576:.1f} MB, above the {SIZE_FAIL // 1048576} MB limit for a committed file")
    elif size > SIZE_WARN:
        print(f"warning: page is {size / 1048576:.1f} MB (above {SIZE_WARN // 1048576} MB)")
    for need in ("data-view=\"executive\"", "data-view=\"portfolio\"", "data-view=\"recommendations\"", "data-view=\"realization\"", "data-view=\"warehouse\"", "data-view=\"audit\""):
        if need not in page_no_data:
            problems.append(f"view missing: {need}")
    problems += script_syntax(page_no_data)
    if not dataset["runs"]:
        problems.append("no complete Publisher run found")
    for run in dataset["runs"]:
        for currency, k in run["kpis"]["by_currency"].items():
            if k["actual"]["total"] is not None and k["actual_known"]["count"] != k["warehouses"]:
                problems.append(f"{run['key']} {currency}: an actual total with unknown warehouses")
    if publisher_root is not None:
        problems += reconcile_workbooks(dataset, publisher_root)
    return problems


def script_syntax(page: str) -> List[str]:
    """The page's own scripts parse (checked with `node --check` when Node is installed; skipped otherwise)."""
    node = shutil.which("node")
    if node is None:
        print("verify: node not found; script syntax not checked")
        return []
    problems = []
    for i, body in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(body)
        try:
            done = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
            if done.returncode != 0:
                problems.append(f"page script {i} does not parse: " + (done.stderr.strip().splitlines() or ["?"])[-1])
        finally:
            os.unlink(f.name)
    return problems


def reconcile_workbooks(dataset: Mapping[str, Any], publisher_root: Path) -> List[str]:
    """Each run's single-currency KPIs equal the cells of its own workbook's Executive Summary."""
    problems = []
    runs_found, _ = discover_publisher_runs(publisher_root)
    by_key = {}
    for folder in runs_found:
        manifest = read_json(folder / "manifest.json")
        if manifest.get("workbook_status") == "GENERATED" and (folder / str(manifest.get("workbook_file") or "workbook.xlsx")).is_file():
            by_key[str(manifest.get("generated_at"))] = folder / str(manifest.get("workbook_file") or "workbook.xlsx")
    checked = 0
    for run in dataset["runs"]:
        path = by_key.get(run["key"])
        if path is None or len(run["kpis"]["by_currency"]) != 1:
            continue
        kpi = next(iter(run["kpis"]["by_currency"].values()))
        for metric, cell in workbook_values(path).items():
            ours: Any = kpi
            for part in WORKBOOK_METRICS[metric]:
                ours = ours[part]
            theirs = None if cell in (None, "UNAVAILABLE", "N/A", "MIXED") else cell
            if (ours is None) != (theirs is None) or (ours is not None and abs(float(ours) - float(theirs)) > 0.011):
                problems.append(f"{run['key']}: {metric} is {ours} on the dashboard but {cell} in the workbook")
        checked += 1
    print(f"verify: reconciled {checked} run(s) with their workbook Executive Summary")
    return problems


# --------------------------------------------------------------------------- demo pipeline

def embedded_run_keys(path: Path) -> List[str]:
    """Run keys of a previously built page (empty when there is none or it cannot be read)."""
    try:
        text = path.read_text(encoding="utf-8")
        blob = text.split('id="dashboard-data">', 1)[1].split("</script>", 1)[0]
        return [r["key"] for r in json.loads(blob.replace("<\\/", "</").replace("<\\!--", "<!--"))["runs"]]
    except (OSError, IndexError, ValueError, KeyError):
        return []


def run_demo_pipeline(workdir: Path) -> Tuple[Path, Path]:
    """Three monthly Publisher demo runs, each followed by a Realizer fleet demo run into one shared state."""
    pub, real = workdir / "publisher", workdir / "realizer"
    if pub.exists() and len(discover_publisher_runs(pub)[0]) == len(DEMO_RUNS) and real.exists():
        return pub, real  # reuse a finished pipeline (--demo-workdir)
    shutil.rmtree(pub, ignore_errors=True); shutil.rmtree(real, ignore_errors=True)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    for as_of, realize_at in DEMO_RUNS:
        before = set(pub.glob("run_date=*/run_ts=*"))
        done = subprocess.run([sys.executable, "-m", "publisher", "--demo", "--demo-as-of", as_of, "--output-dir", str(pub), "--log-level", "WARNING"],
                              cwd=ROOT, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"publisher demo failed:\n{done.stderr[-2000:]}")
        run = sorted(set(pub.glob("run_date=*/run_ts=*")) - before)[-1]
        argv = [sys.executable, "-m", "realizer", "--publisher-run", str(run), "--demo", "--output-dir", str(real), "--log-level", "WARNING"]
        done = subprocess.run(argv + (["--as-of", realize_at] if realize_at else []), cwd=ROOT, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"realizer demo failed:\n{done.stderr[-2000:]}")
        print(f"demo: publisher {as_of} + realizer {realize_at or 'default'}")
    return pub, real


# --------------------------------------------------------------------------- main

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--publisher-root", default="./warehouse_optimizer_out", help="Publisher --output-dir (run_date=/run_ts= folders, or one flat run)")
    ap.add_argument("--realizer-root", default="./realizer_out", help="Realizer --output-dir (state/ and run folders); missing = no realization data")
    ap.add_argument("--out", help="Output HTML (default <publisher-root>/dashboard/inframate-dashboard.html; demo: the committed demo)")
    ap.add_argument("--detail-runs", type=int, default=6, help="Runs with full drill-down detail (KPI trends cover every run)")
    ap.add_argument("--raw", choices=("full", "lean", "none"), help="Raw package JSON for the Audit explorer (default: full; demo: lean = without the evidence map)")
    ap.add_argument("--title", help="Page title (default 'Inframate dashboard')")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Build the committed demo from a scratch Publisher + Realizer demo pipeline")
    mode.add_argument("--check", action="store_true", help="Rebuild the demo in memory and compare it with the committed file")
    mode.add_argument("--refresh", action="store_true", help="Rebuild the real-data page from the roots with the latest runs, verify it, and report what is new")
    ap.add_argument("--demo-workdir", help="Keep (or reuse) the demo pipeline output here instead of a temporary folder")
    ap.add_argument("--verify", action="store_true", help="After building, verify the page and reconcile KPIs with each run's workbook")
    args = ap.parse_args(argv)
    if args.detail_runs < 1:
        ap.error("--detail-runs must be at least 1")

    demo = args.demo or args.check
    if args.refresh:
        args.verify = True
    tmp = None
    if demo:
        workdir = Path(args.demo_workdir) if args.demo_workdir else Path(tmp := tempfile.mkdtemp(prefix="inframate-dashboard-demo-"))
        publisher_root, realizer_root = run_demo_pipeline(workdir)
        out = Path(args.out) if args.out else DEMO_OUT
        raw = args.raw or "lean"
    else:
        publisher_root, realizer_root = Path(args.publisher_root), Path(args.realizer_root)
        if not publisher_root.exists():
            ap.error(f"--publisher-root {publisher_root} does not exist (run the publisher first, or use --demo)")
        out = Path(args.out) if args.out else publisher_root / "dashboard" / "inframate-dashboard.html"
        raw = args.raw or "full"
    try:
        dataset = build_dataset(publisher_root, realizer_root if realizer_root.exists() else None, detail_runs=args.detail_runs, raw=raw, demo=demo)
        html = render(dataset, args.title or ("Inframate dashboard · demo" if demo else "Inframate dashboard"))
        if args.check:
            current = out.read_text(encoding="utf-8") if out.is_file() else ""
            if current != html:
                print(f"check: {rel(out, ROOT)} is out of date; rebuild with --demo")
                return 1
            print(f"check: {rel(out, ROOT)} is current")
        else:
            before = embedded_run_keys(out) if args.refresh else []
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            if args.refresh:
                keys = [r["key"] for r in dataset["runs"]]
                message = f"refresh: newest run {keys[-1][:10] if keys else 'none'}"
                if not before:
                    message += f"; first build, {len(keys)} run(s)"
                else:
                    if keys and before[-1] != keys[-1]:
                        message += f" (the page showed {before[-1][:10]} before)"
                    message += f"; {sum(1 for k in keys if k not in before)} run(s) new since the last build"
                print(message)
            runs = dataset["runs"]
            print(f"wrote {out} ({len(html.encode('utf-8')) / 1048576:.1f} MB, {len(runs)} run(s), "
                  f"{sum(1 for r in runs if r['detail'])} with detail, {dataset['meta']['rpkg_snapshots']} realization snapshot(s), "
                  f"{len(dataset['skipped'])} skipped)")
            if not demo:
                print("note: this page holds owner emails and cost figures from your workspace; keep it local (it is not meant to be committed)")
        if args.verify or args.check:
            problems = verify(html, dataset, publisher_root)
            for p in problems:
                print(f"verify: {p}")
            if problems:
                return 1
            print("verify: OK")
        return 0
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
