"""Artifact-tool renderer for Realizer Workbook Extension v1.0.0.

The output is a *new extension workbook*. The locked Workbook v1.1.0 renderer
remains unchanged and can still be used independently.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from artifact_tool import Blob, SpreadsheetFile

from contracts import realizer_contract as rrc
from contracts import realizer_workbook_contract as rwc
from reporters import workbook_reporter as legacy

ESPRESSO = "#3B2F2F"
CREAM = "#F7F1E8"
WHITE = "#FFFFFF"
TEXT = "#2B2522"
MONEY_FMT = '$#,##0.00;[Red]-$#,##0.00'
PCT_FMT = '0.0%'


def _write_rows(sheet: Any, rows: Sequence[Mapping[str, Any]], *, title: str, subtitle: str) -> None:
    if not rows:
        sheet.get_range("A1").values = [[title]]
        sheet.get_range("A2").values = [["No rows"]]
        return
    cols = list(rows[0].keys())
    end_col = legacy._col_letter(len(cols))
    sheet.merge_cells(f"A1:{end_col}1")
    sheet.get_range("A1").values = [[title]]
    sheet.get_range(f"A1:{end_col}1").format = {"fill": ESPRESSO, "font": {"bold": True, "color": WHITE, "size": 17}}
    sheet.merge_cells(f"A2:{end_col}2")
    sheet.get_range("A2").values = [[subtitle]]
    sheet.get_range(f"A2:{end_col}2").format = {"fill": CREAM, "font": {"italic": True, "color": TEXT, "size": 10}, "wrap_text": True}
    matrix = [cols] + [[row.get(c) for c in cols] for row in rows]
    sheet.get_range(f"A4:{end_col}{3+len(matrix)}").values = matrix
    sheet.get_range(f"A4:{end_col}4").format = {"fill": ESPRESSO, "font": {"bold": True, "color": WHITE}, "wrap_text": True}
    sheet.freeze_panes.freeze_rows(4)
    for idx, key in enumerate(cols, start=1):
        letter = legacy._col_letter(idx)
        rng = sheet.get_range(f"{letter}4:{letter}{3+len(matrix)}")
        width = 18
        if key.endswith("_json") or key in {"payload_json", "evidence_refs"}:
            width = 38; rng.format.wrap_text = True
        elif key in {"warehouse_name", "event_type", "realization_package_id", "recommendation_package_id"}:
            width = 24
        rng.format.column_width = width
        if "savings" in key:
            rng.format.number_format = MONEY_FMT
        if key == "realization_ratio":
            rng.format.number_format = PCT_FMT
    sheet.tables.add(f"A4:{end_col}{3+len(matrix)}", True, title.replace(' ', '') + "Table")


def write_realizer_extension_workbook(
    path: str | Path,
    packages: Sequence[Any],
    realizations: Sequence[rrc.RealizationPackage],
    ledgers: Mapping[str, Sequence[rrc.RealizationLedgerEntry]],
) -> Path:
    """Render locked Workbook v1.1.0 views plus separately versioned Realizer sheets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".legacy-base.xlsx")
    legacy.write_workbook(tmp, packages)
    wb = SpreadsheetFile.import_xlsx(Blob.load(str(tmp)))
    projection = rwc.project_realizer_workbook(realizations, ledgers)
    summary = wb.worksheets.add(rwc.SHEET_REALIZATION_SUMMARY)
    timeline = wb.worksheets.add(rwc.SHEET_REALIZATION_TIMELINE)
    _write_rows(summary, projection.realization_summary, title=rwc.SHEET_REALIZATION_SUMMARY,
                subtitle="Canonical PKG + RPKG projection. Values are presentation-only; no economics are recomputed here.")
    _write_rows(timeline, projection.realization_timeline, title=rwc.SHEET_REALIZATION_TIMELINE,
                subtitle="Append-only Realization Ledger timeline ordered by recorded time and deterministic RLE identity.")
    SpreadsheetFile.export_xlsx(wb).save(str(path))
    try:
        tmp.unlink()
    except OSError:
        pass
    return path
