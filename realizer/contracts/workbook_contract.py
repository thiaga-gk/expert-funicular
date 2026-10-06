"""Locked Workbook Reporter Contract v1.1.0.

The workbook is a presentation projection of Reporter JSON Contract v1.0.0.
It MUST NOT recompute recommendations, confidence, scenario economics, or IDs.

Grains
------
* Executive Summary: portfolio/run aggregation across packages.
* Warehouse Portfolio: exactly one row per recommendation_package_id.
* Scenario Plans: exactly one row per scenario_id.
* Recommendation Registry: exactly one row per candidate_id.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import reporter_contract as rc

WORKBOOK_CONTRACT_VERSION = "1.1.0"
SHEET_EXECUTIVE = "Executive Summary"
SHEET_PORTFOLIO = "Warehouse Portfolio"
SHEET_SCENARIOS = "Scenario Plans"
SHEET_REGISTRY = "Recommendation Registry"
SHEET_NAMES = (SHEET_EXECUTIVE, SHEET_PORTFOLIO, SHEET_SCENARIOS, SHEET_REGISTRY)


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    header: str
    width: float = 16.0
    number_format: Optional[str] = None
    wrap: bool = False


@dataclass(frozen=True)
class WorkbookProjection:
    executive_metrics: List[Dict[str, Any]]
    executive_opportunities: List[Dict[str, Any]]
    warehouse_portfolio: List[Dict[str, Any]]
    scenario_plans: List[Dict[str, Any]]
    recommendation_registry: List[Dict[str, Any]]

    def validate(self, packages: Sequence[rc.RecommendationPackage]) -> None:
        package_ids = {p.recommendation_package_id for p in packages}
        scenario_ids = {s.scenario_id for p in packages for s in p.scenarios}
        candidate_ids = {c.candidate_id for p in packages for c in p.candidates}
        assert len(self.warehouse_portfolio) == len(packages), "one Warehouse Portfolio row per package"
        assert {r["recommendation_package_id"] for r in self.warehouse_portfolio} == package_ids
        assert len(self.scenario_plans) == len(scenario_ids), "one Scenario Plans row per scenario"
        assert {r["scenario_id"] for r in self.scenario_plans} == scenario_ids
        assert len(self.recommendation_registry) == len(candidate_ids), "one Registry row per candidate"
        assert {r["candidate_id"] for r in self.recommendation_registry} == candidate_ids


def _enum(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _json(v: Any) -> str:
    if hasattr(v, "to_dict"):
        v = v.to_dict()
    else:
        v = rc.to_primitive(v)
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _money(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def _pct(n: Optional[float], d: Optional[float]) -> Optional[float]:
    if n is None or d in (None, 0):
        return None
    return round(float(n) / float(d), 6)


def _range_component(component: Optional[rc.Range3], bound: str) -> Optional[float]:
    if component is None:
        return None
    return _money(getattr(component, bound))


def _component_range(ranges: rc.ComponentRanges, component: str, bound: str) -> Optional[float]:
    r = getattr(ranges, component)
    return _range_component(r, bound)


def _candidate_scalar(v: Any) -> Any:
    if isinstance(v, Mapping) and len(v) == 1:
        return next(iter(v.values()))
    return v


def candidate_view_text(package: rc.RecommendationPackage) -> str:
    labels = {
        rc.TunableKnob.ENABLE_PHOTON: "Photon",
        rc.TunableKnob.WAREHOUSE_SIZE: "Size",
        rc.TunableKnob.MAX_CLUSTERS: "Max clusters",
        rc.TunableKnob.MIN_CLUSTERS: "Min clusters",
        rc.TunableKnob.AUTO_STOP_MINUTES: "Auto stop",
        rc.TunableKnob.WAREHOUSE_TYPE: "Warehouse type",
        rc.TunableKnob.WAREHOUSE_CHANNEL: "Channel",
    }
    order = [
        rc.TunableKnob.ENABLE_PHOTON,
        rc.TunableKnob.WAREHOUSE_SIZE,
        rc.TunableKnob.MAX_CLUSTERS,
        rc.TunableKnob.MIN_CLUSTERS,
        rc.TunableKnob.AUTO_STOP_MINUTES,
        rc.TunableKnob.WAREHOUSE_TYPE,
        rc.TunableKnob.WAREHOUSE_CHANNEL,
    ]
    by = {c.knob: c for c in package.candidates}
    lines: List[str] = []
    for knob in order:
        c = by.get(knob)
        if c is None:
            continue
        cur, rec = _candidate_scalar(c.current_value), _candidate_scalar(c.recommended_value)
        if cur == rec:
            change = str(cur)
        else:
            change = f"{cur} → {rec}"
        lines.append(f"{labels[knob]}: {change} [{_enum(c.status)}; {_enum(c.confidence.level)} {'' if c.confidence.score is None else f'{c.confidence.score:.2f}'}]".rstrip())
    return "\n".join(lines)


def execution_plan_text(package: rc.RecommendationPackage) -> str:
    sid = package.recommended_plan.scenario_id
    scenario = next((s for s in package.scenarios if s.scenario_id == sid), None)
    if scenario is None:
        return f"No recommended scenario. Next action: {package.recommended_plan.next_action}"
    lines = [scenario.scenario_name]
    for step in sorted(scenario.execution_plan, key=lambda x: x.step):
        if step.step_type == rc.PlanStepType.CANDIDATE:
            cur = _candidate_scalar(step.current_value)
            rec = _candidate_scalar(step.recommended_value)
            action = f"{_enum(step.knob)}: {cur} → {rec}"
        else:
            action = step.action or _enum(step.step_type)
        suffix = f" [{_enum(step.execution_state)}; {_enum(step.confidence.level)}"
        if step.confidence.score is not None:
            suffix += f" {step.confidence.score:.2f}"
        suffix += "]"
        lines.append(f"{step.step}. {action}{suffix}")
        if step.validation.remeasure_days:
            lines.append(f"   remeasure_days={step.validation.remeasure_days}")
    return "\n".join(lines)


def _scenario_membership(package: rc.RecommendationPackage, candidate_id: str) -> List[Dict[str, Any]]:
    rows = []
    for s in package.scenarios:
        for step in s.execution_plan:
            if step.candidate_id == candidate_id:
                rows.append({
                    "scenario_id": s.scenario_id,
                    "scenario_name": s.scenario_name,
                    "step": step.step,
                    "execution_state": _enum(step.execution_state),
                })
    return rows


def _candidate_evidence(package: rc.RecommendationPackage, candidate: rc.CandidateRecommendation) -> Dict[str, Any]:
    return {eid: rc.to_primitive(package.evidence[eid]) for eid in candidate.evidence_refs if eid in package.evidence}


def _candidate_thresholds(package: rc.RecommendationPackage, candidate: rc.CandidateRecommendation) -> List[Dict[str, Any]]:
    result = []
    for eid in candidate.evidence_refs:
        e = package.evidence.get(eid)
        if e is not None and e.threshold is not None:
            result.append({"evidence_id": eid, "metric": e.metric, "threshold": e.threshold, "comparator": e.comparator})
    return result


def _candidate_realization(package: rc.RecommendationPackage, candidate_id: str) -> Dict[str, Any]:
    records = [r for r in package.realization.realization_records if r.candidate_id == candidate_id]
    if not records:
        return {
            "realization_status": _enum(package.realization.status),
            "applied_at": None,
            "validation_status": None,
            "realized_dbu_savings": None,
            "realized_aws_savings": None,
            "realized_total_savings": None,
            "annualized_realized_savings": None,
            "rollback_required": None,
            "realization_records_json": "[]",
        }
    last = records[-1]
    return {
        "realization_status": _enum(package.realization.status),
        "applied_at": last.applied.applied_at,
        "validation_status": _enum(last.validation.validation_status),
        "realized_dbu_savings": _money(last.realized_savings.dbu_savings),
        "realized_aws_savings": _money(last.realized_savings.aws_savings),
        "realized_total_savings": _money(last.realized_savings.total_savings),
        "annualized_realized_savings": _money(last.realized_savings.annualized_total_savings),
        "rollback_required": last.outcome.rollback_required,
        "realization_records_json": _json(records),
    }


def project_warehouse_portfolio(package: rc.RecommendationPackage) -> Dict[str, Any]:
    b = package.cost_baseline
    pe = package.package_economics
    recommended = next((s for s in package.scenarios if s.scenario_id == package.recommended_plan.scenario_id), None)
    activity = package.workload_profile.activity
    return {
        "recommendation_package_id": package.recommendation_package_id,
        "workspace_id": package.warehouse.workspace_id,
        "warehouse_id": package.warehouse.warehouse_id,
        "warehouse_name": package.warehouse.warehouse_name,
        "owner": package.warehouse.owner,
        "region": package.warehouse.region,
        "current_epoch_id": package.warehouse.current_epoch.epoch_id,
        "epoch_age_days": package.warehouse.current_epoch.age_days,
        "primary_query_source": package.workload_profile.primary_query_source,
        "queries_30d": activity.queries_30d,
        "active_days_30d": activity.active_days_30d,
        "query_source_profile_json": _json(package.workload_profile),
        "warehouse_type": package.current_config.warehouse_type,
        "warehouse_size": package.current_config.warehouse_size,
        "min_clusters": package.current_config.min_clusters,
        "max_clusters": package.current_config.max_clusters,
        "auto_stop_minutes": package.current_config.auto_stop_minutes,
        "warehouse_channel": package.current_config.warehouse_channel,
        "photon_status": _enum(package.current_config.photon.status),
        "actual_dbu_cost_365d": _money(b.actual_365d.dbu_cost),
        "actual_aws_cost_365d": _money(b.actual_365d.aws_cost),
        "actual_total_cost_365d": _money(b.actual_365d.total_cost),
        "forward_dbu_cost_365d": _money(b.forward_365d.dbu_cost),
        "forward_aws_cost_365d": _money(b.forward_365d.aws_cost),
        "forward_total_cost_365d": _money(b.forward_365d.total_cost),
        "package_savings_low_365d": _range_component(pe.estimated_savings_365d, "low"),
        "package_savings_base_365d": _range_component(pe.estimated_savings_365d, "base"),
        "package_savings_high_365d": _range_component(pe.estimated_savings_365d, "high"),
        "package_cost_low_365d": _range_component(pe.estimated_cost_365d, "low"),
        "package_cost_base_365d": _range_component(pe.estimated_cost_365d, "base"),
        "package_cost_high_365d": _range_component(pe.estimated_cost_365d, "high"),
        "base_savings_pct": _pct(_range_component(pe.estimated_savings_365d, "base"), b.forward_365d.total_cost),
        "recommended_scenario_id": package.recommended_plan.scenario_id,
        "recommended_scenario_name": None if recommended is None else recommended.scenario_name,
        "scenario_type": None if recommended is None else _enum(recommended.scenario_type),
        "scenario_confidence_level": None if recommended is None else _enum(recommended.confidence.level),
        "scenario_confidence_score": None if recommended is None else recommended.confidence.score,
        "next_action": package.recommended_plan.next_action,
        "requires_hitl": package.recommended_plan.requires_hitl,
        "overall_status": _enum(package.assessment.overall_status),
        "evidence_quality_status": _enum(package.assessment.evidence_quality.status),
        "evidence_confidence_level": _enum(package.assessment.evidence_quality.confidence.level),
        "evidence_confidence_score": package.assessment.evidence_quality.confidence.score,
        "candidate_count": package.assessment.summary.candidate_count,
        "actionable_count": package.assessment.summary.actionable_count,
        "test_required_count": package.assessment.summary.test_required_count,
        "review_count": package.assessment.summary.review_count,
        "blocked_count": package.assessment.summary.blocked_count,
        "no_change_count": package.assessment.summary.no_change_count,
        "candidate_view": candidate_view_text(package),
        "recommended_execution_plan": execution_plan_text(package),
        "scenario_ids_json": _json([s.scenario_id for s in package.scenarios]),
        "candidate_ids_json": _json([c.candidate_id for c in package.candidates]),
        "evidence_summary_json": _json({k: {"metric": v.metric, "value": v.value, "interpretation": v.interpretation} for k, v in package.evidence.items()}),
    }


def project_scenario(package: rc.RecommendationPackage, s: rc.Scenario) -> Dict[str, Any]:
    e = s.portfolio_economics
    steps = sorted(s.execution_plan, key=lambda x: x.step)
    invalidations = sorted({_enum(k) for st in steps for k in st.invalidates})
    remeasure_days = max((st.validation.remeasure_days or 0 for st in steps), default=0) or None
    return {
        "recommendation_package_id": package.recommendation_package_id,
        "scenario_id": s.scenario_id,
        "workspace_id": package.warehouse.workspace_id,
        "warehouse_id": package.warehouse.warehouse_id,
        "warehouse_name": package.warehouse.warehouse_name,
        "owner": package.warehouse.owner,
        "scenario_name": s.scenario_name,
        "scenario_type": _enum(s.scenario_type),
        "status": _enum(s.status),
        "recommended_flag": s.scenario_id == package.recommended_plan.scenario_id,
        "confidence_score": s.confidence.score,
        "confidence_level": _enum(s.confidence.level),
        "confidence_method": "BOTTLENECK_MINIMUM_STEP_CONFIDENCE",
        "compatibility_status": _enum(s.compatibility.status),
        "compatibility_mode": _enum(s.compatibility.mode),
        "compatibility_checks_json": _json(s.compatibility.checks),
        "baseline_dbu_cost": _money(e.baseline_365d.dbu_cost),
        "baseline_aws_cost": _money(e.baseline_365d.aws_cost),
        "baseline_total_cost": _money(e.baseline_365d.total_cost),
        "dbu_savings_low": _component_range(e.savings_365d, "dbu", "low"),
        "dbu_savings_base": _component_range(e.savings_365d, "dbu", "base"),
        "dbu_savings_high": _component_range(e.savings_365d, "dbu", "high"),
        "aws_savings_low": _component_range(e.savings_365d, "aws", "low"),
        "aws_savings_base": _component_range(e.savings_365d, "aws", "base"),
        "aws_savings_high": _component_range(e.savings_365d, "aws", "high"),
        "total_savings_low": _component_range(e.savings_365d, "total", "low"),
        "total_savings_base": _component_range(e.savings_365d, "total", "base"),
        "total_savings_high": _component_range(e.savings_365d, "total", "high"),
        "estimated_total_cost_low": _component_range(e.estimated_cost_365d, "total", "low"),
        "estimated_total_cost_base": _component_range(e.estimated_cost_365d, "total", "base"),
        "estimated_total_cost_high": _component_range(e.estimated_cost_365d, "total", "high"),
        "estimation_status": _enum(e.estimation_status),
        "execution_plan": execution_plan_text_for_scenario(s),
        "execution_plan_json": _json(steps),
        "candidate_ids_json": _json(s.candidate_refs),
        "dependencies_json": _json([rc.to_primitive(d) for st in steps for d in st.dependencies]),
        "invalidations_json": _json(invalidations),
        "remeasure_required": bool(remeasure_days),
        "remeasure_days": remeasure_days,
    }


def execution_plan_text_for_scenario(s: rc.Scenario) -> str:
    lines = [s.scenario_name]
    for step in sorted(s.execution_plan, key=lambda x: x.step):
        if step.step_type == rc.PlanStepType.CANDIDATE:
            cur = _candidate_scalar(step.current_value)
            rec = _candidate_scalar(step.recommended_value)
            action = f"{_enum(step.knob)}: {cur} → {rec}"
        else:
            action = step.action or _enum(step.step_type)
        lines.append(f"{step.step}. {action} [{_enum(step.execution_state)}]")
        if step.validation.remeasure_days:
            lines.append(f"   Re-measure {step.validation.remeasure_days} days")
    return "\n".join(lines)


def project_candidate(package: rc.RecommendationPackage, c: rc.CandidateRecommendation) -> Dict[str, Any]:
    e = c.independent_economics
    evidence = _candidate_evidence(package, c)
    realization = _candidate_realization(package, c.candidate_id)
    row = {
        "recommendation_package_id": package.recommendation_package_id,
        "candidate_id": c.candidate_id,
        "technique_id": c.technique_id,
        "workspace_id": package.warehouse.workspace_id,
        "warehouse_id": package.warehouse.warehouse_id,
        "warehouse_name": package.warehouse.warehouse_name,
        "owner": package.warehouse.owner,
        "epoch_id": package.warehouse.current_epoch.epoch_id,
        "knob": _enum(c.knob),
        "status": _enum(c.status),
        "objectives": ", ".join(_enum(x) for x in c.objectives),
        "current_value": _json(c.current_value) if isinstance(c.current_value, (dict, list)) else c.current_value,
        "recommended_value": _json(c.recommended_value) if isinstance(c.recommended_value, (dict, list)) else c.recommended_value,
        "rationale": c.rationale,
        "confidence_score": c.confidence.score,
        "confidence_level": _enum(c.confidence.level),
        "confidence_components_json": _json(c.confidence.components),
        "evidence_refs_json": _json(c.evidence_refs),
        "evidence_json": _json(evidence),
        "decision_rule": "; ".join(f"{x['metric']} {x['comparator']} {x['threshold']}" for x in _candidate_thresholds(package, c)),
        "thresholds_json": _json(_candidate_thresholds(package, c)),
        "dependencies_json": _json(c.dependencies),
        "invalidates_json": _json([_enum(x) for x in c.invalidates]),
        "blockers_json": _json(c.blockers),
        "economics_available": e.estimation_status != rc.EstimationStatus.UNAVAILABLE,
        "baseline_dbu_cost": _money(e.baseline_365d.dbu_cost),
        "baseline_aws_cost": _money(e.baseline_365d.aws_cost),
        "baseline_total_cost": _money(e.baseline_365d.total_cost),
        "dbu_savings_low": _component_range(e.savings_365d, "dbu", "low"),
        "dbu_savings_base": _component_range(e.savings_365d, "dbu", "base"),
        "dbu_savings_high": _component_range(e.savings_365d, "dbu", "high"),
        "aws_savings_low": _component_range(e.savings_365d, "aws", "low"),
        "aws_savings_base": _component_range(e.savings_365d, "aws", "base"),
        "aws_savings_high": _component_range(e.savings_365d, "aws", "high"),
        "total_savings_low": _component_range(e.savings_365d, "total", "low"),
        "total_savings_base": _component_range(e.savings_365d, "total", "base"),
        "total_savings_high": _component_range(e.savings_365d, "total", "high"),
        "estimated_total_cost_low": _component_range(e.estimated_cost_365d, "total", "low"),
        "estimated_total_cost_base": _component_range(e.estimated_cost_365d, "total", "base"),
        "estimated_total_cost_high": _component_range(e.estimated_cost_365d, "total", "high"),
        "is_additive": e.is_additive,
        "scenario_membership_json": _json(_scenario_membership(package, c.candidate_id)),
    }
    row.update(realization)
    return row


def project_executive(packages: Sequence[rc.RecommendationPackage], portfolio_rows: Sequence[Mapping[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    currencies = sorted({p.cost_baseline.currency for p in packages})
    same_currency = len(currencies) == 1
    currency = currencies[0] if same_currency else "MIXED"
    def sum_key(key: str) -> Optional[float]:
        vals = [r.get(key) for r in portfolio_rows]
        if not same_currency or any(v is None for v in vals):
            return None
        return round(sum(float(v) for v in vals), 2)
    actual_total = sum_key("actual_total_cost_365d")
    forward_total = sum_key("forward_total_cost_365d")
    low = sum_key("package_savings_low_365d")
    base = sum_key("package_savings_base_365d")
    high = sum_key("package_savings_high_365d")
    assessable = sum(1 for r in portfolio_rows if r.get("package_savings_base_365d") is not None)
    metrics = [
        {"section":"Coverage","metric":"Workbook contract","value":WORKBOOK_CONTRACT_VERSION,"currency":None,"notes":"Pure projection of Reporter JSON Contract v1.0.0"},
        {"section":"Coverage","metric":"Reporter contract","value":rc.CONTRACT_VERSION,"currency":None,"notes":"Locked JSON source of truth"},
        {"section":"Coverage","metric":"Warehouses assessed","value":len(packages),"currency":None,"notes":"One immutable package per warehouse/current epoch"},
        {"section":"Coverage","metric":"Workspaces","value":len({p.warehouse.workspace_id for p in packages}),"currency":None,"notes":None},
        {"section":"Coverage","metric":"Regions","value":len({p.warehouse.region for p in packages}),"currency":None,"notes":None},
        {"section":"Coverage","metric":"Economically assessable","value":assessable,"currency":None,"notes":f"Excluded: {len(packages)-assessable}"},
        {"section":"Cost","metric":"Actual trailing 365d","value":actual_total,"currency":currency,"notes":"Historical assessed total cost"},
        {"section":"Cost","metric":"Forward 365d baseline","value":forward_total,"currency":currency,"notes":"No-change forecast"},
        {"section":"Opportunity","metric":"Annual savings — low","value":low,"currency":currency,"notes":"Σ package low; non-additive within package"},
        {"section":"Opportunity","metric":"Annual savings — recommended/base","value":base,"currency":currency,"notes":"Σ recommended scenario base"},
        {"section":"Opportunity","metric":"Annual savings — high","value":high,"currency":currency,"notes":"Σ package high"},
        {"section":"Opportunity","metric":"Base savings %","value":_pct(base,forward_total),"currency":None,"notes":"Base savings / forward baseline"},
        {"section":"Confidence","metric":"HIGH recommended scenario","value":sum(1 for r in portfolio_rows if r.get("scenario_confidence_level")=="HIGH"),"currency":None,"notes":None},
        {"section":"Confidence","metric":"MEDIUM recommended scenario","value":sum(1 for r in portfolio_rows if r.get("scenario_confidence_level")=="MEDIUM"),"currency":None,"notes":None},
        {"section":"Confidence","metric":"LOW recommended scenario","value":sum(1 for r in portfolio_rows if r.get("scenario_confidence_level")=="LOW"),"currency":None,"notes":None},
    ]
    opportunities = sorted(
        [dict(r) for r in portfolio_rows],
        key=lambda r: (r.get("package_savings_base_365d") is not None, r.get("package_savings_base_365d") or -1),
        reverse=True,
    )[:15]
    for r in opportunities:
        r["savings_range"] = None if r.get("package_savings_low_365d") is None else f"{r['package_savings_low_365d']:,.0f} – {r['package_savings_high_365d']:,.0f}"
    return metrics, opportunities


def project_workbook(packages: Sequence[rc.RecommendationPackage]) -> WorkbookProjection:
    portfolio = [project_warehouse_portfolio(p) for p in packages]
    scenarios = [project_scenario(p, s) for p in packages for s in p.scenarios]
    registry = [project_candidate(p, c) for p in packages for c in p.candidates]
    metrics, opportunities = project_executive(packages, portfolio)
    result = WorkbookProjection(metrics, opportunities, portfolio, scenarios, registry)
    result.validate(packages)
    return result
