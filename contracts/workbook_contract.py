"""Workbook Reporter Contract v1.1.0.

A compact, human-facing projection of locked Reporter JSON Contract v1.0.0.
The workbook never recomputes optimizer decisions, confidence, IDs, or
counterfactual economics.  Signed savings from JSON are rendered as explicit
``SAVINGS`` / ``COST_INCREASE`` presentation values so a negative number is
never mislabeled as savings.

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
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import reporter_contract as rc

WORKBOOK_CONTRACT_VERSION = "1.1.0"
SHEET_EXECUTIVE = "Executive Summary"
SHEET_PORTFOLIO = "Warehouse Portfolio"
SHEET_SCENARIOS = "Scenario Plans"
SHEET_REGISTRY = "Recommendation Registry"
SHEET_NAMES = (SHEET_EXECUTIVE, SHEET_PORTFOLIO, SHEET_SCENARIOS, SHEET_REGISTRY)

DISPLAY_NA = "—"
DISPLAY_UNAVAILABLE = "UNAVAILABLE"
ECON_SAVINGS = "SAVINGS"
ECON_COST_INCREASE = "COST_INCREASE"
ECON_NO_CHANGE = "NO_CHANGE"
ECON_MIXED = "MIXED"
ECON_UNAVAILABLE = "UNAVAILABLE"

MONEY_FORMAT = '#,##0.00;[Red](#,##0.00);-'
PERCENT_FORMAT = '0.0%;[Red](0.0%);-'


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    header: str
    width: float = 16.0
    number_format: Optional[str] = None
    wrap: bool = False


@dataclass(frozen=True)
class ColumnGroup:
    title: str
    start_key: str
    end_key: str


PORTFOLIO_COLUMNS = (
    ColumnSpec("recommendation_package_id", "Package ID", 25, "@"),
    ColumnSpec("workspace_id", "Workspace ID", 17, "@"),
    ColumnSpec("warehouse_id", "Warehouse ID", 16, "@"),
    ColumnSpec("warehouse_name", "Warehouse", 22),
    ColumnSpec("owner", "Owner", 24),
    ColumnSpec("region", "Region", 12),
    ColumnSpec("current_epoch_id", "Epoch ID", 23, "@"),
    ColumnSpec("epoch_age_days", "Epoch age (d)", 11, "#,##0"),
    ColumnSpec("primary_query_source", "Primary source", 15),
    ColumnSpec("queries_30d", "Queries 30d", 12, "#,##0"),
    ColumnSpec("active_days_30d", "Active days 30d", 13, "#,##0"),
    ColumnSpec("warehouse_type", "Type", 11),
    ColumnSpec("warehouse_size", "Size", 10),
    ColumnSpec("min_clusters", "Min", 8, "#,##0"),
    ColumnSpec("max_clusters", "Max", 8, "#,##0"),
    ColumnSpec("auto_stop_minutes", "Auto-stop (min)", 13, "#,##0"),
    ColumnSpec("warehouse_channel", "Channel", 12),
    ColumnSpec("photon_status", "Photon", 11),
    ColumnSpec("currency", "Currency", 9),
    ColumnSpec("actual_dbu_cost_365d", "Actual DBU", 14, MONEY_FORMAT),
    ColumnSpec("actual_aws_cost_365d", "Actual AWS", 14, MONEY_FORMAT),
    ColumnSpec("actual_total_cost_365d", "Actual total", 14, MONEY_FORMAT),
    ColumnSpec("forward_dbu_cost_365d", "Forward DBU", 14, MONEY_FORMAT),
    ColumnSpec("forward_aws_cost_365d", "Forward AWS", 14, MONEY_FORMAT),
    ColumnSpec("forward_total_cost_365d", "Forward total", 14, MONEY_FORMAT),
    ColumnSpec("package_base_impact_type", "Base impact type", 16),
    ColumnSpec("package_low_impact", "Low annual impact", 15, MONEY_FORMAT),
    ColumnSpec("package_base_impact", "Base annual impact", 15, MONEY_FORMAT),
    ColumnSpec("package_high_impact", "High annual impact", 15, MONEY_FORMAT),
    ColumnSpec("package_cost_low_365d", "Est. cost low", 14, MONEY_FORMAT),
    ColumnSpec("package_cost_base_365d", "Est. cost base", 14, MONEY_FORMAT),
    ColumnSpec("package_cost_high_365d", "Est. cost high", 14, MONEY_FORMAT),
    ColumnSpec("impact_pct", "Base impact %", 12, PERCENT_FORMAT),
    ColumnSpec("recommended_scenario_id", "Scenario ID", 25, "@"),
    ColumnSpec("recommended_scenario_name", "Recommended plan", 28),
    ColumnSpec("scenario_type", "Scenario type", 22),
    ColumnSpec("scenario_confidence_level", "Confidence", 11),
    ColumnSpec("scenario_confidence_score", "Conf. score", 11, "0.00"),
    ColumnSpec("next_action", "Next action", 24),
    ColumnSpec("requires_hitl", "HITL?", 9),
    ColumnSpec("overall_status", "Overall status", 22),
    ColumnSpec("evidence_quality_status", "Evidence quality", 17),
    ColumnSpec("evidence_confidence_level", "Evidence conf.", 13),
    ColumnSpec("candidate_count", "Candidates", 11, "#,##0"),
    ColumnSpec("actionable_count", "Actionable", 11, "#,##0"),
    ColumnSpec("test_required_count", "Test req.", 10, "#,##0"),
    ColumnSpec("review_count", "Review", 9, "#,##0"),
    ColumnSpec("blocked_count", "Blocked", 9, "#,##0"),
    ColumnSpec("no_change_count", "No change", 10, "#,##0"),
    ColumnSpec("not_applicable_count", "N/A", 8, "#,##0"),
    ColumnSpec("insufficient_evidence_count", "Insuff. evidence", 14, "#,##0"),
    ColumnSpec("candidate_view", "Candidate view", 42),
    ColumnSpec("recommended_execution_plan", "Execution plan", 50),
    ColumnSpec("query_source_profile_json", "Query source JSON", 22),
    ColumnSpec("scenario_ids_json", "Scenario IDs", 20),
    ColumnSpec("candidate_ids_json", "Candidate IDs", 20),
    ColumnSpec("package_file", "Package JSON", 32),
    ColumnSpec("evidence_summary_json", "Evidence summary JSON", 22),
)
PORTFOLIO_GROUPS = (
    ColumnGroup("Identity / epoch", "recommendation_package_id", "epoch_age_days"),
    ColumnGroup("Workload", "primary_query_source", "active_days_30d"),
    ColumnGroup("Current configuration", "warehouse_type", "photon_status"),
    ColumnGroup("Cost baseline", "currency", "forward_total_cost_365d"),
    ColumnGroup("Package opportunity", "package_base_impact_type", "impact_pct"),
    ColumnGroup("Recommended plan", "recommended_scenario_id", "requires_hitl"),
    ColumnGroup("Assessment", "overall_status", "insufficient_evidence_count"),
    ColumnGroup("Decision view", "candidate_view", "recommended_execution_plan"),
    ColumnGroup("Audit / traceability", "query_source_profile_json", "evidence_summary_json"),
)

SCENARIO_COLUMNS = (
    ColumnSpec("recommendation_package_id", "Package ID", 25, "@"),
    ColumnSpec("scenario_id", "Scenario ID", 25, "@"),
    ColumnSpec("workspace_id", "Workspace ID", 17, "@"),
    ColumnSpec("warehouse_id", "Warehouse ID", 16, "@"),
    ColumnSpec("warehouse_name", "Warehouse", 22),
    ColumnSpec("owner", "Owner", 24),
    ColumnSpec("scenario_name", "Scenario", 30),
    ColumnSpec("scenario_type", "Type", 22),
    ColumnSpec("status", "Status", 14),
    ColumnSpec("recommended_flag", "Recommended?", 12),
    ColumnSpec("confidence_score", "Conf. score", 11, "0.00"),
    ColumnSpec("confidence_level", "Confidence", 11),
    ColumnSpec("confidence_method", "Confidence method", 22),
    ColumnSpec("compatibility_status", "Compatibility", 16),
    ColumnSpec("compatibility_mode", "Compat. mode", 20),
    ColumnSpec("currency", "Currency", 9),
    ColumnSpec("base_impact_type", "Base impact type", 16),
    ColumnSpec("baseline_dbu_cost", "Baseline DBU", 14, MONEY_FORMAT),
    ColumnSpec("baseline_aws_cost", "Baseline AWS", 14, MONEY_FORMAT),
    ColumnSpec("baseline_total_cost", "Baseline total", 14, MONEY_FORMAT),
    ColumnSpec("dbu_low_impact", "DBU low impact", 15, MONEY_FORMAT),
    ColumnSpec("dbu_base_impact", "DBU base impact", 15, MONEY_FORMAT),
    ColumnSpec("dbu_high_impact", "DBU high impact", 15, MONEY_FORMAT),
    ColumnSpec("aws_low_impact", "AWS low impact", 15, MONEY_FORMAT),
    ColumnSpec("aws_base_impact", "AWS base impact", 15, MONEY_FORMAT),
    ColumnSpec("aws_high_impact", "AWS high impact", 15, MONEY_FORMAT),
    ColumnSpec("total_low_impact", "Total low impact", 15, MONEY_FORMAT),
    ColumnSpec("total_base_impact", "Total base impact", 15, MONEY_FORMAT),
    ColumnSpec("total_high_impact", "Total high impact", 15, MONEY_FORMAT),
    ColumnSpec("estimated_total_cost_low", "Est. cost low", 14, MONEY_FORMAT),
    ColumnSpec("estimated_total_cost_base", "Est. cost base", 14, MONEY_FORMAT),
    ColumnSpec("estimated_total_cost_high", "Est. cost high", 14, MONEY_FORMAT),
    ColumnSpec("estimation_status", "Estimation status", 19),
    ColumnSpec("execution_plan", "Execution plan", 48),
    ColumnSpec("remeasure_required", "Remeasure?", 11),
    ColumnSpec("remeasure_days", "Remeasure days", 13, "#,##0"),
    ColumnSpec("compatibility_checks_json", "Compatibility checks", 22),
    ColumnSpec("compatibility_blockers_json", "Compatibility blockers", 22),
    ColumnSpec("execution_plan_json", "Execution plan JSON", 22),
    ColumnSpec("candidate_ids_json", "Candidate IDs", 20),
    ColumnSpec("dependencies_json", "Dependencies", 20),
    ColumnSpec("invalidations_json", "Invalidations", 20),
)
SCENARIO_GROUPS = (
    ColumnGroup("Identity", "recommendation_package_id", "owner"),
    ColumnGroup("Scenario", "scenario_name", "recommended_flag"),
    ColumnGroup("Confidence", "confidence_score", "confidence_method"),
    ColumnGroup("Compatibility", "compatibility_status", "compatibility_mode"),
    ColumnGroup("Economics", "currency", "baseline_total_cost"),
    ColumnGroup("DBU impact", "dbu_low_impact", "dbu_high_impact"),
    ColumnGroup("AWS impact", "aws_low_impact", "aws_high_impact"),
    ColumnGroup("Total impact", "total_low_impact", "total_high_impact"),
    ColumnGroup("Counterfactual cost", "estimated_total_cost_low", "estimation_status"),
    ColumnGroup("Execution", "execution_plan", "remeasure_days"),
    ColumnGroup("Audit / traceability", "compatibility_checks_json", "invalidations_json"),
)

REGISTRY_COLUMNS = (
    ColumnSpec("recommendation_package_id", "Package ID", 25, "@"),
    ColumnSpec("candidate_id", "Candidate ID", 25, "@"),
    ColumnSpec("technique_id", "Technique ID", 27, "@"),
    ColumnSpec("workspace_id", "Workspace ID", 17, "@"),
    ColumnSpec("warehouse_id", "Warehouse ID", 16, "@"),
    ColumnSpec("warehouse_name", "Warehouse", 22),
    ColumnSpec("owner", "Owner", 24),
    ColumnSpec("epoch_id", "Epoch ID", 23, "@"),
    ColumnSpec("knob", "Knob", 20),
    ColumnSpec("status", "Status", 18),
    ColumnSpec("objectives", "Objective", 18),
    ColumnSpec("current_value", "Current", 18),
    ColumnSpec("recommended_value", "Recommended", 18),
    ColumnSpec("rationale", "Rationale", 40),
    ColumnSpec("confidence_score", "Conf. score", 11, "0.00"),
    ColumnSpec("confidence_level", "Confidence", 11),
    ColumnSpec("economics_available", "Economics?", 11),
    ColumnSpec("currency", "Currency", 9),
    ColumnSpec("base_impact_type", "Base impact type", 16),
    ColumnSpec("baseline_dbu_cost", "Baseline DBU", 14, MONEY_FORMAT),
    ColumnSpec("baseline_aws_cost", "Baseline AWS", 14, MONEY_FORMAT),
    ColumnSpec("baseline_total_cost", "Baseline total", 14, MONEY_FORMAT),
    ColumnSpec("dbu_low_impact", "DBU low impact", 15, MONEY_FORMAT),
    ColumnSpec("dbu_base_impact", "DBU base impact", 15, MONEY_FORMAT),
    ColumnSpec("dbu_high_impact", "DBU high impact", 15, MONEY_FORMAT),
    ColumnSpec("aws_low_impact", "AWS low impact", 15, MONEY_FORMAT),
    ColumnSpec("aws_base_impact", "AWS base impact", 15, MONEY_FORMAT),
    ColumnSpec("aws_high_impact", "AWS high impact", 15, MONEY_FORMAT),
    ColumnSpec("total_low_impact", "Total low impact", 15, MONEY_FORMAT),
    ColumnSpec("total_base_impact", "Total base impact", 15, MONEY_FORMAT),
    ColumnSpec("total_high_impact", "Total high impact", 15, MONEY_FORMAT),
    ColumnSpec("estimated_total_cost_low", "Est. cost low", 14, MONEY_FORMAT),
    ColumnSpec("estimated_total_cost_base", "Est. cost base", 14, MONEY_FORMAT),
    ColumnSpec("estimated_total_cost_high", "Est. cost high", 14, MONEY_FORMAT),
    ColumnSpec("is_additive", "Additive?", 10),
    ColumnSpec("realization_status", "Realization", 14),
    ColumnSpec("applied_at", "Applied at", 20),
    ColumnSpec("validation_status", "Validation", 15),
    ColumnSpec("realized_dbu_impact", "Realized DBU impact", 16, MONEY_FORMAT),
    ColumnSpec("realized_aws_impact", "Realized AWS impact", 16, MONEY_FORMAT),
    ColumnSpec("realized_total_impact", "Realized total impact", 17, MONEY_FORMAT),
    ColumnSpec("annualized_realized_impact", "Annualized realized", 17, MONEY_FORMAT),
    ColumnSpec("rollback_required", "Rollback?", 11),
    ColumnSpec("decision_rule", "Decision rule", 32),
    ColumnSpec("confidence_components_json", "Confidence components", 22),
    ColumnSpec("evidence_refs_json", "Evidence refs", 20),
    ColumnSpec("evidence_json", "Evidence / findings", 24),
    ColumnSpec("thresholds_json", "Thresholds", 22),
    ColumnSpec("dependencies_json", "Dependencies", 20),
    ColumnSpec("invalidates_json", "Invalidates", 20),
    ColumnSpec("blockers_json", "Blockers", 20),
    ColumnSpec("scenario_membership_json", "Scenario membership", 22),
    ColumnSpec("realization_records_json", "Realization JSON", 22),
)
REGISTRY_GROUPS = (
    ColumnGroup("Identity", "recommendation_package_id", "epoch_id"),
    ColumnGroup("Decision", "knob", "rationale"),
    ColumnGroup("Confidence", "confidence_score", "confidence_level"),
    ColumnGroup("Independent economics", "economics_available", "is_additive"),
    ColumnGroup("Realization", "realization_status", "rollback_required"),
    ColumnGroup("Evidence / audit", "decision_rule", "realization_records_json"),
)

def _contract_keys(columns: Sequence[ColumnSpec]) -> set[str]:
    return {c.key for c in columns}


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
        for rows, keys, label in (
            (self.warehouse_portfolio, _contract_keys(PORTFOLIO_COLUMNS), "Warehouse Portfolio"),
            (self.scenario_plans, _contract_keys(SCENARIO_COLUMNS), "Scenario Plans"),
            (self.recommendation_registry, _contract_keys(REGISTRY_COLUMNS), "Recommendation Registry"),
        ):
            for row in rows:
                assert set(row) == keys, f"{label} columns drifted: missing={sorted(keys-set(row))}, extra={sorted(set(row)-keys)}"
        for collection in (self.executive_metrics, self.executive_opportunities, self.warehouse_portfolio, self.scenario_plans, self.recommendation_registry):
            for row in collection:
                assert all(value is not None for value in row.values()), f"workbook row contains None: {row}"
                assert all(not isinstance(value, str) or value.strip() for value in row.values()), f"workbook row contains blank string: {row}"


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


def _money_or_unavailable(v: Optional[float]) -> Any:
    return DISPLAY_UNAVAILABLE if v is None else round(float(v), 2)


def _display(v: Any, *, missing: str = DISPLAY_NA) -> Any:
    if v is None:
        return missing
    if isinstance(v, str) and not v.strip():
        return missing
    return _enum(v)



def _range_component(component: Optional[rc.Range3], bound: str) -> Optional[float]:
    if component is None:
        return None
    return _money(getattr(component, bound))



def _candidate_scalar(v: Any) -> Any:
    if isinstance(v, Mapping) and len(v) == 1:
        return next(iter(v.values()))
    return v


def _effect(value: Optional[float], *, epsilon: float = 0.005) -> str:
    if value is None:
        return ECON_UNAVAILABLE
    if value > epsilon:
        return ECON_SAVINGS
    if value < -epsilon:
        return ECON_COST_INCREASE
    return ECON_NO_CHANGE



def _signed_amount(value: Optional[float]) -> Any:
    """Return a sortable numeric impact; positive=savings, negative=cost increase."""
    return DISPLAY_UNAVAILABLE if value is None else round(float(value), 2)






def _range_bound_or_unavailable(value: Optional[rc.Range3], bound: str) -> Any:
    return DISPLAY_UNAVAILABLE if value is None else _money(getattr(value, bound))



def _compact_json_list(values: Sequence[Any]) -> str:
    return _json(list(values)) if values else "[]"


def _sanitize_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Guarantee data rows never render as blank cells."""
    return {key: _display(value) for key, value in row.items()}


