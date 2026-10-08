#!/usr/bin/env python3
"""Mine a publisher run for golden-scenario patterns (read-only, deterministic).

Usage:
  python .claude/skills/databricks-golden-miner/scripts/mine_patterns.py --run-dir mining_out [--out mining_out/report]
      [--catalog <signature-catalog.yaml>] [--real-data mining_out/real_data_answers.json] [--no-demo-reference]

Reads the run's evidence.json (versioned envelope) and packages/*.json, computes one feature vector per warehouse,
matches it against the signature catalog (references/signature-catalog.yaml), and writes pattern_report.json and
pattern_report.md with six parts:
  coverage    -- warehouses per catalog row, and rows with no live match
  gaps        -- warehouses no row matches, grouped by a coarse fingerprint
  mismatches  -- a row matches but the package contradicts that row's expected behavior (its `expect` assertions,
                 the golden assertion DSL of tests/golden/publisher/harness.py). Packages produced under a run scope (non-null
                 `run_scope`) lack the candidates of out-of-scope knobs, so their expectations cannot be judged: they
                 are skipped, and the report header and the `run_scope` object say so. Coverage, gaps, premises and
                 drift come from the evidence and are still reported.
  premises    -- per premise (an assumption a row's expected behavior rests on, e.g. a Pro restart takes ~4 minutes):
                 live warehouses where it holds, is contradicted or cannot be checked, and each contradiction
  drift       -- per matched row, the live range of its drift features next to the demo-fleet warehouse's value
  real_data   -- the read-only real-data-run aggregates (G4b/G5/S4), copied from --real-data when given
Same inputs give byte-identical reports: no clock, sorted keys and lists. Run from the repository root with the
publisher installed (pip install -e .). Exit 0 on success, 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[2]
for p in (REPO, REPO / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from publisher.analyzer.collector.replay import validate_evidence_payload
from publisher.common.utils import as_serializable  # noqa: E402
from publisher.config import defaults  # noqa: E402
from publisher.config.defaults import SIZE_ORDER, SQL_DBU_PER_CLUSTER_HOUR  # noqa: E402
from publisher.governor.cost_profiles import nonprod_tag_match  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from tests.golden.publisher.harness import check, resolve  # noqa: E402

DEFAULT_CATALOG = SKILL_DIR / "references" / "signature-catalog.yaml"
REPORT_VERSION = "1.1.0"
OPS = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "not_null")


# --- Inputs ------------------------------------------------------------------------------------------------------
def find_run(run_dir: Path) -> Path:
    """The folder holding evidence.json: run_dir itself (a flat publisher output, or one Hive run_date=/run_ts= folder),
    or the single run folder below it. Several runs below it are ambiguous: the error lists them so the user can pick."""
    if (run_dir / "evidence.json").is_file():
        return run_dir
    found = sorted(p.parent for p in run_dir.rglob("evidence.json"))
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(f"error: no evidence.json under {run_dir}; pass the run folder (the publisher --output-dir, or a run_date=/run_ts= folder)")
    listing = "\n".join(f"  --run-dir {p}" for p in found)
    raise SystemExit(f"error: {len(found)} runs under {run_dir}; pass exactly one of the run folders:\n{listing}")


def load_run(run: Path) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]]]:
    payload = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
    validate_evidence_payload(payload)
    packages = {}
    for path in sorted((run / "packages").glob("*.json")):
        pkg = json.loads(path.read_text(encoding="utf-8"))
        packages[(pkg["warehouse"]["workspace_id"], pkg["warehouse"]["warehouse_id"])] = pkg
    return payload["warehouses"], packages


# --- Features ----------------------------------------------------------------------------------------------------
def _ts(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _peak(histogram: Optional[Dict[str, float]]) -> Optional[int]:
    levels = [int(k) for k, v in (histogram or {}).items() if v]
    return max(levels) if levels else None


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    return round(num / den, 4) if num is not None and den else None


def _rank(size: Optional[str]) -> Optional[int]:
    return SIZE_ORDER.index(size) if size in SIZE_ORDER else None


def _size_rate_deviation(rates: Optional[Dict[str, float]]) -> Optional[float]:
    """Largest |billed size-pair DBU ratio / published ratio - 1| over the sizes the workspace billed (None below 2)."""
    sizes = [s for s in SIZE_ORDER if (rates or {}).get(s) and s in SQL_DBU_PER_CLUSTER_HOUR]
    if len(sizes) < 2:
        return None
    return round(max(abs((rates[a] / rates[b]) / (SQL_DBU_PER_CLUSTER_HOUR[a] / SQL_DBU_PER_CLUSTER_HOUR[b]) - 1)
                     for i, a in enumerate(sizes) for b in sizes[i + 1:]), 4)


def features(w: Dict[str, Any], t: Thresholds) -> Dict[str, Any]:
    """Scalar features of one serialized WarehouseEvidence; None where the evidence has no value."""
    epochs = sorted(w["epochs"], key=lambda e: e["epoch_start"])
    cur = next(e for e in epochs if e["is_current"])
    prev = [e for e in epochs if not e["is_current"] and e["epoch_start"] < cur["epoch_start"]]
    prev = prev[-1] if prev else None
    cfg, q, ev, gaps = cur["config"], cur["query"] or {}, cur["events"] or {}, cur["gaps"] or {}
    fm = w.get("forecast_metadata") or {}
    qs = (w.get("attribution") or {}).get("query_source") or {}
    analysis = _ts(w["analysis_time"])
    hist = q.get("concurrency_histogram") or {}
    weeks = [q.get(f"queries_week_{i}") for i in range(1, 9)]
    recent, early = weeks[0:2], weeks[6:8]
    days_30 = [d for d in (w.get("daily_cost_365") or [])[-30:] if (d.get("dbu_quantity") or 0) > 0]
    f: Dict[str, Any] = {
        "warehouse_type": cfg["warehouse_type"],
        "warehouse_size": cfg["warehouse_size"],
        "size_rank": _rank(cfg["warehouse_size"]),
        "warehouse_channel": cfg.get("warehouse_channel"),
        "min_clusters": cfg["min_clusters"],
        "max_clusters": cfg["max_clusters"],
        "auto_stop_minutes": cfg["auto_stop_minutes"],
        "epoch_count": len(epochs),
        "current_epoch_age_days": (analysis - _ts(cur["epoch_start"])).days,
        "prior_size_step": (_rank(cfg["warehouse_size"]) - _rank(prev["config"]["warehouse_size"])) if prev and _rank(prev["config"]["warehouse_size"]) is not None else None,
        "prior_type": prev["config"]["warehouse_type"] if prev else None,
        "nonprod": nonprod_tag_match(cur.get("tags") or {}, t) is not None,
        "region_count": len([r for r in str(w.get("source_region") or "").split(",") if r]),
        "photon_status": (w.get("photon_observation") or {}).get("status"),
        "cost_history_days": w.get("cost_history_days"),
        "baseline_forecast_cost_365": w.get("baseline_forecast_cost_365"),
        "dbu_cost_known": w.get("baseline_forecast_cost_365") is not None,
        "billed_days_30": len(days_30),
        "forecast_model": fm.get("forecast_model"),
        "billing_correction_share": fm.get("billing_correction_dbu_share_365"),
        "unpriced_dbu_365": fm.get("unpriced_dbu_quantity_365"),
        "queries_30": q.get("queries_30"),
        "active_days_30": q.get("active_days_30"),
        "p95_execution_ms_30": q.get("p95_execution_ms_30"),
        "queued_query_pct_30": q.get("queued_query_pct_30"),
        "p95_wait_capacity_ms_when_queued_30": q.get("p95_wait_capacity_ms_when_queued_30"),
        "spill_query_pct_30": q.get("spill_query_pct_30"),
        "total_spill_gib_30": q.get("total_spill_gib_30"),
        "failure_cancel_pct_30": q.get("failure_cancel_pct_30"),
        "result_cache_pct_30": q.get("result_cache_pct_30"),
        "compute_wait_queries_30": q.get("compute_wait_queries_30"),
        "growth_ratio_8w": _ratio(sum(recent), sum(early)) if None not in recent + early else None,
        "peak_concurrency_recent": _peak(hist.get("RECENT")),
        "peak_concurrency_epoch": _peak(hist.get("EPOCH")),
        "uptime_minutes": ev.get("uptime_minutes"),
        "time_at_max_pct": ev.get("time_at_max_pct"),
        "time_at_min_pct": ev.get("time_at_min_pct"),
        "starts_30": ev.get("starts_30"),
        "scale_ups_30": ev.get("scale_ups_30"),
        "busy_ratio": _ratio(gaps.get("busy_minutes"), ev.get("uptime_minutes")),
        "p50_gap_minutes": gaps.get("p50_gap_minutes"),
        "p90_gap_minutes": gaps.get("p90_gap_minutes"),
        "kept_alive_gap_count": gaps.get("kept_alive_gap_count"),
        "primary_source": qs.get("primary_category"),
        "active_days_30": q.get("active_days_30"),
        "max_execution_ms_30": q.get("max_execution_ms_30"),
        "owner_known": (w.get("attribution") or {}).get("owner") is not None,
        "p95_wait_compute_ms_when_waited_30": q.get("p95_wait_compute_ms_when_waited_30"),
        "size_rate_deviation": _size_rate_deviation((w.get("billing_features") or {}).get("workspace_pro_dbu_per_cluster_hour_by_size")),
    }
    for cat, pct in sorted((qs.get("percentages") or {}).items()):
        f[f"pct_{cat.lower()}"] = pct
    # Derived shape features (comparisons the catalog cannot express between two features).
    stop, q30 = f["auto_stop_minutes"], f["queries_30"]
    f["compute_wait_share"] = _ratio(f["compute_wait_queries_30"], q30)
    f["gap_p90_below_auto_stop"] = f["p90_gap_minutes"] is not None and stop > 0 and f["p90_gap_minutes"] < stop
    f["gap_p50_above_auto_stop"] = f["p50_gap_minutes"] is not None and stop > 0 and f["p50_gap_minutes"] > stop
    peak = f["peak_concurrency_recent"]
    f["ceiling_headroom"] = f["max_clusters"] - -(-peak // 10) if peak is not None else None
    f["pinned"] = f["min_clusters"] == f["max_clusters"] > 1
    f["pct_bi"] = max([f.get(f"pct_{c}") or 0.0 for c in ("dashboard", "tableau", "power_bi", "looker", "genie")])
    f["epoch_peak_ratio"] = _ratio(f["peak_concurrency_epoch"], peak)
    f["concurrency_per_cluster_at_peak"] = _ratio(peak, f["max_clusters"])
    prev_p95 = ((prev or {}).get("query") or {}).get("epoch_p95_execution_ms")
    f["p95_vs_prior"] = _ratio(f["p95_execution_ms_30"], prev_p95)
    return f


# --- Matching ----------------------------------------------------------------------------------------------------
def _holds(value: Any, op: str, want: Any) -> bool:
    if op == "is_null":
        return value is None
    if op == "not_null":
        return value is not None
    if op in ("eq", "ne", "in", "not_in"):
        hit = value == want if op in ("eq", "ne") else value in want
        return hit if op in ("eq", "in") else not hit
    if value is None or isinstance(value, bool):
        return False
    return {"gt": value > want, "gte": value >= want, "lt": value < want, "lte": value <= want}[op]


def matches(f: Dict[str, Any], signature: Iterable[Dict[str, Any]]) -> bool:
    """Every condition holds; a condition is {feature: name, <op>: value} (or {any: [conditions]})."""
    for cond in signature:
        if "any" in cond:
            if not any(matches(f, [c]) for c in cond["any"]):
                return False
            continue
        ops = [o for o in OPS if o in cond]
        if len(ops) != 1:
            raise SystemExit(f"error: condition {cond} needs exactly one of {OPS}")
        if not _holds(f.get(cond["feature"]), ops[0], cond[ops[0]]):
            return False
    return True


def _features_of(conditions: Iterable[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for c in conditions:
        out += _features_of(c["any"]) if "any" in c else [c["feature"]]
    return out


def premise_status(f: Dict[str, Any], premise: Dict[str, Any]) -> Optional[str]:
    """None when the premise does not apply to this warehouse (its `when` fails); "unknown" when a checked feature has
    no value (missing evidence never contradicts); else "holds" or "contradicted"."""
    if premise.get("when") and not matches(f, premise["when"]):
        return None
    if any(f.get(name) is None for name in _features_of(premise["check"])):
        return "unknown"
    return "holds" if matches(f, premise["check"]) else "contradicted"


def assumed_value(name: str) -> Any:
    """The current value of the publisher setting a premise assumes: a Thresholds field or a config.defaults constant."""
    if name in Thresholds.__dataclass_fields__:
        return getattr(Thresholds(), name)
    if hasattr(defaults, name):
        return getattr(defaults, name)
    raise SystemExit(f"error: premise assumes unknown setting {name!r}")


def load_catalog(path: Path) -> Dict[str, Any]:
    cat = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = cat.get("rows") or []
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("error: duplicate row id in the signature catalog")
    for p in cat.get("premises") or []:
        unknown = set(p.get("rows") or []) - set(ids)
        if unknown:
            raise SystemExit(f"error: premise {p['id']} names rows not in the catalog: {sorted(unknown)}")
    return cat


# --- Report parts ------------------------------------------------------------------------------------------------
def _bucket(value: Optional[float], edges: List[float]) -> str:
    if value is None:
        return "na"
    for edge in edges:
        if value < edge:
            return f"<{edge:g}"
    return f">={edges[-1]:g}"


def fingerprint(f: Dict[str, Any]) -> str:
    """Coarse shape used to group unmatched warehouses."""
    return "|".join([
        f["warehouse_type"], f["warehouse_size"], f"clusters {f['min_clusters']}-{f['max_clusters']}",
        f"auto_stop {_bucket(f['auto_stop_minutes'], [1, 10, 31, 121])}", f"busy {_bucket(f['busy_ratio'], [0.1, 0.3, 0.7])}",
        f"queued {_bucket(f['queued_query_pct_30'], [1, 10, 30])}", f"spill {_bucket(f['spill_query_pct_30'], [1, 10])}",
        f"source {f['primary_source']}",
    ])


def _stats(values: List[float]) -> Dict[str, Any]:
    return {"n": len(values), "min": min(values), "median": statistics.median(values), "max": max(values)}


def demo_reference(t: Thresholds) -> Dict[str, Dict[str, Any]]:
    """Features of each demo-fleet warehouse by golden_scenario tag (the simulated reference for drift)."""
    from publisher.analyzer.collector.demo import synthetic_demo_evidence
    out = {}
    for w in as_serializable(synthetic_demo_evidence()):
        cur = next(e for e in w["epochs"] if e["is_current"])
        out[cur["tags"]["golden_scenario"]] = features(w, t)
    return out


def mine(warehouses: List[Dict[str, Any]], packages: Dict[Tuple[str, str], Dict[str, Any]], catalog: Dict[str, Any],
         reference: Optional[Dict[str, Dict[str, Any]]], real_data: Optional[Any]) -> Dict[str, Any]:
    t = Thresholds()
    rows = sorted(catalog.get("rows") or [], key=lambda r: r["id"])
    premises = sorted(catalog.get("premises") or [], key=lambda p: p["id"])
    tally = {p["id"]: {"holds": 0, "contradicted": 0, "unknown": 0} for p in premises}
    per_wh, hits, mismatches, gaps, contradictions = [], {r["id"]: [] for r in rows}, [], {}, []
    scoped_refs: List[str] = []  # packages produced under a run scope: expectations are not evaluated
    evaluated_refs: List[str] = []  # full-scope packages: expectations are evaluated
    feats: Dict[str, Dict[str, Any]] = {}
    for w in sorted(warehouses, key=lambda x: (x["workspace_id"], x["warehouse_id"])):
        ref = f"{w['workspace_id']}/{w['warehouse_id']}"
        f = features(w, t)
        feats[ref] = f
        matched = [r["id"] for r in rows if r.get("signature") and matches(f, r["signature"])]
        pkg = packages.get((w["workspace_id"], w["warehouse_id"]))
        is_scoped = pkg is not None and bool(pkg.get("run_scope"))
        annotated = annotate(pkg) if pkg is not None and not is_scoped else None
        if is_scoped:
            scoped_refs.append(ref)
        elif pkg is not None:
            evaluated_refs.append(ref)
        broken: Dict[str, List[str]] = {}  # row -> premises contradicted on this warehouse
        for p in premises:
            on_rows = [r for r in p["rows"] if r in matched]
            status = premise_status(f, p) if on_rows else None
            if status is None:
                continue
            tally[p["id"]][status] += 1
            if status == "contradicted":
                contradictions.append({"premise": p["id"], "rows": on_rows, "warehouse": ref, "warehouse_name": w["warehouse_name"],
                                       "values": {n: f.get(n) for n in _features_of(p["check"])}})
                for r in on_rows:
                    broken.setdefault(r, []).append(p["id"])
        for r in rows:
            if r["id"] not in matched:
                continue
            hits[r["id"]].append(ref)
            if annotated is None:
                continue
            failed = [msg for msg in (evaluate(annotated, a) for a in r.get("expect") or []) if msg]
            if failed:
                mismatches.append({"row": r["id"], "warehouse": ref, "warehouse_name": w["warehouse_name"],
                                   "registry_status": r.get("status"), "priority": "high" if r.get("status") == "fail" else "normal",
                                   "failed_assertions": failed, "premises_contradicted": broken.get(r["id"], [])})
        if not any(r["id"] in matched and r.get("role", "primary") == "primary" for r in rows):
            gaps.setdefault(fingerprint(f), []).append(ref)
        per_wh.append({"warehouse": ref, "warehouse_name": w["warehouse_name"], "package": pkg is not None,
                       "matched_rows": matched, "features": f})
    drift = []
    for r in rows:
        if not hits[r["id"]] or not r.get("drift"):
            continue
        demo = (reference or {}).get(r["id"])
        for name in r["drift"]:
            live = [feats[ref][name] for ref in hits[r["id"]] if isinstance(feats[ref].get(name), (int, float)) and not isinstance(feats[ref].get(name), bool)]
            if not live:
                continue
            s = _stats(live)
            dv = demo.get(name) if demo else None
            drift.append({"row": r["id"], "feature": name, "live": s, "demo_value": dv,
                          "demo_outside_live_range": dv is not None and not s["min"] <= dv <= s["max"]})
    return {
        "report_version": REPORT_VERSION,
        "catalog_version": str(catalog.get("version")),
        "warehouse_count": len(per_wh),
        "run_scope": {
            "scoped": bool(scoped_refs),
            "scoped_packages": len(scoped_refs),
            "full_scope_packages": len(evaluated_refs),
            "mismatches_evaluated": bool(evaluated_refs),
            "mismatches_evaluated_for": evaluated_refs,
            "mismatches_not_evaluated_for": scoped_refs,
        },
        "coverage": {
            "rows": [{"row": r["id"], "title": r.get("title"), "registry_status": r.get("status"), "warehouses": hits[r["id"]]} for r in rows],
            "rows_without_live_match": [r["id"] for r in rows if r.get("signature") and not hits[r["id"]]],
            "manual_rows": [r["id"] for r in rows if not r.get("signature")],
        },
        "gaps": [{"fingerprint": k, "warehouses": v} for k, v in sorted(gaps.items())],
        "mismatches": sorted(mismatches, key=lambda m: (m["priority"] != "high", m["row"], m["warehouse"])),
        "drift": drift,
        "premises": [{"premise": p["id"], "text": p["text"], "rows": p["rows"],
                      "assumes": {"name": p["assumes"]["name"], "value": assumed_value(p["assumes"]["name"])},
                      "live": tally[p["id"]]} for p in premises],
        "premise_contradictions": sorted(contradictions, key=lambda c: (c["premise"], c["warehouse"])),
        "real_data": real_data,
        "warehouses": per_wh,
    }


def _direction(knob: str, current: Any, target: Any) -> Optional[str]:
    if current is None or target is None:
        return None
    if knob == "warehouse_size":
        current, target = _rank(current), _rank(target)
    elif knob == "warehouse_type":
        return "same" if current == target else "change"
    if not isinstance(current, (int, float)) or not isinstance(target, (int, float)):
        return None
    return "up" if target > current else "down" if target < current else "same"


def annotate(pkg: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of the package whose candidates carry `direction` (up/down/same/change) for the expect assertions."""
    out = json.loads(json.dumps(pkg))
    for c in out.get("candidates") or []:
        c["direction"] = _direction(c["knob"], c.get("current_value"), c.get("recommended_value"))
    return out


