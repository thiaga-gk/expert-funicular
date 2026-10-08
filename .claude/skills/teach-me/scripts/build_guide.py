#!/usr/bin/env python3
"""Build the inframate field guide: docs/guide/shared/inframate-guide.html.

The guide is a projection, like the workbook: every number, version, table and example comes from the code, the
reference documents or a committed golden case at build time, and the narrative comes from the chapter fragments in
assets/chapters/. Nothing in the output is edited by hand.

    python .claude/skills/teach-me/scripts/build_guide.py            build and write the guide
    python .claude/skills/teach-me/scripts/build_guide.py --verify   build, then check citations, example arithmetic,
                                                                     placeholders and offline-ness (exit 1 on failure)
    python .claude/skills/teach-me/scripts/build_guide.py --check    rebuild in memory, compare with the committed guide
    python .claude/skills/teach-me/scripts/build_guide.py --stale    chapters whose cited sources changed since the
                                                                     committed guide was built (the refresh worklist)

Fragment placeholders are documented in references/authoring.md.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
import math
import re
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL.parents[2]
OUT = ROOT / "docs" / "guide" / "shared" / "inframate-guide.html"
TO_ROOT = "../../../"  # from docs/guide/shared/ back to the repo root
EXAMPLE_CASE = "ge12-full-scope-auto-stop"
GENERATOR_VERSION = "1.0.0"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from publisher.config import defaults as pub_defaults  # noqa: E402
from publisher.config import versions as pub_versions  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402
from publisher.contracts import workbook_contract as wc  # noqa: E402
from publisher.governor import cost_profiles  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from realizer.config import settings as real_settings  # noqa: E402
from realizer.contracts import realization as real_contract  # noqa: E402


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def link_to(repo_path: str) -> str:
    return TO_ROOT + repo_path


def slug(text: str) -> str:
    """GitHub-style heading anchor."""
    text = re.sub(r"[`*_]", "", text.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def money(value: Any) -> str:
    v = float(value)
    s = f"{abs(v):,.2f}"
    return ("−" if v < 0 else "") + s


def split_row(line: str) -> List[str]:
    """Cells of a Markdown table row; a pipe inside backticks does not split."""
    cells, cur, tick = [], "", False
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    for ch in body:
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


def md_tables(path: Path) -> List[Dict[str, Any]]:
    """Every pipe table in a Markdown file with the heading trail above it."""
    out, trail = [], []
    lines = read(path).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#+)\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            trail = [t for t in trail if t[0] < level] + [(level, m.group(2).strip())]
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?\s*$", lines[i + 1]):
            header = split_row(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            out.append({"trail": [t[1] for t in trail], "header": header, "rows": rows})
            continue
        i += 1
    return out


def md_inline(text: str, doc_path: str) -> str:
    """Inline Markdown (code, bold, links) to HTML; relative links are rebased from the document to the guide."""
    parts = re.split(r"(`[^`]*`)", text)
    out = []
    doc_dir = Path(doc_path).parent
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            out.append("<code>" + esc(part[1:-1]) + "</code>")
            continue
        s = esc(part)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)

        def _link(m: "re.Match[str]") -> str:
            label, target = m.group(1), html.unescape(m.group(2))
            if re.match(r"^[a-z]+://", target):
                return f'<a href="{esc(target)}" rel="noopener">{label}</a>'
            file_part, _, anchor = target.partition("#")
            resolved = (doc_dir / file_part).as_posix() if file_part else doc_path
            resolved = str(Path(resolved)).replace("\\", "/")
            norm = []
            for seg in resolved.split("/"):
                if seg == "..":
                    norm.pop()
                elif seg and seg != ".":
                    norm.append(seg)
            href = link_to("/".join(norm)) + (("#" + anchor) if anchor else "")
            return f'<a href="{esc(href)}">{label}</a>'

        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, s)
        out.append(s)
    return "".join(out)


def strip_ticks(cell: str) -> str:
    return cell.strip().strip("`").strip()


def files_under(repo_path: str) -> List[Path]:
    path = ROOT / repo_path
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store")
    return []


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------- JSON path access for examples

_SEL = re.compile(r"([^.\[\]]+)|\[(\d+)\]|\[([\w.]+)=([^\]]+)\]")


def get_path(doc: Any, path: str) -> Any:
    """Dotted path with [n] indexes and [field=value] selectors, e.g. candidates[knob=auto_stop_minutes].confidence.score."""
    node = doc
    if not path:
        return node
    for name, index, field, value in _SEL.findall(path):
        if name:
            if not isinstance(node, dict) or name not in node:
                raise KeyError(path)
            node = node[name]
        elif index:
            node = node[int(index)]
        else:
            hits = [x for x in node if str(get_path(x, field)) == value]
            if len(hits) != 1:
                raise KeyError(f"{path}: selector [{field}={value}] matched {len(hits)} items")
            node = hits[0]
    return node


def resolve_selectors(doc: Any, path: str) -> str:
    """The same path with every [field=value] selector replaced by its index (what the explorer understands)."""
    node, out = doc, []
    for name, index, field, value in _SEL.findall(path):
        if name:
            node = node[name]
            out.append(("." if out else "") + name)
        elif index:
            node = node[int(index)]
            out.append(f"[{index}]")
        else:
            idx = [i for i, x in enumerate(node) if str(get_path(x, field)) == value]
            if len(idx) != 1:
                raise KeyError(path)
            node = node[idx[0]]
            out.append(f"[{idx[0]}]")
    return "".join(out)


# ---------------------------------------------------------------- facts from code and reference docs

def capture_parser(parse: Callable[..., Any], argv: Sequence[str]) -> Any:
    """The ArgumentParser a parse_args() function builds, without parsing anything."""
    class _Got(Exception):
        pass

    original = argparse.ArgumentParser.parse_args

    def _stop(self: argparse.ArgumentParser, *args: Any, **kwargs: Any) -> Any:
        raise _Got(self)

    argparse.ArgumentParser.parse_args = _stop  # type: ignore[assignment]
    try:
        parse(list(argv))
    except _Got as got:
        return got.args[0]
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[assignment]
    raise BuildError(f"{parse.__module__}.{parse.__name__} built no parser")


def cli_rows(parser: argparse.ArgumentParser) -> List[Dict[str, str]]:
    rows = []
    for action in parser._actions:  # noqa: SLF001 - reading the parser is the point
        if not action.option_strings or action.dest == "help":
            continue
        arg = ""
        if action.choices:
            arg = "{" + ",".join(str(c) for c in action.choices) + "}"
        elif action.metavar:
            arg = str(action.metavar)
        elif action.nargs != 0:
            arg = action.dest.upper()
        rows.append({"flag": ", ".join(action.option_strings), "arg": arg,
                     "help": (action.help or "").replace("%%", "%"), "repeat": isinstance(action, argparse._AppendAction)})  # noqa: SLF001
    return rows


def collect_facts() -> Dict[str, Any]:
    t = Thresholds()
    facts: Dict[str, Any] = {}

    facts["versions"] = {
        "publisher": {k.lower(): v for k, v in vars(pub_versions).items() if k.isupper() and isinstance(v, str)},
        "reporter_contract": rc.CONTRACT_VERSION,
        "workbook_contract": wc.WORKBOOK_CONTRACT_VERSION,
        "realizer": {k.lower(): v for k, v in vars(real_settings).items() if k.endswith("_VERSION") and isinstance(v, str)},
    }
    from realizer.config import loader as real_loader
    from realizer.contracts import approvals, backtest, measurement, observation, report
    from realizer.packager import lineage
    from realizer.reporter import run_manifest
    facts["versions"]["realizer_formats"] = {
        "realization_package_contract": real_contract.CONTRACT_VERSION,
        "ledger_contract": real_contract.LEDGER_CONTRACT_VERSION,
        "supported_publisher_contract": real_loader.SUPPORTED_PUBLISHER_CONTRACT,
        "observation_format": observation.OBSERVATION_FORMAT_VERSION,
        "measurement_evidence_format": measurement.MEASUREMENT_EVIDENCE_FORMAT_VERSION,
        "backtest_evidence_format": backtest.BACKTEST_EVIDENCE_FORMAT_VERSION,
        "backtest_report_format": backtest.BACKTEST_REPORT_FORMAT_VERSION,
        "approvals_format": approvals.APPROVALS_FORMAT_VERSION,
        "realization_workbook_contract": report.REALIZATION_WORKBOOK_CONTRACT_VERSION,
        "run_manifest": run_manifest.RUN_MANIFEST_VERSION,
        "state_index": lineage.STATE_INDEX_VERSION,
    }

    # Realizer policy defaults and the realization constants written in code.
    from realizer.analyzer import crosscheck
    from realizer.estimator import economics as real_economics
    from realizer.governor.policy import RealizerPolicy
    from realizer.packager import to_date
    policy = RealizerPolicy()
    facts["realizer"] = {
        "policy": {f.name: (str(getattr(policy, f.name)) if isinstance(getattr(policy, f.name), Decimal) else getattr(policy, f.name))
                   for f in dataclasses.fields(RealizerPolicy)},
        "review_above_high": str(real_economics.REVIEW_ABOVE_HIGH),
        "reoptimize_below_low": str(real_economics.REOPTIMIZE_BELOW_LOW),
        "volume_ratio_band": [str(x) for x in real_settings.VOLUME_RATIO_BAND],
        "agreement_band_pct": str(crosscheck.AGREEMENT_BAND * 100).rstrip("0").rstrip("."),
        "noise_multiple": str(crosscheck.NOISE_MULTIPLE),
        "mature_days": to_date.MATURE_DAYS,
        "canary_guard_window_days": real_settings.CANARY_GUARD_WINDOW_DAYS,
    }

    # Thresholds: code defaults, doc meaning, profile overlays.
    doc = "docs/reference/publisher/thresholds.md"
    meaning: Dict[str, Tuple[str, str]] = {}
    for table in md_tables(ROOT / doc):
        if "Parameter reference" in table["trail"] and table["header"][:2] == ["Parameter", "Default"]:
            group = table["trail"][-1]
            for row in table["rows"]:
                meaning[strip_ticks(row[0])] = (group, md_inline(row[2], doc))
    profiles = {name: cost_profiles.profile_values(name) for name in ("conservative", "balanced", "aggressive")}
    rows = []
    for f in dataclasses.fields(Thresholds):
        group, text = meaning.get(f.name, ("Other", ""))
        rows.append({"name": f.name, "default": getattr(t, f.name), "group": group, "meaning": text,
                     "profiles": {p: profiles[p].get(f.name) for p in profiles} if f.name in cost_profiles.PROFILED_FIELDS else None})
    facts["thresholds"] = {r["name"]: r["default"] for r in rows}
    # Derived values (properties such as the confidence caps) read like fields in fragments.
    for name, member in vars(Thresholds).items():
        if isinstance(member, property):
            facts["thresholds"][name] = getattr(t, name)
    facts["threshold_rows"] = rows
    facts["threshold_count"] = len(rows)
    facts["cost_profile_groups"] = {k: list(v) for k, v in rc.COST_PROFILE_GROUPS.items()}
    facts["profiles"] = profiles

    # Product constants and scope.
    doc = "docs/reference/publisher/product-constants.md"
    const_meaning = {}
    for table in md_tables(ROOT / doc):
        if table["header"][:2] == ["Constant", "Value"]:
            for row in table["rows"]:
                const_meaning[strip_ticks(row[0])] = md_inline(row[2], doc)
    facts["product_constants"] = dict(pub_defaults.PRODUCT_CONSTANTS)
    facts["product_constant_rows"] = [{"name": k, "value": v, "meaning": const_meaning.get(k, "")} for k, v in pub_defaults.PRODUCT_CONSTANTS.items()]
    from publisher import publisher as pub_facade
    facts["demo_fleet_size"] = pub_facade.demo_fleet_size()
    facts["supported_warehouse_types"] = sorted(pub_defaults.SUPPORTED_WAREHOUSE_TYPES)
    facts["active_knobs"] = list(pub_defaults.ACTIVE_KNOBS)
    facts["size_order"] = list(pub_defaults.SIZE_ORDER)
    facts["scope_scenarios"] = {name: {"scenario_type": st, "techniques": dict(tech)} for name, (st, tech) in pub_defaults.SCOPE_SCENARIOS.items()}

    # Technique catalog and blocker codes (their docs are the topic homes; the guide reads them).
    doc = "docs/reference/publisher/technique-catalog.md"
    tech_rows = []
    for table in md_tables(ROOT / doc):
        if table["header"][:2] == ["Knob", "Stable technique IDs"]:
            for row in table["rows"]:
                tech_rows.append({"knob": md_inline(row[0], doc), "ids": md_inline(row[1], doc), "algorithm": md_inline(row[2], doc), "blocker": md_inline(row[3], doc)})
    facts["technique_rows"] = tech_rows

    doc = "docs/reference/publisher/blocker-codes.md"
    golden_text = {p.parent.parent.name: read(p) for p in sorted((ROOT / "tests/golden/publisher/cases").glob("*/expected/*.json"))}
    blockers = []
    for table in md_tables(ROOT / doc):
        if table["header"][:1] == ["Code"]:
            group = table["trail"][-1] if table["trail"] else ""
            for row in table["rows"]:
                code = strip_ticks(row[0])
                if not re.fullmatch(r"[A-Z0-9_]+", code):
                    continue
                cols = dict(zip(table["header"], row))
                cases = sorted(name for name, text in golden_text.items() if f'"{code}"' in text)
                blockers.append({"code": code, "group": group, "meaning": md_inline(cols.get("Meaning", ""), doc),
                                 "fires": md_inline(cols.get("Fires when", ""), doc), "status": md_inline(cols.get("Resulting status", ""), doc),
                                 "cases": cases})
    facts["blockers"] = blockers
    facts["blocker_count"] = len(blockers)

    # Workbook.
    sheets = []
    for name, cols, groups in ((wc.SHEET_PORTFOLIO, wc.PORTFOLIO_COLUMNS, wc.PORTFOLIO_GROUPS),
                               (wc.SHEET_SCENARIOS, wc.SCENARIO_COLUMNS, wc.SCENARIO_GROUPS),
                               (wc.SHEET_REGISTRY, wc.REGISTRY_COLUMNS, wc.REGISTRY_GROUPS)):
        sheets.append({"name": name, "columns": len(cols), "groups": [g.title for g in groups], "first": cols[0].header})
    facts["workbook"] = {"sheet_names": list(wc.SHEET_NAMES), "sheets": sheets}

    # Golden cases.
    pub_cases = []
    for folder in sorted((ROOT / "tests/golden/publisher/cases").iterdir()):
        if (folder / "case.yaml").is_file():
            meta = yaml.safe_load(read(folder / "case.yaml")) or {}
            pub_cases.append({"name": folder.name, "description": " ".join(str(meta.get("description", "")).split())})
    real_cases = []
    for folder in sorted((ROOT / "tests/golden/realizer/cases").iterdir()):
        if (folder / "case.yaml").is_file():
            meta = yaml.safe_load(read(folder / "case.yaml")) or {}
            real_cases.append({"name": folder.name, "id": meta.get("id", ""), "description": " ".join(str(meta.get("description", "")).split())})
    facts["golden"] = {"publisher": pub_cases, "realizer_e2e": real_cases, "publisher_count": len(pub_cases), "realizer_e2e_count": len(real_cases)}

    # Command lines.
    from publisher.cli import publish as pub_cli
    from realizer.cli import realize as real_cli
    facts["cli"] = {
        "publisher": cli_rows(capture_parser(pub_cli.parse_args, [])),
        "realizer": cli_rows(capture_parser(real_cli.parse_args, [])),
        "realizer_backtest": cli_rows(capture_parser(real_cli.parse_backtest_args, [])),
    }

    # Contracts.
    facts["ledger_event_types"] = [e.value for e in real_contract.EventType]
    pkg_schema = json.loads(read(ROOT / "schema/recommendation-package.schema.json"))
    rpkg_schema = json.loads(read(ROOT / "schema/realization-package.schema.json"))
    facts["schema"] = {"package_required": list(pkg_schema.get("required", [])), "rpkg_required": list(rpkg_schema.get("required", []))}
    facts["hash_hex_length"] = rc.HASH_HEX_LENGTH
    return facts


# ---------------------------------------------------------------- the worked example (golden ge12) and its arithmetic

def load_example() -> Dict[str, Any]:
    case = ROOT / "tests/golden/realizer/cases" / EXAMPLE_CASE
    if not case.is_dir():
        raise BuildError(f"example case {EXAMPLE_CASE} not found")
    real = case / "expected/realizer"
    rpkg_path = next((real / "realization/packages").glob("RPKG-*.json"))
    ledger_path = next((real / "realization/ledger").glob("RPKG-*.jsonl"))
    measurement = json.loads(read(real / "measurement.json"))
    trimmed = dict(measurement)
    if isinstance(trimmed.get("daily_cost"), list) and len(trimmed["daily_cost"]) > 4:
        n = len(trimmed["daily_cost"])
        trimmed["daily_cost"] = trimmed["daily_cost"][:3] + [f"... {n - 3} more daily rows elided in the guide (see expected/realizer/measurement.json)"]
    return {
        "case": yaml.safe_load(read(case / "case.yaml")),
        "pkg": json.loads(read(case / "expected/publisher/package.json")),
        "rpkg": json.loads(read(rpkg_path)),
        "ledger": [json.loads(line) for line in read(ledger_path).splitlines() if line.strip()],
        "measurement": trimmed,
        "observation": json.loads(read(real / "observation.json")),
        "paths": {"case": rel(case), "rpkg": rel(rpkg_path), "ledger": rel(ledger_path)},
    }


class Checks:
    def __init__(self) -> None:
        self.items: List[Tuple[str, bool, str]] = []

    def close(self, name: str, got: float, want: float, tol: float = 0.011) -> bool:
        ok = abs(float(got) - float(want)) <= tol
        self.items.append((name, ok, f"computed {got:.6g}, published {want:.6g}"))
        return ok

    def equal(self, name: str, got: Any, want: Any) -> bool:
        ok = got == want
        self.items.append((name, ok, f"computed {got!r}, published {want!r}"))
        return ok


def worked_examples(ex: Dict[str, Any], checks: Checks) -> Dict[str, Any]:
    """Every number a formula card shows, recomputed from the example's own inputs; each recomputation is a check."""
    t = Thresholds()
    pkg, rpkg = ex["pkg"], ex["rpkg"]
    w: Dict[str, Any] = {}

    # Candidate economics: savings = baseline - cost, per track and component; low pairs with the high cost.
    cand = get_path(pkg, "candidates[knob=auto_stop_minutes]")
    econ = cand["independent_economics"]
    base = econ["baseline_365d"]
    for comp, bkey in (("dbu", "dbu_cost"), ("aws", "aws_cost"), ("total", "total_cost")):
        for track, cost_track in (("low", "high"), ("base", "base"), ("high", "low")):
            got = base[bkey] - econ["estimated_cost_365d"][comp][cost_track]
            checks.close(f"auto-stop {comp} savings {track} = baseline - cost.{cost_track}", got, econ["savings_365d"][comp][track])
    for track in ("low", "base", "high"):
        checks.close(f"auto-stop total = dbu + aws ({track})", econ["savings_365d"]["dbu"][track] + econ["savings_365d"]["aws"][track], econ["savings_365d"]["total"][track])

    # Auto-stop factor: cost = baseline x max(0, 1 - share_r), share_r = realized * r + fixed, r = the realization factors.
    r_opt, r_base, r_cons = t.realization_optimistic, t.realization_base, t.realization_conservative
    b = base["dbu_cost"]
    share = {track: 1 - econ["estimated_cost_365d"]["dbu"][cost_track] / b for track, cost_track in (("optimistic", "low"), ("base", "base"), ("conservative", "high"))}
    realized = (share["optimistic"] - share["conservative"]) / (r_opt - r_cons)
    fixed = share["optimistic"] - realized * r_opt
    predicted_base = realized * r_base + fixed
    checks.close("auto-stop base share = realization_base x realized + fixed", predicted_base, share["base"], tol=0.0005)
    w["autostop"] = {
        "baseline_dbu": money(b),
        "cost_low": money(econ["estimated_cost_365d"]["dbu"]["low"]), "cost_base": money(econ["estimated_cost_365d"]["dbu"]["base"]),
        "cost_high": money(econ["estimated_cost_365d"]["dbu"]["high"]),
        "share_optimistic": f"{share['optimistic']:.4f}", "share_base": f"{share['base']:.4f}", "share_conservative": f"{share['conservative']:.4f}",
        "realized": f"{realized:.4f}", "fixed": f"{fixed:+.5f}", "predicted_base": f"{predicted_base:.4f}",
        "factor_base": f"{1 - share['base']:.4f}",
        "r_optimistic": f"{r_opt:.2f}", "r_base": f"{r_base:.2f}", "r_conservative": f"{r_cons:.2f}",
        "savings_dbu_base": money(econ["savings_365d"]["dbu"]["base"]),
        "current": cand["current_value"], "target": cand["recommended_value"],
    }

    # Confidence: weights and caps come from Thresholds.
    def confidence(c: Dict[str, Any], label: str) -> Dict[str, str]:
        comp = c["confidence"]["components"]
        raw = (t.confidence_base_weight + t.confidence_coverage_weight * comp["coverage"] + t.confidence_sample_weight * comp["sample"]
               + t.confidence_stability_weight * (comp.get("stability") or 0.0) + t.confidence_epoch_weight * comp["epoch_maturity"]
               + t.confidence_signal_weight * comp["signal"])
        cap = {"ACTIONABLE": t.confidence_cap_actionable, "TEST_REQUIRED": t.confidence_cap_test_required}.get(c["status"], t.confidence_cap_default)
        score = round(min(max(raw, 0.0), cap), 3)
        checks.close(f"{label} confidence = min(weighted sum, cap)", score, c["confidence"]["score"], tol=0.0006)
        level = "HIGH" if score >= t.confidence_high_threshold else "MEDIUM" if score >= t.confidence_medium_threshold else "LOW"
        checks.equal(f"{label} confidence level", level, c["confidence"]["level"])
        return {"raw": f"{raw:.4f}", "cap": f"{cap:.2f}", "score": f"{score:.3f}", "level": level, "status": c["status"],
                **{k: f"{v:.3f}" for k, v in comp.items() if v is not None}}

    w["confidence_autostop"] = confidence(cand, "auto-stop")
    w["confidence_size"] = confidence(get_path(pkg, "candidates[knob=warehouse_size]"), "size")
    w["weights"] = {k: f"{getattr(t, 'confidence_' + k + '_weight'):.2f}" for k in ("base", "coverage", "sample", "stability", "epoch", "signal")}

    # Scenario chain: running savings accumulate step savings; the scenario total is the last running value.
    scn = get_path(pkg, f"scenarios[scenario_id={pkg['recommended_plan']['scenario_id']}]")
    priced = [s for s in scn["execution_plan"] if s.get("projected_economics")]
    running = 0.0
    chain = []
    for step in priced:
        running += step["projected_economics"]["step_savings_365d"]["total"]["base"]
        checks.close(f"step {step['step']} running total base = sum of step savings", running, step["projected_economics"]["running_savings_365d"]["total"]["base"])
        chain.append({"step": step["step"], "knob": step["knob"], "state": step["execution_state"],
                      "step_base": money(step["projected_economics"]["step_savings_365d"]["total"]["base"]),
                      "step_low": money(step["projected_economics"]["step_savings_365d"]["total"]["low"]),
                      "running_base": money(step["projected_economics"]["running_savings_365d"]["total"]["base"])})
    checks.close("scenario savings = last running total", running, scn["portfolio_economics"]["savings_365d"]["total"]["base"])
    w["chain"] = chain

    # Package envelope: low = min of scenario lows, base = recommended scenario base, high = max of scenario highs.
    pe = pkg["package_economics"]
    included = [get_path(pkg, f"scenarios[scenario_id={sid}]") for sid in pe["included_scenario_ids"]]
    lows = {s["scenario_id"]: s["portfolio_economics"]["savings_365d"]["total"]["low"] for s in included}
    highs = {s["scenario_id"]: s["portfolio_economics"]["savings_365d"]["total"]["high"] for s in included}
    checks.close("package low = min(scenario lows)", min(lows.values()), pe["estimated_savings_365d"]["low"])
    checks.close("package high = max(scenario highs)", max(highs.values()), pe["estimated_savings_365d"]["high"])
    checks.close("package base = recommended scenario base", scn["portfolio_economics"]["savings_365d"]["total"]["base"], pe["estimated_savings_365d"]["base"])
    w["envelope"] = {"scenarios": [{"id": s["scenario_id"], "type": s["scenario_type"], "status": s["status"],
                                    "low": money(lows[s["scenario_id"]]), "base": money(s["portfolio_economics"]["savings_365d"]["total"]["base"]),
                                    "high": money(highs[s["scenario_id"]])} for s in included],
                     "low": money(pe["estimated_savings_365d"]["low"]), "base": money(pe["estimated_savings_365d"]["base"]), "high": money(pe["estimated_savings_365d"]["high"])}

    # Package identity: PKG- = first 24 hex of SHA-256 over the canonical hash payload.
    payload = rc.package_hash_payload_from_document(pkg)
    canonical = rc.canonical_json_for_hash(payload)
    full = rc.sha256_hex(payload)
    checks.equal("full package hash = SHA-256(canonical payload)", full, pkg["provenance"]["package_hash_full_sha256"])
    checks.equal("PKG id = 'PKG-' + first 24 hex", rc.content_id("PKG", payload), pkg["recommendation_package_id"])
    w["identity"] = {"payload_keys": sorted(payload.keys()), "canonical_prefix": canonical[:220] + " …", "canonical_length": f"{len(canonical):,}",
                     "full": full, "id": pkg["recommendation_package_id"]}
    w["hash_payload"] = json.loads(canonical)

    # Realizer: the auto-stop replay and the realized economics.
    e = rpkg["economics"]
    m = e["measurement"]
    rp = m["replay"]
    dec = lambda s: float(Decimal(str(s)))  # noqa: E731
    dbu_saving = dec(rp["net_cluster_minutes"]) * dec(rp["dbu_cost_per_cluster_minute"])
    aws_saving = dec(rp["net_cluster_minutes"]) / 60 * dec(rp["aws_cluster_hourly_price"])
    checks.close("replay DBU saving = net cluster-minutes x DBU cost per cluster-minute", dbu_saving, dec(e["dbu_realized"]))
    checks.close("replay AWS saving = net cluster-minutes / 60 x AWS hourly price", aws_saving, dec(e["aws_economic_realized"]))
    observed_aws = dec(m["post_window"]["running_cluster_minutes"]) / 60 * dec(rp["aws_cluster_hourly_price"])
    observed_new = dec(m["post_window"]["billed_cost"]) + observed_aws
    checks.close("observed new cost = billed DBU cost + modeled AWS for running cluster-minutes", observed_new, dec(e["observed_new_cost"]))
    cf_old = observed_new + dbu_saving + aws_saving
    checks.close("counterfactual old cost = observed new cost + replayed saving", cf_old, dec(e["counterfactual_old_cost"]))
    realized_period = dec(e["counterfactual_old_cost"]) - dec(e["observed_new_cost"])
    checks.close("realized = counterfactual old cost - observed new cost", realized_period, dec(e["realized_savings_period"]))
    annual = realized_period * 365 / dec(e["period_days"])
    checks.close("annualized = realized x 365 / period days", annual, dec(e["annualized_realized_savings"]), tol=0.02)
    rng = e["expected_range"]
    ratio = dec(e["annualized_realized_savings"]) / dec(e["expected_same_scope_annual_savings"])
    checks.close("realization ratio = annualized / expected", ratio, dec(e["realization_ratio"]), tol=0.0005)
    from realizer.estimator import economics as real_economics
    review_line = dec(rng["high"]) * float(real_economics.REVIEW_ABOVE_HIGH)
    verdict = "REALIZED" if dec(e["annualized_realized_savings"]) >= dec(rng["low"]) else "UNDER_REALIZED"
    checks.equal("classification against the published range", verdict, e["classification_detail"])
    cc = m["cross_check"]
    weekly = [dec(x) for x in cc["pre_weekly_costs"]]
    noise = statistics.pstdev(weekly) * math.sqrt(m["post_window"]["days"] / 7)
    checks.close("cross-check noise = pstdev(pre weekly) x sqrt(post days / 7)", noise, dec(cc["noise"]))
    diff = abs(dec(cc["billed_dbu_reduction"]) - dec(cc["replay_dbu_saving"])) / dec(cc["replay_dbu_saving"]) * 100
    checks.close("cross-check difference % = |billed - replay| / replay", diff, dec(cc["difference_pct"]))
    checks.equal("RPKG id derives from the PKG id", real_contract.make_rpkg_id(pkg["recommendation_package_id"]), rpkg["realization_package_id"])
    w["realizer"] = {
        "net_minutes": rp["net_cluster_minutes"], "dbu_per_min": rp["dbu_cost_per_cluster_minute"], "aws_hourly": rp["aws_cluster_hourly_price"],
        "added_idle": rp["added_idle_cluster_minutes"], "avoided_starts": rp["avoided_starts"], "restart_cost": rp["restart_cost_cluster_minutes"],
        "replayed_target": rp["replayed_target_minutes"],
        "dbu_saving": money(dbu_saving), "aws_saving": money(aws_saving),
        "post_billed": money(dec(m["post_window"]["billed_cost"])), "post_minutes": m["post_window"]["running_cluster_minutes"],
        "observed_aws": money(observed_aws), "observed_new": money(observed_new), "cf_old": money(cf_old),
        "realized": money(realized_period), "days": e["period_days"], "annual": money(dec(e["annualized_realized_savings"])),
        "expected": money(dec(e["expected_same_scope_annual_savings"])), "ratio": f"{ratio:.3f}",
        "low": money(dec(rng["low"])), "high": money(dec(rng["high"])), "review_line": money(review_line), "verdict": verdict,
        "weekly": ", ".join(money(x) for x in weekly), "noise": money(dec(cc["noise"])), "billed_reduction": money(dec(cc["billed_dbu_reduction"])),
        "replay_dbu": money(dec(cc["replay_dbu_saving"])), "diff_pct": cc["difference_pct"], "cross_check": cc["outcome"],
        "pre_billed": money(dec(m["pre_window"]["billed_cost"])), "volume_ratio": m["volume_ratio"],
        "rpkg_id": rpkg["realization_package_id"],
    }
    w["checks_total"] = len(checks.items)
    return w


