"""Artifact-tool workbook reporter for Workbook Contract v1.1.0.

This module is presentation-only. It consumes already validated locked
RecommendationPackage objects and never changes recommendation semantics.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from artifact_tool import SpreadsheetFile, Workbook

from contracts import reporter_contract as rc
from contracts import workbook_contract as wc

ESPRESSO = "#3B2F2F"
ESPRESSO_2 = "#5A463B"
CREAM = "#F7F1E8"
CREAM_2 = "#EFE4D4"
GOLD = "#B58A3A"
GREEN = "#DDEBDD"
AMBER = "#FFF0CC"
RED = "#F8D7DA"
BLUE = "#DDEAF6"
GRAY = "#E9E7E4"
WHITE = "#FFFFFF"
TEXT = "#2B2522"
MONEY_FMT = '$#,##0.00;[Red]-$#,##0.00'
PCT_FMT = '0.0%'
INT_FMT = '#,##0'
DECIMAL_FMT = '0.00'


def _col_letter(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _normalize(v: Any) -> Any:
    if hasattr(v, "value"):
        return v.value
    return v


def _write_table(
    sheet: Any,
    *,
    start_row: int,
    columns: Sequence[Tuple[str, str]],
    rows: Sequence[Mapping[str, Any]],
    table_name: str,
    widths: Optional[Mapping[str, float]] = None,
    wrap_keys: Optional[Sequence[str]] = None,
    money_keys: Optional[Sequence[str]] = None,
    pct_keys: Optional[Sequence[str]] = None,
    int_keys: Optional[Sequence[str]] = None,
    decimal_keys: Optional[Sequence[str]] = None,
    data_row_height: float = 36.0,
) -> None:
    """Write a filterable table. start_row is 1-based header row."""
    widths = dict(widths or {})
    wrap = set(wrap_keys or [])
    money = set(money_keys or [])
    pct = set(pct_keys or [])
    ints = set(int_keys or [])
    decimals = set(decimal_keys or [])
    headers = [h for _, h in columns]
    matrix = [headers] + [[_normalize(r.get(k)) for k, _ in columns] for r in rows]
    end_row = start_row + len(rows)
    end_col = len(columns)
    rng = sheet.get_range(f"A{start_row}:{_col_letter(end_col)}{end_row}")
    rng.values = matrix
    header = sheet.get_range(f"A{start_row}:{_col_letter(end_col)}{start_row}")
    header.format = {
        "fill": ESPRESSO,
        "font": {"bold": True, "color": WHITE, "size": 10},
        "vertical_alignment": "center",
        "wrap_text": True,
    }
    header.format.row_height = 34
    data = sheet.get_range(f"A{start_row+1}:{_col_letter(end_col)}{end_row}") if rows else None
    if data is not None:
        data.format.font = {"color": TEXT, "size": 9}
        data.format.vertical_alignment = "top"
        data.format.row_height = data_row_height
    for idx, (key, _header) in enumerate(columns, start=1):
        letter = _col_letter(idx)
        col_rng = sheet.get_range(f"{letter}{start_row}:{letter}{end_row}")
        col_rng.format.column_width = widths.get(key, 15)
        if key in wrap:
            col_rng.format.wrap_text = True
        if key in money:
            col_rng.format.number_format = MONEY_FMT
        elif key in pct:
            col_rng.format.number_format = PCT_FMT
        elif key in ints:
            col_rng.format.number_format = INT_FMT
        elif key in decimals:
            col_rng.format.number_format = DECIMAL_FMT
    if rows:
        table = sheet.tables.add(f"A{start_row}:{_col_letter(end_col)}{end_row}", True, table_name)
        # Keep table styling modest; explicit header formatting is the visual anchor.
        try:
            table.style = "TableStyleMedium2"
        except Exception:
            pass


def _sheet_title(sheet: Any, title: str, subtitle: str, end_col: int) -> None:
    end = _col_letter(max(end_col, 8))
    sheet.merge_cells(f"A1:{end}1")
    sheet.get_range("A1").values = [[title]]
    sheet.get_range(f"A1:{end}1").format = {
        "fill": ESPRESSO,
        "font": {"bold": True, "color": WHITE, "size": 18},
        "vertical_alignment": "center",
    }
    sheet.get_range(f"A1:{end}1").format.row_height = 30
    sheet.merge_cells(f"A2:{end}2")
    sheet.get_range("A2").values = [[subtitle]]
    sheet.get_range(f"A2:{end}2").format = {
        "fill": CREAM,
        "font": {"color": ESPRESSO_2, "italic": True, "size": 10},
        "wrap_text": True,
    }
    sheet.get_range(f"A2:{end}2").format.row_height = 28


def _apply_status_formatting(sheet: Any, key_to_col: Mapping[str, int], start_row: int, end_row: int) -> None:
    for key in ("status", "overall_status", "evidence_quality_status", "scenario_confidence_level", "confidence_level"):
        col = key_to_col.get(key)
        if not col or end_row < start_row:
            continue
        letter = _col_letter(col)
        rng = sheet.get_range(f"{letter}{start_row}:{letter}{end_row}")
        for word, fill in [
            ("ACTIONABLE", GREEN), ("RECOMMENDED", GREEN), ("HIGH", GREEN), ("SUFFICIENT", GREEN),
            ("TEST_REQUIRED", AMBER), ("REVIEW", AMBER), ("MEDIUM", AMBER), ("PARTIAL", AMBER),
            ("BLOCKED", RED), ("LOW", RED), ("INSUFFICIENT_EVIDENCE", RED),
            ("NO_CHANGE", GRAY), ("NOT_APPLICABLE", GRAY), ("ALTERNATIVE", BLUE),
        ]:
            rng.conditional_formats.add_custom(f'={letter}{start_row}="{word}"', {"fill": fill})


def _build_executive(wb: Any, projection: wc.WorkbookProjection, packages: Sequence[rc.RecommendationPackage]) -> None:
    sheet = wb.worksheets.add(wc.SHEET_EXECUTIVE)
    _sheet_title(sheet, "SQL Warehouse Optimization — Executive Summary", "Portfolio view derived exclusively from Reporter JSON Contract v1.0.0. Candidate savings are non-additive; portfolio economics come from package/scenario envelopes.", 14)

    metrics = projection.executive_metrics
    metric_cols = [("section","Section"),("metric","Metric"),("value","Value"),("currency","Currency"),("notes","Notes")]
    _write_table(sheet, start_row=4, columns=metric_cols, rows=metrics, table_name="ExecutiveMetrics",
                 widths={"section":14,"metric":30,"value":18,"currency":10,"notes":48}, wrap_keys=["notes"])
    # Format the metric values individually because the column contains mixed semantic types.
    for i, row in enumerate(metrics, start=5):
        metric = row["metric"]
        cell = sheet.get_range(f"C{i}")
        if "savings %" in metric.lower():
            cell.format.number_format = PCT_FMT
        elif row.get("currency") and isinstance(row.get("value"), (int, float)):
            cell.format.number_format = MONEY_FMT
        elif isinstance(row.get("value"), int):
            cell.format.number_format = INT_FMT

    opp_start = 4
    opp_cols = [
        ("warehouse_name","Warehouse"),("owner","Owner"),("forward_total_cost_365d","Forward baseline"),
        ("package_savings_base_365d","Base savings"),("savings_range","Savings range"),("base_savings_pct","Savings %"),
        ("recommended_scenario_name","Recommended plan"),("scenario_confidence_level","Confidence"),("next_action","Next action")
    ]
    # Put top opportunities at G4 rather than A; helper writes at A, so write manually with an offset.
    headers=[h for _,h in opp_cols]
    data=[headers]+[[_normalize(r.get(k)) for k,_ in opp_cols] for r in projection.executive_opportunities]
    end_row=opp_start+len(projection.executive_opportunities)
    sheet.get_range(f"G{opp_start}:O{end_row}").values=data
    sheet.get_range(f"G{opp_start}:O{opp_start}").format={"fill":GOLD,"font":{"bold":True,"color":WHITE,"size":10},"wrap_text":True}
    sheet.get_range(f"G{opp_start}:O{opp_start}").format.row_height=34
    widths=[24,24,16,16,18,12,34,12,28]
    for j,w in enumerate(widths,start=7): sheet.get_range(f"{_col_letter(j)}{opp_start}:{_col_letter(j)}{end_row}").format.column_width=w
    for c in ("I","J"): sheet.get_range(f"{c}{opp_start+1}:{c}{end_row}").format.number_format=MONEY_FMT
    sheet.get_range(f"L{opp_start+1}:L{end_row}").format.number_format=PCT_FMT
    for c in ("G","H","K","M","N","O"): sheet.get_range(f"{c}{opp_start+1}:{c}{end_row}").format.wrap_text=True
    if projection.executive_opportunities:
        t=sheet.tables.add(f"G{opp_start}:O{end_row}",True,"TopOpportunities")
        try: t.style="TableStyleMedium4"
        except Exception: pass
        sheet.get_range(f"N{opp_start+1}:N{end_row}").conditional_formats.add_custom(f'=N{opp_start+1}="HIGH"',{"fill":GREEN})
        sheet.get_range(f"N{opp_start+1}:N{end_row}").conditional_formats.add_custom(f'=N{opp_start+1}="MEDIUM"',{"fill":AMBER})
        sheet.get_range(f"N{opp_start+1}:N{end_row}").conditional_formats.add_custom(f'=N{opp_start+1}="LOW"',{"fill":RED})

    # Visual: top opportunities by base annual savings.
    if projection.executive_opportunities:
        chart = sheet.charts.add("bar", {"title":"Top annual savings opportunities","has_legend":False})
        series = chart.series.add("Base annual savings")
        series.category_formula = f"'{wc.SHEET_EXECUTIVE}'!$G$5:$G${end_row}"
        series.formula = f"'{wc.SHEET_EXECUTIVE}'!$J$5:$J${end_row}"
        series.values_format_code = "$#,##0"
        chart.set_position("G22","O40")
        try:
            chart.x_axis.number_format_code = "$#,##0"
        except Exception:
            pass

    sheet.freeze_panes.freeze_rows(3)
    sheet.get_range("A1:O40").format.vertical_alignment="top"


def _build_portfolio(wb: Any, projection: wc.WorkbookProjection) -> None:
    rows = projection.warehouse_portfolio
    sheet = wb.worksheets.add(wc.SHEET_PORTFOLIO)
    cols = [
        ("recommendation_package_id","Package ID"),("workspace_id","Workspace ID"),("warehouse_id","Warehouse ID"),("warehouse_name","Warehouse"),("owner","Owner"),("region","Region"),
        ("current_epoch_id","Epoch ID"),("epoch_age_days","Epoch age (d)"),("primary_query_source","Primary source"),("queries_30d","Queries 30d"),("active_days_30d","Active days 30d"),
        ("warehouse_type","Type"),("warehouse_size","Size"),("min_clusters","Min clusters"),("max_clusters","Max clusters"),("auto_stop_minutes","Auto stop min"),("warehouse_channel","Channel"),("photon_status","Photon"),
        ("actual_dbu_cost_365d","Actual DBU 365d"),("actual_aws_cost_365d","Actual AWS 365d"),("actual_total_cost_365d","Actual total 365d"),
        ("forward_dbu_cost_365d","Forward DBU 365d"),("forward_aws_cost_365d","Forward AWS 365d"),("forward_total_cost_365d","Forward total 365d"),
        ("package_savings_low_365d","Savings low"),("package_savings_base_365d","Savings base"),("package_savings_high_365d","Savings high"),("base_savings_pct","Base savings %"),
        ("recommended_scenario_id","Recommended scenario ID"),("recommended_scenario_name","Recommended scenario"),("scenario_type","Scenario type"),("scenario_confidence_level","Scenario conf."),("scenario_confidence_score","Scenario score"),
        ("next_action","Next action"),("requires_hitl","HITL"),("overall_status","Overall status"),("evidence_quality_status","Evidence quality"),("evidence_confidence_level","Evidence conf."),
        ("candidate_count","Candidates"),("actionable_count","Actionable"),("test_required_count","Test required"),("blocked_count","Blocked"),("no_change_count","No change"),
        ("candidate_view","Candidate view"),("recommended_execution_plan","Recommended execution plan"),("scenario_ids_json","Scenario IDs JSON"),("candidate_ids_json","Candidate IDs JSON"),("evidence_summary_json","Evidence summary JSON"),
    ]
    _sheet_title(sheet,"Warehouse Portfolio","One row per recommendation package. Filterable current config, workload, economics, recommended scenario, and compact HITL decision views.",len(cols))
    widths={"recommendation_package_id":28,"workspace_id":18,"warehouse_id":18,"warehouse_name":24,"owner":24,"current_epoch_id":24,"recommended_scenario_id":24,"recommended_scenario_name":32,"next_action":28,"candidate_view":42,"recommended_execution_plan":52,"scenario_ids_json":34,"candidate_ids_json":34,"evidence_summary_json":45}
    wrap=["warehouse_name","owner","recommended_scenario_name","next_action","candidate_view","recommended_execution_plan"]
    money=[k for k,_ in cols if "cost_365d" in k or "savings_" in k and k!="base_savings_pct"]
    pct=["base_savings_pct"]
    ints=["epoch_age_days","queries_30d","active_days_30d","min_clusters","max_clusters","auto_stop_minutes","candidate_count","actionable_count","test_required_count","blocked_count","no_change_count"]
    decimals=["scenario_confidence_score"]
    _write_table(sheet,start_row=4,columns=cols,rows=rows,table_name="WarehousePortfolio",widths=widths,wrap_keys=wrap,money_keys=money,pct_keys=pct,int_keys=ints,decimal_keys=decimals,data_row_height=84)
    sheet.freeze_panes.freeze_rows(4); sheet.freeze_panes.freeze_columns(4)
    keycol={k:i+1 for i,(k,_) in enumerate(cols)}
    _apply_status_formatting(sheet,keycol,5,4+len(rows))
    if rows:
        col=keycol["base_savings_pct"]; letter=_col_letter(col)
        sheet.get_range(f"{letter}5:{letter}{4+len(rows)}").conditional_formats.add_data_bar({"color":"accent5","gradient":True})


def _build_scenarios(wb: Any, projection: wc.WorkbookProjection) -> None:
    rows=projection.scenario_plans
    sheet=wb.worksheets.add(wc.SHEET_SCENARIOS)
    cols=[
        ("recommendation_package_id","Package ID"),("scenario_id","Scenario ID"),("workspace_id","Workspace ID"),("warehouse_id","Warehouse ID"),("warehouse_name","Warehouse"),("owner","Owner"),
        ("scenario_name","Scenario"),("scenario_type","Type"),("status","Status"),("recommended_flag","Recommended"),("confidence_score","Confidence score"),("confidence_level","Confidence"),("confidence_method","Confidence method"),
        ("compatibility_status","Compatibility"),("compatibility_mode","Compatibility mode"),("compatibility_checks_json","Compatibility checks JSON"),
        ("baseline_dbu_cost","Baseline DBU"),("baseline_aws_cost","Baseline AWS"),("baseline_total_cost","Baseline total"),
        ("dbu_savings_low","DBU save low"),("dbu_savings_base","DBU save base"),("dbu_savings_high","DBU save high"),
        ("aws_savings_low","AWS save low"),("aws_savings_base","AWS save base"),("aws_savings_high","AWS save high"),
        ("total_savings_low","Total save low"),("total_savings_base","Total save base"),("total_savings_high","Total save high"),
        ("estimated_total_cost_low","Est. cost low"),("estimated_total_cost_base","Est. cost base"),("estimated_total_cost_high","Est. cost high"),("estimation_status","Estimation status"),
        ("execution_plan","Execution plan"),("execution_plan_json","Execution plan JSON"),("candidate_ids_json","Candidate IDs JSON"),("dependencies_json","Dependencies JSON"),("invalidations_json","Invalidations JSON"),("remeasure_required","Remeasure"),("remeasure_days","Remeasure days"),
    ]
    _sheet_title(sheet,"Scenario Plans","One row per scenario. Compare Plan A/Plan B alternatives, compatibility, stateful economics, confidence, dependencies, invalidations, and execution sequence.",len(cols))
    widths={"recommendation_package_id":28,"scenario_id":26,"workspace_id":18,"warehouse_id":18,"warehouse_name":24,"owner":24,"scenario_name":34,"confidence_method":28,"compatibility_checks_json":42,"execution_plan":52,"execution_plan_json":48,"candidate_ids_json":36,"dependencies_json":36,"invalidations_json":32}
    wrap=["warehouse_name","owner","scenario_name","confidence_method","execution_plan"]
    money=[k for k,_ in cols if "cost" in k or "savings" in k]
    decimals=["confidence_score"]
    ints=["remeasure_days"]
    _write_table(sheet,start_row=4,columns=cols,rows=rows,table_name="ScenarioPlans",widths=widths,wrap_keys=wrap,money_keys=money,decimal_keys=decimals,int_keys=ints,data_row_height=66)
    sheet.freeze_panes.freeze_rows(4); sheet.freeze_panes.freeze_columns(5)
    keycol={k:i+1 for i,(k,_) in enumerate(cols)}
    _apply_status_formatting(sheet,keycol,5,4+len(rows))


def _build_registry(wb: Any, projection: wc.WorkbookProjection) -> None:
    rows=projection.recommendation_registry
    sheet=wb.worksheets.add(wc.SHEET_REGISTRY)
    cols=[
        ("recommendation_package_id","Package ID"),("candidate_id","Candidate ID"),("technique_id","Technique ID"),("workspace_id","Workspace ID"),("warehouse_id","Warehouse ID"),("warehouse_name","Warehouse"),("owner","Owner"),("epoch_id","Epoch ID"),
        ("knob","Knob"),("status","Status"),("objectives","Objectives"),("current_value","Current"),("recommended_value","Recommended"),("rationale","Rationale"),
        ("confidence_score","Confidence score"),("confidence_level","Confidence"),("confidence_components_json","Confidence components JSON"),
        ("evidence_refs_json","Evidence refs"),("evidence_json","Evidence / findings JSON"),("decision_rule","Decision rule"),("thresholds_json","Thresholds JSON"),("dependencies_json","Dependencies JSON"),("invalidates_json","Invalidates JSON"),("blockers_json","Blockers JSON"),
        ("economics_available","Economics available"),("baseline_dbu_cost","Baseline DBU"),("baseline_aws_cost","Baseline AWS"),("baseline_total_cost","Baseline total"),
        ("dbu_savings_low","DBU save low"),("dbu_savings_base","DBU save base"),("dbu_savings_high","DBU save high"),("aws_savings_low","AWS save low"),("aws_savings_base","AWS save base"),("aws_savings_high","AWS save high"),("total_savings_low","Total save low"),("total_savings_base","Total save base"),("total_savings_high","Total save high"),
        ("estimated_total_cost_low","Est. cost low"),("estimated_total_cost_base","Est. cost base"),("estimated_total_cost_high","Est. cost high"),("is_additive","Additive?"),("scenario_membership_json","Scenario membership JSON"),
        ("realization_status","Realization"),("applied_at","Applied at"),("validation_status","Validation"),("realized_dbu_savings","Realized DBU save"),("realized_aws_savings","Realized AWS save"),("realized_total_savings","Realized total save"),("annualized_realized_savings","Annualized realized save"),("rollback_required","Rollback?"),("realization_records_json","Realization JSON"),
    ]
    _sheet_title(sheet,"Recommendation Registry","One row per candidate. This is the detailed HITL/audit surface: technique, rationale, confidence, evidence/findings, thresholds, blockers, independent economics, scenario membership, and realization.",len(cols))
    widths={"recommendation_package_id":28,"candidate_id":26,"technique_id":28,"workspace_id":18,"warehouse_id":18,"warehouse_name":24,"owner":24,"epoch_id":24,"objectives":20,"current_value":20,"recommended_value":20,"rationale":52,"confidence_components_json":35,"evidence_refs_json":28,"evidence_json":58,"decision_rule":42,"thresholds_json":42,"dependencies_json":32,"invalidates_json":28,"blockers_json":36,"scenario_membership_json":38,"realization_records_json":42}
    wrap=["warehouse_name","owner","objectives","current_value","recommended_value","rationale","decision_rule"]
    money=[k for k,_ in cols if "cost" in k or "savings" in k]
    decimals=["confidence_score"]
    _write_table(sheet,start_row=4,columns=cols,rows=rows,table_name="RecommendationRegistry",widths=widths,wrap_keys=wrap,money_keys=money,decimal_keys=decimals,data_row_height=54)
    sheet.freeze_panes.freeze_rows(4); sheet.freeze_panes.freeze_columns(5)
    keycol={k:i+1 for i,(k,_) in enumerate(cols)}
    _apply_status_formatting(sheet,keycol,5,4+len(rows))


def write_workbook(path: str | Path, packages: Sequence[rc.RecommendationPackage]) -> Path:
    """Generate the locked four-sheet workbook projection and save it to *path*."""
    for p in packages:
        p.validate()
    projection = wc.project_workbook(packages)
    wb = Workbook.create()
    _build_executive(wb, projection, packages)
    _build_portfolio(wb, projection)
    _build_scenarios(wb, projection)
    _build_registry(wb, projection)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    SpreadsheetFile.export_xlsx(wb).save(str(target))
    return target