def evaluate(pkg: Dict[str, Any], assertion: Dict[str, Any]) -> Optional[str]:
    """None when the assertion holds. The golden DSL (tests/golden/publisher/harness.py check) plus `in`/`not_in` per value and
    `any: [assertions]` (holds when one of them holds)."""
    try:
        if "any" in assertion:
            failed = [evaluate(pkg, a) for a in assertion["any"]]
            return None if None in failed else "any of: " + " | ".join(failed)
        if "in" in assertion or "not_in" in assertion:
            values = resolve(pkg, assertion["path"])
            if not values:
                return f"{assertion['path']}: no value matched"
            for v in values:
                if "in" in assertion and v not in assertion["in"]:
                    return f"{assertion['path']}: expected one of {assertion['in']!r}, got {v!r}"
                if "not_in" in assertion and v in assertion["not_in"]:
                    return f"{assertion['path']}: expected none of {assertion['not_in']!r}, got {v!r}"
            return None
        return check(pkg, assertion)
    except (KeyError, TypeError, ValueError) as exc:
        return f"{assertion.get('path')}: {type(exc).__name__}: {exc}"


def scope_note(r: Dict[str, Any]) -> str:
    """One sentence for the report header and the console when any package was produced under a run scope; else ''."""
    s = r["run_scope"]
    if not s["scoped"]:
        return ""
    which = "all" if not s["full_scope_packages"] else f"{s['scoped_packages']} of {s['scoped_packages'] + s['full_scope_packages']}"
    return (f"Scoped run: {which} package(s) were produced under a run scope (`run_scope` is set), so their candidates for "
            "out-of-scope knobs are absent and behaviour mismatches were not evaluated for them. Coverage, gaps, premises and "
            "drift come from the evidence and are reported as usual; mine a full-scope run for mismatches.")


