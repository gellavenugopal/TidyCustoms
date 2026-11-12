"""Write one or more Reports to an .xlsx workbook (bytes)."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .services.reports import Report

FORMATS = {"money": "#,##0.00", "qty": "#,##0.000", "rate": "0.00", "date": "DD-MM-YYYY"}
HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")


def _cell_value(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _write_sheet(ws, report: Report):
    ws.title = (report.sheet or report.title)[:31].replace("/", "-")
    ws.append([report.title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([report.subtitle] if report.subtitle else [])
    ws.append([])
    ws.append([c.label for c in report.columns])
    head_row = ws.max_row
    for i in range(1, len(report.columns) + 1):
        cell = ws.cell(row=head_row, column=i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r in report.rows:
        ws.append([_cell_value(r.get(c.key)) for c in report.columns])
    if report.totals:
        ws.append([("Total" if i == 0 else _cell_value(report.totals.get(c.key)))
                   for i, c in enumerate(report.columns)])
        for i in range(1, len(report.columns) + 1):
            ws.cell(row=ws.max_row, column=i).font = Font(bold=True)
    for i, col in enumerate(report.columns, start=1):
        fmt = FORMATS.get(col.kind)
        width = max([len(col.label)] + [len(str(r.get(col.key) or "")) for r in report.rows[:500]]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(max(width, 10), 60)
        if fmt:
            for row in range(head_row + 1, ws.max_row + 1):
                ws.cell(row=row, column=i).number_format = fmt
    ws.freeze_panes = ws.cell(row=head_row + 1, column=1)


def reports_to_xlsx(*reports: Report) -> bytes:
    wb = Workbook()
    first = True
    used = set()
    for rep in reports:
        ws = wb.active if first else wb.create_sheet()
        first = False
        _write_sheet(ws, rep)
        base, n = ws.title, 2
        while ws.title in used:  # unique sheet names
            ws.title = f"{base[:28]}-{n}"
            n += 1
        used.add(ws.title)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
