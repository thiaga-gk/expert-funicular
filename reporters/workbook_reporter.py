"""Excel workbook reporter for Workbook Contract v1.2.0.

The workbook is a presentation-only projection of locked Reporter JSON Contract
v1.0.0.  Exact detailed-sheet columns and section groups are owned by
``contracts.workbook_contract``; this renderer cannot silently drop fields.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from artifact_tool import SpreadsheetFile, Workbook

from contracts import reporter_contract as rc
from contracts import workbook_contract as wc

ESPRESSO = "#3B2F2F"
ESPRESSO_2 = "#5A463B"
CREAM = "#F7F1E8"
CREAM_2 = "#EEE3D3"
GOLD = "#B58A3A"
GREEN = "#DDEBDD"
AMBER = "#FFF0CC"
RED = "#F8D7DA"
BLUE = "#DDEAF6"
GRAY = "#E9E7E4"
WHITE = "#FFFFFF"
TEXT = "#2B2522"
MUTED = "#6B625D"
MONEY_FMT = wc.MONEY_FORMAT
PCT_FMT = wc.PERCENT_FORMAT
INT_FMT = '#,##0'
DECIMAL_FMT = '0.00'


def _col_letter(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _normalize(v: Any) -> Any:
    if v is None:
        return wc.DISPLAY_NA
    if hasattr(v, "value"):
        return v.value
    return v


def _excel_text(v: Any) -> Any:
    """Preserve long digit-only identifiers as exact visible Excel text.

    artifact_tool/Excel can coerce a digit-only string to a numeric cell even
    when the column uses the Text number format. A leading apostrophe is the
    standard Excel text-entry marker and is not displayed to the user. This is
    a renderer-only representation; contract/projection values remain unchanged.
    """
    value = _normalize(v)
    if isinstance(value, str) and value.isdigit() and len(value) >= 12:
        return "'" + value
    return value


def _sheet_title(sheet: Any, title: str, subtitle: str, end_col: int) -> None:
    end = _col_letter(max(end_col, 8))
    sheet.merge_cells(f"A1:{end}1")
    sheet.get_range("A1").values = [[title]]
    sheet.get_range(f"A1:{end}1").format = {
        "fill": ESPRESSO,
        "font": {"bold": True, "color": WHITE, "size": 16},
        "vertical_alignment": "center",
    }
    sheet.get_range(f"A1:{end}1").format.row_height = 28
    sheet.merge_cells(f"A2:{end}2")
    sheet.get_range("A2").values = [[subtitle]]
    sheet.get_range(f"A2:{end}2").format = {
        "fill": CREAM,
        "font": {"color": ESPRESSO_2, "italic": True, "size": 9},
        "wrap_text": True,
        "vertical_alignment": "center",
    }
    sheet.get_range(f"A2:{end}2").format.row_height = 24


def _write_group_row(sheet: Any, columns: Sequence[wc.ColumnSpec], groups: Sequence[wc.ColumnGroup], row: int = 3) -> None:
    by_key = {c.key: idx + 1 for idx, c in enumerate(columns)}
    for i, group in enumerate(groups):
        start = by_key[group.start_key]
        end = by_key[group.end_key]
        start_letter, end_letter = _col_letter(start), _col_letter(end)
        if start != end:
            sheet.merge_cells(f"{start_letter}{row}:{end_letter}{row}")
        cell = sheet.get_range(f"{start_letter}{row}:{end_letter}{row}")
        cell.values = [[group.title]] if start == end else None
        if start != end:
            sheet.get_range(f"{start_letter}{row}").values = [[group.title]]
        cell.format = {
            "fill": GOLD if i % 2 == 0 else ESPRESSO_2,
            "font": {"bold": True, "color": WHITE, "size": 9},
            "horizontal_alignment": "center",
            "vertical_alignment": "center",
        }
    end_letter = _col_letter(len(columns))
    sheet.get_range(f"A{row}:{end_letter}{row}").format.row_height = 20


def _write_contract_table(
    sheet: Any,
    *,
    columns: Sequence[wc.ColumnSpec],
    groups: Sequence[wc.ColumnGroup],
    rows: Sequence[Mapping[str, Any]],
    table_name: str,
    freeze_columns: int,
    wrap_keys: Sequence[str] = (),
    json_keys: Sequence[str] = (),
    row_height: float = 24,
) -> None:
    _write_group_row(sheet, columns, groups, 3)
    headers = [c.header for c in columns]
    matrix = [headers] + [
        [(_excel_text(row[c.key]) if c.number_format == "@" else _normalize(row[c.key])) for c in columns]
        for row in rows
    ]
    end_row = 4 + len(rows)
    end_col = len(columns)
    end_letter = _col_letter(end_col)

    # Excel can coerce long digit-only identifiers (for example workspace IDs)
    # to numbers/scientific notation if the text format is applied only after
    # assigning values. Pre-format contract text columns before writing.
    for idx, spec in enumerate(columns, start=1):
        if spec.number_format == "@":
            letter = _col_letter(idx)
            sheet.get_range(f"{letter}4:{letter}{end_row}").format.number_format = "@"

    sheet.get_range(f"A4:{end_letter}{end_row}").values = matrix
    header = sheet.get_range(f"A4:{end_letter}4")
    header.format = {
        "fill": ESPRESSO,
        "font": {"bold": True, "color": WHITE, "size": 9},
        "horizontal_alignment": "center",
        "vertical_alignment": "center",
        "wrap_text": True,
    }
    header.format.row_height = 34

    if rows:
        data = sheet.get_range(f"A5:{end_letter}{end_row}")
        data.format.font = {"color": TEXT, "size": 9}
        data.format.vertical_alignment = "center"
        data.format.row_height = row_height

    wrap = set(wrap_keys)
    jsons = set(json_keys)
    center_keys = {
        "warehouse_type", "warehouse_size", "min_clusters", "max_clusters", "warehouse_channel", "photon_status",
        "requires_hitl", "scenario_confidence_level", "evidence_confidence_level", "status", "recommended_flag",
        "confidence_level", "compatibility_status", "remeasure_required", "economics_available", "is_additive",
        "realization_status", "validation_status", "rollback_required", "knob", "package_base_impact_type",
        "base_impact_type", "recommended_impact_type",
    }

    for idx, spec in enumerate(columns, start=1):
        letter = _col_letter(idx)
        col = sheet.get_range(f"{letter}4:{letter}{end_row}")
        col.format.column_width = spec.width
        if spec.number_format:
            col.format.number_format = spec.number_format
        elif spec.key.endswith("_json") or spec.key == "package_file":
            col.format.number_format = "@"
        if spec.key in wrap:
            col.format.wrap_text = True
            col.format.vertical_alignment = "top"
        if spec.key in center_keys:
            col.format.horizontal_alignment = "center"
        if spec.key in jsons:
            col.format.font = {"color": MUTED, "size": 8}
            col.format.wrap_text = False
        if spec.number_format:
            col.format.horizontal_alignment = "right"

    if rows:
        table = sheet.tables.add(f"A4:{end_letter}{end_row}", True, table_name)
        try:
            table.style = "TableStyleMedium2"
        except Exception:
            pass

    sheet.freeze_panes.freeze_rows(4)
    sheet.freeze_panes.freeze_columns(freeze_columns)
    sheet.get_range(f"A1:{end_letter}{end_row}").format.vertical_alignment = "center"
    _apply_status_formatting(sheet, {c.key: i + 1 for i, c in enumerate(columns)}, 5, end_row)


def _apply_status_formatting(sheet: Any, key_to_col: Mapping[str, int], start_row: int, end_row: int) -> None:
    if end_row < start_row:
        return
    for key in (
        "status", "overall_status", "evidence_quality_status", "scenario_confidence_level", "evidence_confidence_level",
        "confidence_level", "realization_status", "validation_status", "compatibility_status",
        "package_base_impact_type", "base_impact_type", "recommended_impact_type",
    ):
        col = key_to_col.get(key)
        if not col:
            continue
        letter = _col_letter(col)
        rng = sheet.get_range(f"{letter}{start_row}:{letter}{end_row}")
        for word, fill in [
            ("ACTIONABLE", GREEN), ("RECOMMENDED", GREEN), ("HIGH", GREEN), ("SUFFICIENT", GREEN), ("PASS", GREEN), ("REALIZED", GREEN),
            ("TEST_REQUIRED", AMBER), ("REVIEW", AMBER), ("MEDIUM", AMBER), ("PARTIAL", AMBER), ("REQUIRES_VALIDATION", AMBER), ("VALIDATING", AMBER),
            ("BLOCKED", RED), ("LOW", RED), ("INSUFFICIENT_EVIDENCE", RED), ("FAILED_VALIDATION", RED), ("ROLLED_BACK", RED),
            ("SAVINGS", GREEN), ("COST_INCREASE", RED), ("MIXED", AMBER),
            ("NO_CHANGE", GRAY), ("NOT_APPLICABLE", GRAY), ("ALTERNATIVE", BLUE), ("UNAVAILABLE", GRAY), ("NOT_APPLIED", GRAY), ("UNKNOWN", GRAY),
        ]:
            rng.conditional_formats.add_custom(f'={letter}{start_row}="{word}"', {"fill": fill})

    # Boolean flags that matter operationally.
    for key, fill_true in (("requires_hitl", AMBER), ("recommended_flag", GREEN), ("remeasure_required", AMBER), ("rollback_required", RED)):
        col = key_to_col.get(key)
        if col:
            letter = _col_letter(col)
            sheet.get_range(f"{letter}{start_row}:{letter}{end_row}").conditional_formats.add_custom(
                f"={letter}{start_row}=TRUE", {"fill": fill_true}
            )


def _build_executive(wb: Any, projection: wc.WorkbookProjection) -> None:
    sheet = wb.worksheets.add(wc.SHEET_EXECUTIVE)
    _sheet_title(
        sheet,
        "SQL Warehouse Optimization — Executive Summary",
        "Decision-ready portfolio view from Reporter JSON Contract v1.1.0. Financial cells are numeric; impact direction is explicit and sortable.",
        20,
    )
    # Section labels make the side-by-side layout obvious.
    sheet.merge_cells("A3:E3")
    sheet.get_range("A3").values = [["Portfolio metrics"]]
    sheet.get_range("A3:E3").format = {"fill": GOLD, "font": {"bold": True, "color": WHITE, "size": 9}, "horizontal_alignment": "center"}
    sheet.merge_cells("G3:T3")
    sheet.get_range("G3").values = [["Top warehouse impacts (up to 15)"]]
    sheet.get_range("G3:T3").format = {"fill": ESPRESSO_2, "font": {"bold": True, "color": WHITE, "size": 9}, "horizontal_alignment": "center"}
    sheet.get_range("A3:T3").format.row_height = 20

    metrics = projection.executive_metrics
    metric_cols = [("section", "Section"), ("metric", "Metric"), ("value", "Value"), ("currency", "Currency"), ("notes", "Notes")]
    matrix = [[h for _, h in metric_cols]] + [[_normalize(r[k]) for k, _ in metric_cols] for r in metrics]
    metric_end = 4 + len(metrics)
    sheet.get_range(f"A4:E{metric_end}").values = matrix
    sheet.get_range("A4:E4").format = {"fill": ESPRESSO, "font": {"bold": True, "color": WHITE, "size": 9}, "wrap_text": True}
    sheet.get_range("A4:E4").format.row_height = 30
    widths = [16, 30, 34, 10, 45]
    for i, w in enumerate(widths, 1):
        sheet.get_range(f"{_col_letter(i)}4:{_col_letter(i)}{metric_end}").format.column_width = w
    sheet.get_range(f"E5:E{metric_end}").format.wrap_text = True
    sheet.get_range(f"A5:E{metric_end}").format.row_height = 23
    for i, row in enumerate(metrics, start=5):
        value = row.get("value")
        if row.get("currency") not in (wc.DISPLAY_NA, "MIXED") and isinstance(value, (int, float)):
            sheet.get_range(f"C{i}").format.number_format = MONEY_FMT
        elif isinstance(value, int):
            sheet.get_range(f"C{i}").format.number_format = INT_FMT
    if metrics:
        table = sheet.tables.add(f"A4:E{metric_end}", True, "ExecutiveMetrics")
        try: table.style = "TableStyleMedium2"
        except Exception: pass

    opp_cols = [
        ("warehouse_name", "Warehouse"), ("owner", "Owner"), ("currency", "Currency"),
        ("forward_total_cost_365d", "Forward baseline"), ("recommended_impact_type", "Impact type"),
        ("recommended_annual_impact", "Recommended annual impact"), ("scenario_low_impact", "Scenario low"),
        ("scenario_high_impact", "Scenario high"), ("impact_pct", "Impact %"),
        ("recommended_scenario_name", "Recommended plan"), ("scenario_confidence_level", "Confidence"),
        ("next_action", "Next action"), ("savings_amount", "Savings"), ("cost_increase_amount", "Cost increase"),
    ]
    opp_rows = projection.executive_opportunities
    opp_end = 4 + len(opp_rows)
    sheet.get_range(f"G4:T{opp_end}").values = [[h for _, h in opp_cols]] + [[_normalize(r[k]) for k, _ in opp_cols] for r in opp_rows]
    sheet.get_range("G4:T4").format = {"fill": ESPRESSO, "font": {"bold": True, "color": WHITE, "size": 9}, "wrap_text": True}
    sheet.get_range("G4:T4").format.row_height = 30
    widths = [22, 32, 9, 16, 15, 18, 15, 15, 11, 34, 12, 30, 14, 14]
    for j, width in enumerate(widths, start=7):
        sheet.get_range(f"{_col_letter(j)}4:{_col_letter(j)}{opp_end}").format.column_width = width
    for c in ("J", "L", "M", "N", "S", "T"):
        sheet.get_range(f"{c}5:{c}{opp_end}").format.number_format = MONEY_FMT
    sheet.get_range(f"O5:O{opp_end}").format.number_format = PCT_FMT
    sheet.get_range(f"G5:T{opp_end}").format.row_height = 30
    for c in ("G", "P", "R"):
        sheet.get_range(f"{c}5:{c}{opp_end}").format.wrap_text = True
    sheet.get_range(f"H5:H{opp_end}").format.wrap_text = False
    if opp_rows:
        table = sheet.tables.add(f"G4:T{opp_end}", True, "MaterialImpacts")
        try: table.style = "TableStyleMedium4"
        except Exception: pass
        for col, word, fill in (("K", "SAVINGS", GREEN), ("K", "COST_INCREASE", RED), ("K", "NO_CHANGE", GRAY), ("Q", "HIGH", GREEN), ("Q", "MEDIUM", AMBER), ("Q", "LOW", RED)):
            sheet.get_range(f"{col}5:{col}{opp_end}").conditional_formats.add_custom(f'={col}5="{word}"', {"fill": fill})
        chart = sheet.charts.add("bar", {"title": "Recommended annual economic impact", "has_legend": True})
        s1 = chart.series.add("Savings"); s1.category_formula = f"'{wc.SHEET_EXECUTIVE}'!$G$5:$G${opp_end}"; s1.formula = f"'{wc.SHEET_EXECUTIVE}'!$S$5:$S${opp_end}"; s1.values_format_code = "#,##0"
        s2 = chart.series.add("Cost increase"); s2.category_formula = f"'{wc.SHEET_EXECUTIVE}'!$G$5:$G${opp_end}"; s2.formula = f"'{wc.SHEET_EXECUTIVE}'!$T$5:$T${opp_end}"; s2.values_format_code = "#,##0"
        chart.set_position("G22", "T40")
    sheet.freeze_panes.freeze_rows(4)


def _build_portfolio(wb: Any, projection: wc.WorkbookProjection) -> None:
    sheet = wb.worksheets.add(wc.SHEET_PORTFOLIO)
    _sheet_title(sheet, "Warehouse Portfolio", "One row per recommendation package. All agreed contract columns are retained; verbose audit fields are placed at the far right.", len(wc.PORTFOLIO_COLUMNS))
    _write_contract_table(
        sheet, columns=wc.PORTFOLIO_COLUMNS, groups=wc.PORTFOLIO_GROUPS, rows=projection.warehouse_portfolio,
        table_name="WarehousePortfolio", freeze_columns=4, row_height=24,
        wrap_keys=("warehouse_name", "recommended_scenario_name", "next_action"),
        json_keys=("query_source_profile_json", "scenario_ids_json", "candidate_ids_json", "package_file", "evidence_summary_json"),
    )


def _build_scenarios(wb: Any, projection: wc.WorkbookProjection) -> None:
    sheet = wb.worksheets.add(wc.SHEET_SCENARIOS)
    _sheet_title(sheet, "Scenario Plans", "One row per scenario. Economics are numeric and sortable; signed annual impact is positive for savings and negative for cost increase.", len(wc.SCENARIO_COLUMNS))
    _write_contract_table(
        sheet, columns=wc.SCENARIO_COLUMNS, groups=wc.SCENARIO_GROUPS, rows=projection.scenario_plans,
        table_name="ScenarioPlans", freeze_columns=5, row_height=24,
        wrap_keys=("warehouse_name", "scenario_name"),
        json_keys=("compatibility_checks_json", "compatibility_blockers_json", "execution_plan_json", "candidate_ids_json", "dependencies_json", "invalidations_json"),
    )


def _build_registry(wb: Any, projection: wc.WorkbookProjection) -> None:
    sheet = wb.worksheets.add(wc.SHEET_REGISTRY)
    _sheet_title(sheet, "Recommendation Registry", "One row per candidate. Decision, economics, realization and audit evidence align to Workbook Contract v1.1.0.", len(wc.REGISTRY_COLUMNS))
    _write_contract_table(
        sheet, columns=wc.REGISTRY_COLUMNS, groups=wc.REGISTRY_GROUPS, rows=projection.recommendation_registry,
        table_name="RecommendationRegistry", freeze_columns=6, row_height=32,
        wrap_keys=("warehouse_name", "objectives", "current_value", "recommended_value", "rationale", "decision_rule"),
        json_keys=("confidence_components_json", "evidence_refs_json", "evidence_json", "thresholds_json", "dependencies_json", "invalidates_json", "blockers_json", "scenario_membership_json", "realization_records_json"),
    )


def write_workbook(path: str | Path, packages: Sequence[rc.RecommendationPackage]) -> Path:
    """Generate the locked four-sheet workbook projection and save it to *path*."""
    for package in packages:
        package.validate()
    projection = wc.project_workbook(packages)
    wb = Workbook.create()
    _build_executive(wb, projection)
    _build_portfolio(wb, projection)
    _build_scenarios(wb, projection)
    _build_registry(wb, projection)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    SpreadsheetFile.export_xlsx(wb).save(str(target))
    return target