def render_md(r: Dict[str, Any]) -> str:
    out = [f"# Golden pattern report ({r['warehouse_count']} warehouses, catalog {r['catalog_version']})", ""]
    if scope_note(r):
        out += [f"> {scope_note(r)}", ""]
    out += ["## Coverage", "", "| Row | Title | Registry status | Live warehouses |", "|---|---|---|---|"]
    out += [f"| {c['row']} | {c['title']} | {c['registry_status']} | {len(c['warehouses'])} |" for c in r["coverage"]["rows"] if c["warehouses"]]
    out += ["", f"Rows with no live match: {', '.join(r['coverage']['rows_without_live_match']) or 'none'}",
            f"Rows checked by hand only: {', '.join(r['coverage']['manual_rows']) or 'none'}", "", "## Gaps", ""]
    out += [f"- `{g['fingerprint']}`: {len(g['warehouses'])} warehouse(s): {', '.join(g['warehouses'])}" for g in r["gaps"]] or ["None."]
    out += ["", "## Behaviour mismatches", ""]
    if r["run_scope"]["scoped"]:
        out += [f"Not evaluated for {len(r['run_scope']['mismatches_not_evaluated_for'])} scoped package(s) (see the note at the top).", ""]
    out += [f"- **{m['row']}** ({m['priority']}) {m['warehouse']} {m['warehouse_name']}: " + "; ".join(m["failed_assertions"])
            + (f" [premise contradicted: {', '.join(m['premises_contradicted'])}]" if m["premises_contradicted"] else "")
            for m in r["mismatches"]] or ([] if not r["run_scope"]["mismatches_evaluated"] else ["None."])
    out += ["", "## Premise contradictions", "", "| Premise | Assumes | Rows | Holds | Contradicted | Unknown |", "|---|---|---|---|---|---|"]
    out += [f"| {p['premise']} | {p['assumes']['name']} = {p['assumes']['value']} | {', '.join(p['rows'])} | {p['live']['holds']} | "
            f"{p['live']['contradicted']} | {p['live']['unknown']} |" for p in r["premises"]]
    out += [""] + [f"- **{c['premise']}** on {c['warehouse']} {c['warehouse_name']} (rows {', '.join(c['rows'])}): "
                   + ", ".join(f"{k}={v}" for k, v in c["values"].items()) for c in r["premise_contradictions"]]
    out += ["", "## Drift (live range vs demo-fleet value)", "", "| Row | Feature | n | Live min | Median | Max | Demo | Outside |", "|---|---|---|---|---|---|---|---|"]
    out += [f"| {d['row']} | {d['feature']} | {d['live']['n']} | {d['live']['min']} | {d['live']['median']} | {d['live']['max']} | {d['demo_value']} | {'yes' if d['demo_outside_live_range'] else ''} |" for d in r["drift"]]
    out += ["", "## Real-data answers", "", "```json", json.dumps(r["real_data"], indent=2, sort_keys=True, default=str), "```", ""]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--out", type=Path, help="report folder (default: <run folder>/pattern_report)")
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--real-data", type=Path, help="real_data_answers.json from the statement-API queries")
    ap.add_argument("--no-demo-reference", action="store_true", help="skip the demo-fleet drift reference")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    run = find_run(args.run_dir)
    warehouses, packages = load_run(run)
    catalog = load_catalog(args.catalog)
    reference = None if args.no_demo_reference else demo_reference(Thresholds())
    real = json.loads(args.real_data.read_text(encoding="utf-8")) if args.real_data else None
    report = mine(warehouses, packages, catalog, reference, real)
    out = args.out or run / "pattern_report"
    out.mkdir(parents=True, exist_ok=True)
    (out / "pattern_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (out / "pattern_report.md").write_text(render_md(report), encoding="utf-8")
    print(f"pattern report: {out} | warehouses={report['warehouse_count']} gaps={sum(len(g['warehouses']) for g in report['gaps'])} "
          f"mismatches={len(report['mismatches']) if report['run_scope']['mismatches_evaluated'] else 'not evaluated (scoped run)'} premise_contradictions={len(report['premise_contradictions'])} "
          f"drift_flags={sum(d['demo_outside_live_range'] for d in report['drift'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