def candidate_view_text(package: rc.RecommendationPackage) -> str:
    labels = {
        rc.TunableKnob.ENABLE_PHOTON: "Photon",
        rc.TunableKnob.WAREHOUSE_SIZE: "Size",
        rc.TunableKnob.MAX_CLUSTERS: "Max",
        rc.TunableKnob.MIN_CLUSTERS: "Min",
        rc.TunableKnob.AUTO_STOP_MINUTES: "Auto-stop",
        rc.TunableKnob.WAREHOUSE_TYPE: "Type",
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
    parts: List[str] = []
    for knob in order:
        c = by.get(knob)
        if c is None:
            continue
        cur, rec = _candidate_scalar(c.current_value), _candidate_scalar(c.recommended_value)
        change = str(cur) if cur == rec else f"{cur}→{rec}"
        parts.append(f"{labels[knob]} {change} [{_enum(c.status)}]" )
    return " | ".join(parts) or DISPLAY_NA


def execution_plan_text(package: rc.RecommendationPackage) -> str:
    sid = package.recommended_plan.scenario_id
    scenario = next((s for s in package.scenarios if s.scenario_id == sid), None)
    if scenario is None:
        return f"No recommended scenario | Next: {package.recommended_plan.next_action}"
    parts = [scenario.scenario_name]
    for step in sorted(scenario.execution_plan, key=lambda x: x.step):
        if step.step_type == rc.PlanStepType.CANDIDATE:
            cur = _candidate_scalar(step.current_value)
            rec = _candidate_scalar(step.recommended_value)
            action = f"{_enum(step.knob)} {cur}→{rec}"
        else:
            action = step.action or _enum(step.step_type)
        text = f"{step.step}. {action} [{_enum(step.execution_state)}]"
        if step.validation.remeasure_days:
            text += f"; remeasure {step.validation.remeasure_days}d"
        parts.append(text)
    return " | ".join(parts)


def execution_plan_text_for_scenario(s: rc.Scenario) -> str:
    parts = [s.scenario_name]
    for step in sorted(s.execution_plan, key=lambda x: x.step):
        if step.step_type == rc.PlanStepType.CANDIDATE:
            cur = _candidate_scalar(step.current_value)
            rec = _candidate_scalar(step.recommended_value)
            action = f"{_enum(step.knob)} {cur}→{rec}"
        else:
            action = step.action or _enum(step.step_type)
        text = f"{step.step}. {action} [{_enum(step.execution_state)}]"
        if step.validation.remeasure_days:
            text += f"; remeasure {step.validation.remeasure_days}d"
        parts.append(text)
    return " | ".join(parts)


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


def _candidate_realization_fields(package: rc.RecommendationPackage, candidate_id: str) -> Dict[str, Any]:
    records = [r for r in package.realization.realization_records if r.candidate_id == candidate_id]
    if not records:
        return {
            "realization_status": _enum(package.realization.status), "applied_at": DISPLAY_NA, "validation_status": DISPLAY_NA,
            "realized_dbu_impact": DISPLAY_UNAVAILABLE, "realized_aws_impact": DISPLAY_UNAVAILABLE,
            "realized_total_impact": DISPLAY_UNAVAILABLE, "annualized_realized_impact": DISPLAY_UNAVAILABLE,
            "rollback_required": DISPLAY_NA, "realization_records_json": "[]",
        }
    last = records[-1]
    currency = last.realized_savings.currency
    return {
        "realization_status": _enum(package.realization.status), "applied_at": last.applied.applied_at,
        "validation_status": _enum(last.validation.validation_status),
        "realized_dbu_impact": _signed_amount(last.realized_savings.dbu_savings),
        "realized_aws_impact": _signed_amount(last.realized_savings.aws_savings),
        "realized_total_impact": _signed_amount(last.realized_savings.total_savings),
        "annualized_realized_impact": _signed_amount(last.realized_savings.annualized_total_savings),
        "rollback_required": last.outcome.rollback_required, "realization_records_json": _json(records),
    }

def project_warehouse_portfolio(package: rc.RecommendationPackage) -> Dict[str, Any]:
    b, pe = package.cost_baseline, package.package_economics
    recommended = next((s for s in package.scenarios if s.scenario_id == package.recommended_plan.scenario_id), None)
    activity, summary = package.workload_profile.activity, package.assessment.summary
    base = None if pe.estimated_savings_365d is None else float(pe.estimated_savings_365d.base)
    row = {
        "recommendation_package_id": package.recommendation_package_id, "workspace_id": package.warehouse.workspace_id,
        "warehouse_id": package.warehouse.warehouse_id, "warehouse_name": package.warehouse.warehouse_name,
        "owner": _display(package.warehouse.owner, missing="UNKNOWN"), "region": package.warehouse.region,
        "current_epoch_id": package.warehouse.current_epoch.epoch_id, "epoch_age_days": package.warehouse.current_epoch.age_days,
        "primary_query_source": _display(package.workload_profile.primary_query_source, missing="UNKNOWN"),
        "queries_30d": activity.queries_30d, "active_days_30d": activity.active_days_30d,
        "query_source_profile_json": _json({"query_sources": package.workload_profile.query_sources, "client_applications": package.workload_profile.client_applications, "workload_characteristics": package.workload_profile.workload_characteristics, "multi_label": package.workload_profile.multi_label}),
        "warehouse_type": package.current_config.warehouse_type, "warehouse_size": package.current_config.warehouse_size,
        "min_clusters": package.current_config.min_clusters, "max_clusters": package.current_config.max_clusters,
        "auto_stop_minutes": package.current_config.auto_stop_minutes, "warehouse_channel": package.current_config.warehouse_channel,
        "photon_status": _enum(package.current_config.photon.status),
        "currency": b.currency, "actual_dbu_cost_365d": _money_or_unavailable(b.actual_365d.dbu_cost), "actual_aws_cost_365d": _money_or_unavailable(b.actual_365d.aws_cost),
        "actual_total_cost_365d": _money_or_unavailable(b.actual_365d.total_cost), "forward_dbu_cost_365d": _money_or_unavailable(b.forward_365d.dbu_cost),
        "forward_aws_cost_365d": _money_or_unavailable(b.forward_365d.aws_cost), "forward_total_cost_365d": _money_or_unavailable(b.forward_365d.total_cost),
        "package_base_impact_type": _effect(base),
        "package_low_impact": DISPLAY_UNAVAILABLE if pe.estimated_savings_365d is None else _signed_amount(pe.estimated_savings_365d.low),
        "package_base_impact": DISPLAY_UNAVAILABLE if pe.estimated_savings_365d is None else _signed_amount(pe.estimated_savings_365d.base),
        "package_high_impact": DISPLAY_UNAVAILABLE if pe.estimated_savings_365d is None else _signed_amount(pe.estimated_savings_365d.high),
        "package_cost_low_365d": _range_bound_or_unavailable(pe.estimated_cost_365d, "low"),
        "package_cost_base_365d": _range_bound_or_unavailable(pe.estimated_cost_365d, "base"),
        "package_cost_high_365d": _range_bound_or_unavailable(pe.estimated_cost_365d, "high"), "impact_pct": DISPLAY_UNAVAILABLE if base is None or b.forward_365d.total_cost in (None, 0) else round(float(base) / float(b.forward_365d.total_cost), 6),
        "recommended_scenario_id": _display(package.recommended_plan.scenario_id),
        "recommended_scenario_name": _display(None if recommended is None else recommended.scenario_name),
        "scenario_type": _display(None if recommended is None else _enum(recommended.scenario_type)),
        "scenario_confidence_level": _display(None if recommended is None else _enum(recommended.confidence.level), missing="UNKNOWN"),
        "scenario_confidence_score": _display(None if recommended is None else recommended.confidence.score, missing="UNKNOWN"),
        "next_action": _display(package.recommended_plan.next_action), "requires_hitl": package.recommended_plan.requires_hitl,
        "overall_status": _enum(package.assessment.overall_status), "evidence_quality_status": _enum(package.assessment.evidence_quality.status),
        "evidence_confidence_level": _enum(package.assessment.evidence_quality.confidence.level),
        "candidate_count": summary.candidate_count, "actionable_count": summary.actionable_count, "test_required_count": summary.test_required_count,
        "review_count": summary.review_count, "blocked_count": summary.blocked_count, "no_change_count": summary.no_change_count,
        "not_applicable_count": summary.not_applicable_count, "insufficient_evidence_count": summary.insufficient_evidence_count,
        "candidate_view": candidate_view_text(package), "recommended_execution_plan": execution_plan_text(package),
        "scenario_ids_json": _compact_json_list([s.scenario_id for s in package.scenarios]),
        "candidate_ids_json": _compact_json_list([c.candidate_id for c in package.candidates]),
        "package_file": f"packages/{package.warehouse.workspace_id}__{package.warehouse.warehouse_id}.json",
        "evidence_summary_json": _json({k: {"metric": v.metric, "value": v.value, "interpretation": v.interpretation} for k, v in package.evidence.items()}),
    }
    return _sanitize_row(row)

def project_scenario(package: rc.RecommendationPackage, s: rc.Scenario) -> Dict[str, Any]:
    e, steps = s.portfolio_economics, sorted(s.execution_plan, key=lambda x: x.step)
    invalidations = sorted({_enum(k) for st in steps for k in st.invalidates})
    remeasure_days = max((st.validation.remeasure_days or 0 for st in steps), default=0)
    blockers = [rc.to_primitive(c) for c in s.compatibility.checks if _enum(c.status) != "PASS" or c.requires_hitl]
    row = {
        "recommendation_package_id": package.recommendation_package_id, "scenario_id": s.scenario_id, "workspace_id": package.warehouse.workspace_id,
        "warehouse_id": package.warehouse.warehouse_id, "warehouse_name": package.warehouse.warehouse_name, "owner": _display(package.warehouse.owner, missing="UNKNOWN"),
        "scenario_name": s.scenario_name, "scenario_type": _enum(s.scenario_type), "status": _enum(s.status),
        "recommended_flag": s.scenario_id == package.recommended_plan.scenario_id, "confidence_score": _display(s.confidence.score, missing="UNKNOWN"),
        "confidence_level": _enum(s.confidence.level), "confidence_method": _display(s.confidence.method, missing="UNKNOWN"),
        "compatibility_status": _enum(s.compatibility.status), "compatibility_mode": _enum(s.compatibility.mode),
        "compatibility_checks_json": _json(s.compatibility.checks), "compatibility_blockers_json": _json(blockers),
        "currency": package.cost_baseline.currency, "base_impact_type": _effect(_range_component(e.savings_365d.total, "base")),
        "baseline_dbu_cost": _money_or_unavailable(e.baseline_365d.dbu_cost), "baseline_aws_cost": _money_or_unavailable(e.baseline_365d.aws_cost),
        "baseline_total_cost": _money_or_unavailable(e.baseline_365d.total_cost),
        "dbu_low_impact": _signed_amount(_range_component(e.savings_365d.dbu, "low")), "dbu_base_impact": _signed_amount(_range_component(e.savings_365d.dbu, "base")),
        "dbu_high_impact": _signed_amount(_range_component(e.savings_365d.dbu, "high")),
        "aws_low_impact": _signed_amount(_range_component(e.savings_365d.aws, "low")), "aws_base_impact": _signed_amount(_range_component(e.savings_365d.aws, "base")),
        "aws_high_impact": _signed_amount(_range_component(e.savings_365d.aws, "high")),
        "total_low_impact": _signed_amount(_range_component(e.savings_365d.total, "low")), "total_base_impact": _signed_amount(_range_component(e.savings_365d.total, "base")),
        "total_high_impact": _signed_amount(_range_component(e.savings_365d.total, "high")),
        "estimated_total_cost_low": _range_bound_or_unavailable(e.estimated_cost_365d.total, "low"),
        "estimated_total_cost_base": _range_bound_or_unavailable(e.estimated_cost_365d.total, "base"),
        "estimated_total_cost_high": _range_bound_or_unavailable(e.estimated_cost_365d.total, "high"), "estimation_status": _enum(e.estimation_status),
        "execution_plan": execution_plan_text_for_scenario(s), "execution_plan_json": _json(steps), "candidate_ids_json": _compact_json_list(s.candidate_refs),
        "dependencies_json": _json([rc.to_primitive(d) for st in steps for d in st.dependencies]), "invalidations_json": _json(invalidations),
        "remeasure_required": bool(remeasure_days), "remeasure_days": remeasure_days if remeasure_days else DISPLAY_NA,
    }
    return _sanitize_row(row)

def project_candidate(package: rc.RecommendationPackage, c: rc.CandidateRecommendation) -> Dict[str, Any]:
    e, evidence, thresholds = c.independent_economics, _candidate_evidence(package, c), _candidate_thresholds(package, c)
    realization = _candidate_realization_fields(package, c.candidate_id)
    row = {
        "recommendation_package_id": package.recommendation_package_id, "candidate_id": c.candidate_id, "technique_id": c.technique_id,
        "workspace_id": package.warehouse.workspace_id, "warehouse_id": package.warehouse.warehouse_id, "warehouse_name": package.warehouse.warehouse_name,
        "owner": _display(package.warehouse.owner, missing="UNKNOWN"), "epoch_id": package.warehouse.current_epoch.epoch_id, "knob": _enum(c.knob), "status": _enum(c.status),
        "objectives": ", ".join(_enum(x) for x in c.objectives) or DISPLAY_NA, "current_value": _json(c.current_value) if isinstance(c.current_value, (dict, list)) else _display(c.current_value),
        "recommended_value": _json(c.recommended_value) if isinstance(c.recommended_value, (dict, list)) else _display(c.recommended_value), "rationale": c.rationale,
        "confidence_score": _display(c.confidence.score, missing="UNKNOWN"), "confidence_level": _enum(c.confidence.level),
        "confidence_components_json": _json(c.confidence.components), "evidence_refs_json": _compact_json_list(c.evidence_refs), "evidence_json": _json(evidence),
        "decision_rule": "; ".join(f"{x['metric']} {x['comparator']} {x['threshold']}" for x in thresholds) or "No numeric threshold", "thresholds_json": _json(thresholds),
        "dependencies_json": _compact_json_list(c.dependencies), "invalidates_json": _json([_enum(x) for x in c.invalidates]), "blockers_json": _compact_json_list(c.blockers),
        "economics_available": e.estimation_status != rc.EstimationStatus.UNAVAILABLE, "currency": package.cost_baseline.currency,
        "base_impact_type": _effect(_range_component(e.savings_365d.total, "base")),
        "baseline_dbu_cost": _money_or_unavailable(e.baseline_365d.dbu_cost), "baseline_aws_cost": _money_or_unavailable(e.baseline_365d.aws_cost), "baseline_total_cost": _money_or_unavailable(e.baseline_365d.total_cost),
        "dbu_low_impact": _signed_amount(_range_component(e.savings_365d.dbu, "low")), "dbu_base_impact": _signed_amount(_range_component(e.savings_365d.dbu, "base")),
        "dbu_high_impact": _signed_amount(_range_component(e.savings_365d.dbu, "high")), "aws_low_impact": _signed_amount(_range_component(e.savings_365d.aws, "low")),
        "aws_base_impact": _signed_amount(_range_component(e.savings_365d.aws, "base")), "aws_high_impact": _signed_amount(_range_component(e.savings_365d.aws, "high")),
        "total_low_impact": _signed_amount(_range_component(e.savings_365d.total, "low")), "total_base_impact": _signed_amount(_range_component(e.savings_365d.total, "base")),
        "total_high_impact": _signed_amount(_range_component(e.savings_365d.total, "high")),
        "estimated_total_cost_low": _range_bound_or_unavailable(e.estimated_cost_365d.total, "low"), "estimated_total_cost_base": _range_bound_or_unavailable(e.estimated_cost_365d.total, "base"),
        "estimated_total_cost_high": _range_bound_or_unavailable(e.estimated_cost_365d.total, "high"), "is_additive": e.is_additive,
        "scenario_membership_json": _json(_scenario_membership(package, c.candidate_id)), **realization,
    }
    return _sanitize_row(row)

def project_executive(packages: Sequence[rc.RecommendationPackage], portfolio_rows: Sequence[Mapping[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    currencies = sorted({p.cost_baseline.currency for p in packages})
    same_currency = len(currencies) == 1
    currency = currencies[0] if same_currency else "MIXED"

    def _sum_known(values: Sequence[Optional[float]]) -> Optional[float]:
        if not same_currency or any(v is None for v in values):
            return None
        return round(sum(float(v) for v in values), 2)

    actual_totals = [p.cost_baseline.actual_365d.total_cost for p in packages]
    forward_totals = [p.cost_baseline.forward_365d.total_cost for p in packages]
    package_ranges = [p.package_economics.estimated_savings_365d for p in packages]
    assessable = [r for r in package_ranges if r is not None]
    actual_total = _sum_known(actual_totals)
    forward_total = _sum_known(forward_totals)

    lows = [float(r.low) for r in assessable]
    bases = [float(r.base) for r in assessable]
    highs = [float(r.high) for r in assessable]
    downside = round(sum(lows), 2) if same_currency and len(assessable) == len(packages) else None
    upside = round(sum(highs), 2) if same_currency and len(assessable) == len(packages) else None
    recommended_savings = round(sum(max(v, 0.0) for v in bases), 2) if same_currency else None
    recommended_cost_increase = round(sum(max(-v, 0.0) for v in bases), 2) if same_currency else None
    net = None if recommended_savings is None else round(recommended_savings - (recommended_cost_increase or 0.0), 2)


    metrics = [
        {"section":"Coverage","metric":"Workbook contract","value":WORKBOOK_CONTRACT_VERSION,"currency":DISPLAY_NA,"notes":"Compact projection of Reporter JSON Contract v1.0.0"},
        {"section":"Coverage","metric":"Reporter contract","value":rc.CONTRACT_VERSION,"currency":DISPLAY_NA,"notes":"Locked JSON source of truth"},
        {"section":"Coverage","metric":"Warehouses assessed","value":len(packages),"currency":DISPLAY_NA,"notes":"One immutable package per warehouse/current epoch"},
        {"section":"Coverage","metric":"Workspaces","value":len({p.warehouse.workspace_id for p in packages}),"currency":DISPLAY_NA,"notes":DISPLAY_NA},
        {"section":"Coverage","metric":"Regions","value":len({p.warehouse.region for p in packages}),"currency":DISPLAY_NA,"notes":DISPLAY_NA},
        {"section":"Coverage","metric":"Economically assessable","value":len(assessable),"currency":DISPLAY_NA,"notes":f"Excluded: {len(packages)-len(assessable)}"},
        {"section":"Cost","metric":"Actual trailing 365d","value":_money_or_unavailable(actual_total),"currency":currency,"notes":"UNAVAILABLE when any package lacks historical AWS total"},
        {"section":"Cost","metric":"Forward 365d baseline","value":_money_or_unavailable(forward_total),"currency":currency,"notes":"No-change forecast"},
        {"section":"Economic impact","metric":"Recommended annual savings","value":_money_or_unavailable(recommended_savings),"currency":currency,"notes":"Sum of positive recommended-scenario base impacts"},
        {"section":"Economic impact","metric":"Recommended annual cost increase","value":_money_or_unavailable(recommended_cost_increase),"currency":currency,"notes":"Absolute sum of negative recommended-scenario base impacts"},
        {"section":"Economic impact","metric":"Recommended net annual impact","value":_money_or_unavailable(net),"currency":currency,"notes":"Positive = net savings; negative = net cost increase"},
        {"section":"Economic impact","metric":"Scenario envelope low","value":_money_or_unavailable(downside),"currency":currency,"notes":"Lowest package-level modeled impact across the portfolio"},
        {"section":"Economic impact","metric":"Scenario envelope high","value":_money_or_unavailable(upside),"currency":currency,"notes":"Highest package-level modeled impact across the portfolio"},
        {"section":"Confidence","metric":"HIGH recommended scenario","value":sum(1 for r in portfolio_rows if r.get("scenario_confidence_level")=="HIGH"),"currency":DISPLAY_NA,"notes":DISPLAY_NA},
        {"section":"Confidence","metric":"MEDIUM recommended scenario","value":sum(1 for r in portfolio_rows if r.get("scenario_confidence_level")=="MEDIUM"),"currency":DISPLAY_NA,"notes":DISPLAY_NA},
        {"section":"Confidence","metric":"LOW recommended scenario","value":sum(1 for r in portfolio_rows if r.get("scenario_confidence_level")=="LOW"),"currency":DISPLAY_NA,"notes":DISPLAY_NA},
    ]

    package_by_id = {p.recommendation_package_id: p for p in packages}
    opportunities = []
    for source in portfolio_rows:
        r = dict(source)
        package = package_by_id[r["recommendation_package_id"]]
        savings_range = package.package_economics.estimated_savings_365d
        base = None if savings_range is None else float(savings_range.base)
        r["recommended_impact_type"] = _effect(base)
        r["recommended_annual_impact"] = _signed_amount(base)
        r["scenario_low_impact"] = DISPLAY_UNAVAILABLE if savings_range is None else _signed_amount(savings_range.low)
        r["scenario_high_impact"] = DISPLAY_UNAVAILABLE if savings_range is None else _signed_amount(savings_range.high)
        r["currency"] = package.package_economics.currency
        r["savings_amount"] = max(float(base or 0.0), 0.0) if base is not None else 0.0
        r["cost_increase_amount"] = max(-float(base or 0.0), 0.0) if base is not None else 0.0
        opportunities.append(r)
    rank = {ECON_SAVINGS: 3, ECON_NO_CHANGE: 2, ECON_COST_INCREASE: 1, ECON_UNAVAILABLE: 0}
    opportunities.sort(key=lambda r: (rank.get(str(r.get("recommended_impact_type")), 0), abs(float(r.get("recommended_annual_impact") or 0.0)) if isinstance(r.get("recommended_annual_impact"), (int, float)) else 0.0), reverse=True)
    opportunities = opportunities[:15]
    return [_sanitize_row(x) for x in metrics], [_sanitize_row(x) for x in opportunities]


def project_workbook(packages: Sequence[rc.RecommendationPackage]) -> WorkbookProjection:
    portfolio = [project_warehouse_portfolio(p) for p in packages]
    scenarios = [project_scenario(p, s) for p in packages for s in p.scenarios]
    registry = [project_candidate(p, c) for p in packages for c in p.candidates]
    metrics, opportunities = project_executive(packages, portfolio)
    result = WorkbookProjection(metrics, opportunities, portfolio, scenarios, registry)
    result.validate(packages)
    return result