# ---------------------------------------------------------------- contract field docs for the explorers

def contract_fields(doc: str, aliases: Dict[str, str], remap: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    """path -> {type, required, nullable, text, ref, section} from a contract document's field tables.

    ``aliases`` names the path of a heading without a backticked path; ``remap`` sends a backticked type or field name
    to every place it occurs in a package (CostBreakdown appears under several parents)."""
    fields: Dict[str, Dict[str, str]] = {}
    for table in md_tables(ROOT / doc):
        if not table["header"] or table["header"][0] != "Field" or not table["trail"]:
            continue
        heading = table["trail"][-1]
        m = re.search(r"`([^`]+)`", heading)
        title = re.sub(r"^[\d.]+\s*", "", heading).strip()
        if title in aliases:
            base = aliases[title]
        elif m:
            base = m.group(1)
        else:
            continue
        cols = table["header"]
        for row in table["rows"]:
            cells = dict(zip(cols, row))
            name = strip_ticks(row[0])
            if not re.fullmatch(r"[\w\[\]]+", name):
                continue
            text_key = next((c for c in cols if c.lower().startswith(("semantics", "constraints", "meaning"))), cols[-1])
            entry = {"type": strip_ticks(cells.get("Type", "")), "required": cells.get("Req.", ""),
                     "nullable": cells.get("Nullable", "").replace("*", ""),
                     "text": re.sub(r"`", "", cells.get(text_key, "")), "ref": link_to(doc) + "#" + slug(heading), "section": heading.replace("`", "")}
            for b in remap.get(base, [base]):
                fields[f"{b}.{name}" if b else name] = entry
    return fields


# ---------------------------------------------------------------- fragment rendering

PLACEHOLDER = re.compile(r"\{\{(\w+):([^{}]*)\}\}")
STATUS_TONE = {
    "ACTIONABLE": "good", "READY": "good", "PASSED": "good", "COMPATIBLE": "good", "AGREES": "good", "CONFIRMED": "good",
    "TEST_REQUIRED": "warn", "READY_FOR_CANARY": "warn", "PROVISIONAL": "warn", "REVIEW": "warn", "PENDING_REEVALUATION": "warn", "MONITORING": "warn",
    "INCONCLUSIVE": "warn", "PROVISIONAL_MODELED_RANGE": "warn", "PRE_CANARY_MODELED_RANGE": "warn",
    "BLOCKED": "bad", "VALIDATION_FAILED": "bad", "REJECTED": "bad", "DISAGREES": "bad", "UNDER_REALIZED": "bad", "NOT_ATTRIBUTABLE": "bad",
    "NO_CHANGE": "hold", "INSUFFICIENT_EVIDENCE": "hold", "REQUIRED": "hold", "NOT_APPLIED": "hold", "OBSERVABLE_ONLY": "hold", "INVALIDATED": "hold",
    "REALIZED": "real", "CLAIMABLE": "real", "HIGH_CONFIDENCE": "real", "MATURE": "real", "REPRESENTATIVE": "real", "APPLIED": "real",
    "RECOMMENDED": "pub", "ALTERNATIVE": "hold", "MODELED_RANGE": "pub",
}
REALIZER_PREFIXES = {"RPKG", "RLE", "RSNAP", "APP", "VAL", "MEAS", "CF", "REVID", "RERUN", "PSNAP"}


class Renderer:
    def __init__(self, facts: Dict[str, Any], example: Dict[str, Any], worked: Dict[str, Any], glossary: Dict[str, Any], explorers: Dict[str, Any]) -> None:
        self.facts, self.example, self.worked, self.glossary, self.explorers = facts, example, worked, glossary, explorers
        self.citations: Dict[Tuple[str, str], None] = {}
        self.errors: List[str] = []
        self.chapter = ""

    # -- formatting
    def fmt(self, value: Any, how: str) -> str:
        if how == "money":
            return money(value)
        if how == "int":
            return f"{int(value):,}"
        if how == "raw":
            return str(value)
        if how == "id":
            return self.idt(str(value))
        if how == "status":
            return self.status(str(value))
        if how == "json":
            return "<code>" + esc(json.dumps(value)) + "</code>"
        if how == "list":
            return ", ".join(f"<code>{esc(v)}</code>" for v in value)
        if how == "count":
            return str(len(value))
        if how.startswith("pct"):
            return f"{float(value):.{int(how[3:] or 1)}f}%"
        if isinstance(value, float) and value.is_integer():
            return esc(str(value))
        return esc(value)

    def idt(self, value: str) -> str:
        prefix, _, rest = value.partition("-")
        cls = "r" if prefix in REALIZER_PREFIXES else "p"
        return f'<span class="idt {cls}" title="{esc(value)}"><i>{esc(prefix)}-</i>{esc(rest)}</span>'

    def status(self, value: str) -> str:
        return f'<span class="chip {STATUS_TONE.get(value, "hold")}">{esc(value)}</span>'

    # -- placeholders
    def render(self, text: str, chapter: str) -> str:
        self.chapter = chapter
        return PLACEHOLDER.sub(self._one, text)

    def _one(self, m: "re.Match[str]") -> str:
        kind, arg = m.group(1), m.group(2).strip()
        try:
            handler = getattr(self, "p_" + kind)
        except AttributeError:
            self.errors.append(f"{self.chapter}: unknown placeholder kind {kind!r}")
            return m.group(0)
        try:
            return handler(arg)
        except (KeyError, IndexError, TypeError, ValueError, BuildError) as exc:
            self.errors.append(f"{self.chapter}: {{{{{kind}:{arg}}}}} -> {exc}")
            return f'<span class="chip bad">unresolved {esc(kind)}:{esc(arg)}</span>'

    def _split(self, arg: str) -> Tuple[str, str]:
        path, _, how = arg.partition("|")
        return path.strip(), how.strip()

    def p_fact(self, arg: str) -> str:
        path, how = self._split(arg)
        return self.fmt(get_path(self.facts, path), how)

    def p_pkg(self, arg: str) -> str:
        path, how = self._split(arg)
        return self.fmt(get_path(self.example["pkg"], path), how)

    def p_rpkg(self, arg: str) -> str:
        path, how = self._split(arg)
        return self.fmt(get_path(self.example["rpkg"], path), how)

    def p_ex(self, arg: str) -> str:
        path, how = self._split(arg)
        return self.fmt(get_path(self.worked, path), how)

    def p_status(self, arg: str) -> str:
        return self.status(arg)

    def p_id(self, arg: str) -> str:
        return self.idt(arg)

    def p_cite(self, arg: str) -> str:
        target, _, label = arg.partition("|")
        path, _, symbol = target.strip().partition("::")
        path, symbol = path.strip(), symbol.strip()
        self.citations[(path, symbol)] = None
        shown = label.strip() or symbol or path
        title = path + (f" :: {symbol}" if symbol else "")
        return f'<a class="cite" href="{esc(link_to(path))}" title="{esc(title)}">{esc(shown)}</a>'

    def p_doc(self, arg: str) -> str:
        target, _, label = arg.partition("|")
        path, _, anchor = target.strip().partition("#")
        self.citations[(path, "")] = None
        href = link_to(path) + (f"#{anchor}" if anchor else "")
        return f'<a href="{esc(href)}">{esc(label.strip() or path)}</a>'

    def p_term(self, arg: str) -> str:
        key, _, text = arg.partition("|")
        key = key.strip()
        if key not in self.glossary:
            raise KeyError(f"glossary term {key!r}")
        term = self.glossary[key]["term"]
        first = term.split(" ")[0]
        # "Cost profile" reads "cost profile" mid-sentence; names and acronyms (RecommendationPackage, DBU) keep their case.
        inline = term[0].lower() + term[1:] if first.isalpha() and len(first) > 1 and first[1:].islower() else term
        return f'<span class="term" data-term="{esc(key)}">{esc(text.strip() or inline)}</span>'

    def p_explorer(self, arg: str) -> str:
        parts = [p.strip() for p in arg.split("|")]
        name = parts[0]
        if name not in self.explorers:
            raise KeyError(f"explorer {name!r}")
        path = resolve_selectors(self.explorers[name]["document"], parts[1]) if len(parts) > 1 and parts[1] else ""
        depth = parts[2] if len(parts) > 2 and parts[2] else "1"
        select = resolve_selectors(self.explorers[name]["document"], parts[3]) if len(parts) > 3 and parts[3] else ""
        if path:
            get_path(self.explorers[name]["document"], path)
        return (f'<div data-src="{esc(name)}" data-path="{esc(path)}" data-open="{esc(depth)}"'
                + (f' data-select="{esc(select)}"' if select else "") + "></div>")

    def p_range(self, arg: str) -> str:
        """{{range:source|path-to-{low,base,high}|label|obs-path}} -> a range bar (source: pkg, rpkg or ex)."""
        parts = [p.strip() for p in arg.split("|")]
        doc = {"pkg": self.example["pkg"], "rpkg": self.example["rpkg"], "ex": self.worked}[parts[0]]
        r = get_path(doc, parts[1])
        label = parts[2] if len(parts) > 2 else ""
        obs = ""
        cls = "range"
        if len(parts) > 3 and parts[3]:
            obs_src, _, obs_path = parts[3].partition(":")
            obs_doc = {"pkg": self.example["pkg"], "rpkg": self.example["rpkg"], "ex": self.worked}[obs_src]
            obs = f' data-obs="{float(Decimal(str(get_path(obs_doc, obs_path))))}"'
            cls = "range real"
        lo, ba, hi = (float(Decimal(str(r[k]))) for k in ("low", "base", "high"))
        return f'<div class="{cls}" data-low="{lo}" data-base="{ba}" data-high="{hi}" data-unit="USD / year" data-label="{esc(label)}"{obs}></div>'

    def p_table(self, arg: str) -> str:
        name = arg.strip()
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise KeyError(f"table {name!r}")
        return fn()

    # -- generated tables
    def _table(self, head: Sequence[str], rows: Sequence[Sequence[str]], *, table_id: str = "", tall: bool = False, row_attrs: Optional[Sequence[str]] = None) -> str:
        out = [f'<div class="table-wrap{" tall" if tall else ""}"><table class="data"' + (f' id="{table_id}"' if table_id else "") + "><thead><tr>"]
        out += [f"<th>{esc(h)}</th>" for h in head]
        out.append("</tr></thead><tbody>")
        for i, row in enumerate(rows):
            attrs = row_attrs[i] if row_attrs else ""
            out.append(f"<tr{attrs}>" + "".join(row) + "</tr>")
        out.append("</tbody></table></div>")
        return "".join(out)

    def t_versions(self) -> str:
        v = self.facts["versions"]
        rows = [[f"<td>{esc(k)}</td>", f"<td><code>{esc(val)}</code></td>"] for k, val in v["publisher"].items()]
        rows += [["<td>reporter_contract</td>", f"<td><code>{esc(v['reporter_contract'])}</code></td>"],
                 ["<td>workbook_contract</td>", f"<td><code>{esc(v['workbook_contract'])}</code></td>"]]
        rrows = [[f"<td>{esc(k)}</td>", f"<td><code>{esc(val)}</code></td>"] for k, val in {**v["realizer"], **v["realizer_formats"]}.items()]
        return ('<div class="grid c2"><div class="table-wrap"><table class="data"><thead><tr><th>Publisher component</th><th>Version</th></tr></thead><tbody>'
                + "".join("<tr>" + "".join(r) + "</tr>" for r in rows) + "</tbody></table></div>"
                + '<div class="table-wrap"><table class="data"><thead><tr><th>Realizer component or format</th><th>Version</th></tr></thead><tbody>'
                + "".join("<tr>" + "".join(r) + "</tr>" for r in rrows) + "</tbody></table></div></div>")

    def t_thresholds(self) -> str:
        rows, attrs, group = [], [], None
        for r in self.facts["threshold_rows"]:
            if r["group"] != group:
                group = r["group"]
                rows.append([f'<td colspan="5">{esc(group)}</td>'])
                attrs.append(' class="grp"')
            prof = r["profiles"]
            cons = f'<td class="num diff">{esc(prof["conservative"])}</td>' if prof else '<td class="num muted">·</td>'
            aggr = f'<td class="num diff">{esc(prof["aggressive"])}</td>' if prof else '<td class="num muted">·</td>'
            rows.append([f"<td><code>{esc(r['name'])}</code></td>", f'<td class="num">{esc(r["default"])}</td>', cons, aggr, f"<td>{r['meaning']}</td>"])
            attrs.append(f' data-group="{esc(group)}"')
        groups = sorted({r["group"] for r in self.facts["threshold_rows"]}, key=[r["group"] for r in self.facts["threshold_rows"]].index)
        chips = "".join(f'<button class="btn" type="button" data-group="{esc(g)}" aria-pressed="false">{esc(g)}</button>' for g in groups)
        return (f'<div class="filter" data-filter-for="thr-table"><input id="thr-q" type="search" placeholder="Filter {len(self.facts["threshold_rows"])} parameters" aria-label="Filter parameters"><span class="count"></span><div class="filter">{chips}</div></div>'
                + self._table(["Parameter", "Balanced (default)", "Conservative", "Aggressive", "Meaning"], rows, table_id="thr-table", tall=True, row_attrs=attrs))

    def t_profiles(self) -> str:
        rows = []
        for group, names in self.facts["cost_profile_groups"].items():
            for n in names:
                p = self.facts["profiles"]
                rows.append([f"<td>{esc(group)}</td>", f"<td><code>{esc(n)}</code></td>"] + [f'<td class="num">{esc(p[k][n])}</td>' for k in ("conservative", "balanced", "aggressive")])
        return self._table(["Technique", "Threshold", "Conservative", "Balanced", "Aggressive"], rows)

    def t_product_constants(self) -> str:
        rows = [[f"<td><code>{esc(r['name'])}</code></td>", f'<td class="num">{esc(r["value"])}</td>', f"<td>{r['meaning']}</td>"] for r in self.facts["product_constant_rows"]]
        return self._table(["Constant", "Value", "Meaning"], rows)

    def t_scope(self) -> str:
        rows = []
        for name, s in self.facts["scope_scenarios"].items():
            for tech, knob in s["techniques"].items():
                rows.append([f"<td><code>{esc(name)}</code></td>", f"<td><code>{esc(s['scenario_type'])}</code></td>", f"<td><code>{esc(tech)}</code></td>", f"<td><code>{esc(knob)}</code></td>"])
        return self._table(["Scope scenario", "Contract scenario type", "Technique (scope key)", "Knob"], rows)

    def t_techniques(self) -> str:
        rows = [[f"<td>{r['knob']}</td>", f"<td>{r['ids']}</td>", f"<td>{r['algorithm']}</td>", f"<td>{r['blocker']}</td>"] for r in self.facts["technique_rows"]]
        return self._table(["Knob", "Technique IDs", "Algorithm", "Primary blocker / validation"], rows, tall=True)

    def t_blockers(self) -> str:
        rows, attrs = [], []
        for b in self.facts["blockers"]:
            cases = " ".join(f"<code>{esc(c)}</code>" for c in b["cases"]) or '<span class="muted">none</span>'
            rows.append([f'<td id="b-{esc(b["code"])}"><code>{esc(b["code"])}</code><div class="small muted">{esc(b["group"])}</div></td>',
                         f"<td>{b['meaning']}</td>", f"<td>{b['fires']}</td>", f"<td>{b['status']}</td>", f'<td class="small">{cases}</td>'])
            attrs.append(f' data-group="{esc(b["group"])}"')
        groups = []
        for b in self.facts["blockers"]:
            if b["group"] not in groups:
                groups.append(b["group"])
        chips = "".join(f'<button class="btn" type="button" data-group="{esc(g)}" aria-pressed="false">{esc(g)}</button>' for g in groups)
        return (f'<div class="filter" data-filter-for="blk-table"><input id="blk-q" type="search" placeholder="Type a code, a word or a status ({len(rows)} codes)" aria-label="Filter blocker codes"><span class="count"></span></div>'
                f'<div class="filter" data-filter-for="blk-table">{chips}<input hidden aria-hidden="true"></div>'
                + self._table(["Code", "Meaning", "Fires when", "Resulting status", "Golden cases that emit it"], rows, table_id="blk-table", tall=True, row_attrs=attrs))

    def t_workbook(self) -> str:
        rows = [[f"<td>{esc(s['name'])}</td>", f'<td class="num">{s["columns"]}</td>', f"<td>{esc(', '.join(s['groups']))}</td>"] for s in self.facts["workbook"]["sheets"]]
        return self._table(["Sheet", "Columns", "Column groups (banners)"], rows)

    def _cli(self, key: str) -> str:
        repeat = ' <span class="chip hold">repeatable</span>'
        rows = [[f"<td><code>{esc(r['flag'])}</code></td>", f"<td><code>{esc(r['arg'])}</code></td>" if r["arg"] else "<td></td>",
                 f"<td>{esc(r['help'])}{repeat if r['repeat'] else ''}</td>"] for r in self.facts["cli"][key]]
        return self._table(["Flag", "Value", "What it does"], rows, tall=True)

    def t_cli_publisher(self) -> str:
        return self._cli("publisher")

    def t_cli_realizer(self) -> str:
        return self._cli("realizer")

    def t_cli_backtest(self) -> str:
        return self._cli("realizer_backtest")

    def t_golden_publisher(self) -> str:
        rows = [[f"<td><code>{esc(c['name'])}</code></td>", f"<td>{esc(c['description'])}</td>"] for c in self.facts["golden"]["publisher"]]
        return self._table(["Publisher golden case", "What it pins"], rows, tall=True)

    def t_golden_realizer(self) -> str:
        rows = [[f"<td><code>{esc(c['name'])}</code></td>", f"<td>{esc(c['description'])}</td>"] for c in self.facts["golden"]["realizer_e2e"]]
        return self._table(["End-to-end case (Publisher to Realizer)", "Story"], rows, tall=True)

    def t_ledger(self) -> str:
        """The example's ledger as a timeline."""
        items = []
        for e in self.example["ledger"]:
            keys = ", ".join(sorted(e["payload"].keys())[:6])
            ids = " ".join(self.idt(e[k]) for k in ("application_episode_id", "validation_episode_id", "measurement_id") if e.get(k))
            items.append(f'<li><span class="when">{esc(e["effective_at_utc"])}</span><span class="what">{esc(e["event_type"])}</span>'
                         f'<span class="detail">{self.idt(e["ledger_entry_id"])} {ids}</span><span class="detail small muted">payload: {esc(keys)}</span></li>')
        return '<ol class="tl">' + "".join(items) + "</ol>"

    def t_ledger_events(self) -> str:
        return " ".join(f"<code>{esc(e)}</code>" for e in self.facts["ledger_event_types"])

    def t_envelope(self) -> str:
        env = self.worked["envelope"]
        rows = [[f"<td>{self.idt(s['id'])}</td>", f"<td><code>{esc(s['type'])}</code></td>", f"<td>{self.status(s['status'])}</td>",
                 f'<td class="num">{s["low"]}</td>', f'<td class="num">{s["base"]}</td>', f'<td class="num">{s["high"]}</td>'] for s in env["scenarios"]]
        rows.append(['<td colspan="3"><b>Package envelope</b> (min of lows, recommended base, max of highs)</td>',
                     f'<td class="num"><b>{env["low"]}</b></td>', f'<td class="num"><b>{env["base"]}</b></td>', f'<td class="num"><b>{env["high"]}</b></td>'])
        return self._table(["Scenario", "Type", "Status", "Low", "Base", "High"], rows)

    def t_chain(self) -> str:
        rows = [[f'<td class="num">{c["step"]}</td>', f"<td><code>{esc(c['knob'])}</code></td>", f"<td>{self.status(c['state'])}</td>",
                 f'<td class="num">{c["step_low"]}</td>', f'<td class="num">{c["step_base"]}</td>', f'<td class="num"><b>{c["running_base"]}</b></td>'] for c in self.worked["chain"]]
        return self._table(["Step", "Knob", "Execution state", "Step savings (low)", "Step savings (base)", "Running total (base)"], rows)

    def t_candidates(self) -> str:
        rows = []
        for c in self.example["pkg"]["candidates"]:
            s = c["independent_economics"].get("savings_365d") or {}
            tot = s.get("total") if s else None
            econ = f'{money(tot["low"])} / {money(tot["base"])} / {money(tot["high"])}' if tot else '<span class="muted">null</span>'
            rows.append([f"<td><code>{esc(c['knob'])}</code></td>", f"<td>{esc(c['current_value'])} → {esc(c['recommended_value'])}</td>",
                         f"<td>{self.status(c['status'])}</td>", f"<td><code>{esc(c['technique_id'])}</code></td>",
                         f'<td class="num">{econ}</td>', f"<td>{esc(c['confidence']['score'])} {esc(c['confidence']['level'])}</td>"])
        return self._table(["Knob", "Change", "Status", "Technique", "Isolated total savings low / base / high", "Confidence"], rows)

    def t_checks(self) -> str:
        good, bad = '<span class="chip good">match</span>', '<span class="chip bad">MISMATCH</span>'
        rows = [[f"<td>{esc(n)}</td>", f"<td>{good if ok else bad}</td>", f'<td class="small mono">{esc(d)}</td>']
                for n, ok, d in self.worked["_checks"]]
        return self._table(["Recomputed from the example's inputs", "Result", "Values"], rows, tall=True)

    def t_glossary(self) -> str:
        items = []
        for key in sorted(self.glossary, key=lambda k: self.glossary[k]["term"].lower()):
            g = self.glossary[key]
            home = f' <a class="small" href="{esc(link_to(g["home"]))}">{esc(g["home"].split("/")[-1])}</a>' if g.get("home") else ""
            items.append(f'<dt id="g-{esc(key)}">{esc(g["term"])}</dt><dd>{esc(g["definition"])}{home}</dd>')
        return '<dl class="gloss">' + "".join(items) + "</dl>"

    def t_docs_map(self) -> str:
        rows = []
        for table in md_tables(ROOT / "docs/README.md"):
            if table["header"][:3] == ["Type", "Product", "Document"]:
                kind = prod = ""
                for row in table["rows"]:
                    kind = row[0].strip("` ") or kind
                    prod = row[1] or prod
                    rows.append([f"<td><code>{esc(kind)}</code></td>", f"<td>{esc(prod)}</td>", f"<td>{md_inline(row[2], 'docs/README.md')}</td>", f"<td>{md_inline(row[3], 'docs/README.md')}</td>"])
        return self._table(["Type", "Product", "Document", "Read it when"], rows, tall=True)


# ---------------------------------------------------------------- assembly

def load_yaml(path: Path) -> Any:
    return yaml.safe_load(read(path)) or {}


def fingerprints(chapters: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    out = {}
    for ch in chapters:
        files: Dict[str, str] = {}
        for src in ch["sources"]:
            found = files_under(src)
            if not found:
                raise BuildError(f"chapter {ch['id']}: source {src} does not exist")
            for p in found:
                files[rel(p)] = file_hash(p)
        out[ch["id"]] = dict(sorted(files.items()))
    return out


def build() -> Tuple[str, Dict[str, Any]]:
    cmap = load_yaml(SKILL / "references/chapter-map.yaml")["chapters"]
    glossary = load_yaml(SKILL / "references/glossary.yaml")
    notes = load_yaml(SKILL / "assets/notes.yaml")
    for name in ("pkg", "rpkg", "ledger", "measurement", "hash_payload"):
        extra = SKILL / "assets/notes" / f"{name}.yaml"
        if extra.is_file():
            notes[name] = {**(notes.get(name) or {}), **load_yaml(extra)}
    facts = collect_facts()
    example = load_example()
    checks = Checks()
    worked = worked_examples(example, checks)
    worked["_checks"] = checks.items

    explorers = {
        "pkg": {"title": f"RecommendationPackage · {EXAMPLE_CASE}", "document": example["pkg"],
                "fields": contract_fields("docs/contracts/shared/reporter-json-contract.md", notes.get("contract_heading_aliases", {}), notes.get("contract_path_remap", {})),
                "notes": notes.get("pkg", {}), "defs": [r"^candidates\[\d+\]\.candidate_id$", r"^scenarios\[\d+\]\.scenario_id$",
                                                         r"^warehouse\.current_epoch\.epoch_id$", r"^recommendation_package_id$"]},
        "rpkg": {"title": f"Realization package (RPKG) · {EXAMPLE_CASE}", "document": example["rpkg"], "fields": {},
                 "notes": notes.get("rpkg", {}), "defs": [r"^realization_package_id$", r"^snapshot_id$", r"^application\.active_episode_id$",
                                                           r"^validation\.validation_episode_id$", r"^source_recommendation\.recommendation_package_id$"]},
        "ledger": {"title": "Ledger (one JSON object per line)", "document": example["ledger"], "fields": {}, "notes": notes.get("ledger", {}),
                   "defs": [r"^\[\d+\]\.ledger_entry_id$"]},
        "measurement": {"title": "Measurement evidence (trimmed)", "document": example["measurement"], "fields": {}, "notes": notes.get("measurement", {}), "defs": []},
        "hash_payload": {"title": "Package hash payload (what the PKG- id hashes)", "document": worked["hash_payload"], "fields": {}, "notes": notes.get("hash_payload", {}), "defs": []},
    }
    renderer = Renderer(facts, example, worked, glossary, explorers)

    # Chapters.
    nav: List[Tuple[str, List[str]]] = []  # consecutive chapters of one group share a heading
    sections = []
    for number, ch in enumerate(cmap, 1):
        frag_path = SKILL / "assets/chapters" / f"{ch['id']}.html"
        if not frag_path.is_file():
            raise BuildError(f"missing fragment {rel(frag_path)}")
        body = renderer.render(read(frag_path), ch["id"])
        subs = re.findall(r'<h3 id="([\w-]+)">(.*?)</h3>', body)
        where = " ".join(f'<a href="{esc(link_to(d))}">{esc(d.replace("docs/", ""))}</a>' for d in ch["docs"])
        num = f"{number:02d}"
        sections.append(
            f'<section class="chapter" id="{esc(ch["id"])}" aria-labelledby="{esc(ch["id"])}-h">'
            f'<header class="ch-head"><div class="eyebrow"><span class="num">{num}</span><span>{esc(ch["group"])}</span></div>'
            f'<h2 id="{esc(ch["id"])}-h">{esc(ch["title"])}</h2></header>\n{body}\n'
            f'<p class="where"><b>Where this lives</b> {where}</p></section>')
        sub_nav = "".join(f'<a href="#{sid}">{re.sub(r"<[^>]+>", "", title)}</a>' for sid, title in subs)
        if not nav or nav[-1][0] != ch["group"]:
            nav.append((ch["group"], []))
        nav[-1][1].append(
            f'<div class="toc-item"><a href="#{esc(ch["id"])}"><span class="n">{num}</span><span>{esc(ch["title"])}</span></a>'
            + (f'<div class="sub">{sub_nav}</div>' if sub_nav else "") + "</div>")
    nav_html = "\n".join(f'<div class="toc-group"><span>{esc(g)}</span>{"".join(items)}</div>' for g, items in nav)

    v = facts["versions"]
    hero = (
        '<header class="hero"><div class="eyebrow">inframate-v1 · engineering field guide</div>'
        '<h1>How the warehouse optimizer thinks</h1>'
        '<p class="lede">The Publisher reads system tables and writes one locked RecommendationPackage per SQL warehouse. '
        'The Realizer takes one package after an owner applies a change and records what was actually saved. '
        'This guide walks both, layer by layer, with one real warehouse as the running example.</p>'
        '<div class="verrow">'
        f'<span class="ver pub"><span>Publisher</span><b>{esc(v["publisher"]["publisher_version"])}</b></span>'
        f'<span class="ver pub"><span>Reporter JSON</span><b>{esc(v["reporter_contract"])}</b></span>'
        f'<span class="ver pub"><span>Workbook</span><b>{esc(v["workbook_contract"])}</b></span>'
        f'<span class="ver real"><span>Realizer</span><b>{esc(v["realizer"]["realizer_version"])}</b></span>'
        f'<span class="ver real"><span>RPKG</span><b>{esc(v["realizer_formats"]["realization_package_contract"])}</b></span>'
        f'<span class="ver real"><span>Ledger</span><b>{esc(v["realizer_formats"]["ledger_contract"])}</b></span>'
        '</div>'
        f'<p class="stamp">Generated by the <code>teach-me</code> skill from the code, the reference documents and golden case <code>{EXAMPLE_CASE}</code>. '
        f'{worked["checks_total"]} worked-example calculations were recomputed at build time and match the published values. '
        'Do not edit this file; run <code>/teach-me refresh</code>.</p></header>')
    foot = ('<p>Generated file. Source: <code>.claude/skills/teach-me/</code> (chapter fragments, notes, glossary) plus the code it cites. '
            'Rebuild: <code>python .claude/skills/teach-me/scripts/build_guide.py --verify</code>.</p>'
            '<p>The code and its tests are authoritative, then the locked contracts; this guide only teaches and points (docs/README.md, Precedence).</p>')

    search_extra = [{"t": b["code"], "k": "Blocker code", "h": f"#b-{b['code']}"} for b in facts["blockers"]]
    search_extra += [{"t": r["name"], "k": "Threshold", "h": "#policy"} for r in facts["threshold_rows"]]
    data = {"glossary": glossary, "explorers": explorers, "id_families": notes.get("id_families", {}), "search_extra": search_extra}
    meta = {
        "generator_version": GENERATOR_VERSION,
        "example_case": EXAMPLE_CASE,
        "versions": {"publisher": v["publisher"]["publisher_version"], "reporter_contract": v["reporter_contract"],
                     "workbook_contract": v["workbook_contract"], "realizer": v["realizer"]["realizer_version"]},
        "chapters": [c["id"] for c in cmap],
        "fingerprints": fingerprints(cmap),
        "citations": [{"path": p, "symbol": s} for p, s in sorted(renderer.citations)],
    }

    def embed(obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    page = read(SKILL / "assets/template.html")
    for marker, value in (("<!--@NAV@-->", nav_html), ("<!--@HERO@-->", hero), ("<!--@CHAPTERS@-->", "\n".join(sections)),
                          ("<!--@FOOT@-->", foot), ("<!--@DATA@-->", embed(data)), ("<!--@META@-->", embed(meta))):
        if marker not in page:
            raise BuildError(f"template lost marker {marker}")
        page = page.replace(marker, value)
    state = {"meta": meta, "errors": renderer.errors, "checks": checks.items, "citations": renderer.citations, "glossary": glossary, "page": page}
    return page, state


# ---------------------------------------------------------------- modes

def committed_meta() -> Optional[Dict[str, Any]]:
    if not OUT.is_file():
        return None
    m = re.search(r'<script type="application/json" id="guide-meta">(.*?)</script>', read(OUT), re.S)
    return json.loads(m.group(1).replace("<\\/", "</")) if m else None


def verify(state: Dict[str, Any]) -> List[str]:
    problems = list(state["errors"])
    for name, ok, detail in state["checks"]:
        if not ok:
            problems.append(f"worked example drifted: {name} ({detail})")
    for path, symbol in state["citations"]:
        target = ROOT / path
        if not target.exists():
            problems.append(f"cited path does not exist: {path}")
            continue
        if symbol and target.is_file():
            pattern = rf"(def|class)\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*[:=]|\b{re.escape(symbol)}\s*=\s*"
            if not re.search(pattern, read(target), re.M):
                problems.append(f"cited symbol not found: {path} :: {symbol}")
    page = state["page"]
    # Repo paths written as plain <code> (not citations) must exist too; a pattern with <placeholder> or * is skipped.
    for path in sorted(set(re.findall(r"<code>((?:src|tests|docs|scripts|schema|examples|pricing)/[\w./-]+)</code>", page))):
        if not (ROOT / path.rstrip("/")).exists():
            problems.append(f"path in <code> does not exist: {path}")
    if re.search(r"\{\{\w+:", page):
        problems.append("unresolved placeholder left in the page")
    for m in re.finditer(r'<(script|link|img|iframe)\b[^>]*\b(src|href)="(https?:)?//', page):
        problems.append(f"external resource in the page: {m.group(0)[:80]}")
    for key in re.findall(r'data-term="([\w-]+)"', page):
        if key not in state["glossary"]:
            problems.append(f"unknown glossary term {key}")
    ids = re.findall(r'\sid="([^"]+)"', page)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate element ids: {', '.join(dupes[:10])}")
    for href in re.findall(r'href="#([^"]+)"', page):
        if href not in ids:
            problems.append(f"in-page link to a missing id: #{href}")
    return problems


def stale() -> int:
    old = committed_meta()
    if old is None:
        print("No committed guide yet: build every chapter.")
        return 0
    cmap = load_yaml(SKILL / "references/chapter-map.yaml")["chapters"]
    now = fingerprints(cmap)
    listed = 0
    for ch in cmap:
        before = old.get("fingerprints", {}).get(ch["id"])
        after = now[ch["id"]]
        if before == after:
            continue
        listed += 1
        before = before or {}
        changed = sorted(p for p in after if p in before and before[p] != after[p])
        added = sorted(p for p in after if p not in before)
        removed = sorted(p for p in before if p not in after)
        print(f"STALE {ch['id']} ({ch['title']}) -> assets/chapters/{ch['id']}.html")
        for label, items in (("changed", changed), ("added", added), ("removed", removed)):
            for p in items:
                print(f"    {label}: {p}")
    if old.get("chapters") != [c["id"] for c in cmap]:
        print("Chapter list changed since the last build.")
    if not listed:
        print("No chapter is stale: every cited source matches the committed guide.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="build, write, then check citations, example arithmetic, placeholders and offline-ness")
    mode.add_argument("--check", action="store_true", help="rebuild in memory and compare with the committed guide (no write)")
    mode.add_argument("--stale", action="store_true", help="list chapters whose cited sources changed since the committed guide was built")
    parser.add_argument("--out", type=Path, default=OUT, help="output path (default: docs/guide/shared/inframate-guide.html)")
    args = parser.parse_args(argv)

    if args.stale:
        return stale()
    try:
        page, state = build()
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if not args.out.is_file():
            print("no committed guide to compare with")
            return 1
        if read(args.out) == page:
            print(f"{rel(args.out)} is current.")
            return 0
        print(f"{rel(args.out)} differs from a fresh build. Run /teach-me refresh (or build_guide.py) and review the diff.")
        stale()
        return 1
    problems = verify(state)
    if state["errors"] and not args.verify:
        for p in state["errors"]:
            print("error:", p, file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    passed = sum(1 for _, ok, _ in state["checks"] if ok)
    print(f"wrote {rel(args.out) if args.out.is_relative_to(ROOT) else args.out} ({len(page) // 1024} KB, "
          f"{len(state['meta']['chapters'])} chapters, {len(state['citations'])} citations, {passed}/{len(state['checks'])} worked-example checks)")
    if args.verify:
        if problems:
            for p in problems:
                print("FAIL", p)
            return 1
        print("verify: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
