"""Excel templates for Bills of Entry (imports), Shipping Bills (exports) and
Bills of Material (BOMs per finished good).

Workflow
  1. Upload BoE / SB PDFs  -> the app extracts every item and returns a
     pre-filled template (.xlsx). Cells still needed are highlighted yellow.
  2. Fill the gaps and tag each line with its unique Part No (imports) or
     Model / Part No (exports) right in the sheet.
  3. Upload the template -> preview of what will be created -> confirm.
     Re-uploading is safe: invoices / shipments already in the tracker are skipped.

Unknown Part Nos on the import template are created as new items. Tagging a
description saves a mapping, so the same description is tagged automatically
in every later template.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_rules
from .extract.boe_parser import BoeResult, parse_date
from .models import BOM, Bond, ExportMapping, ExportShipment, FGModel, ImportInvoice, Item, Supplier
from .numbers import D
from .services import exports as export_svc
from .services import imports as import_svc
from .services import masters
from .services.errors import BusinessError, ValidationError

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
NEED_FILL = PatternFill("solid", fgColor="FFF2CC")
CALC_FILL = PatternFill("solid", fgColor="E2EFDA")
FORMULA_FONT = Font(italic=True, color="375623")
TEMPLATE_ROWS = 300  # formulas / highlighting prepared for this many rows


@dataclass
class Col:
    key: str
    header: str
    width: int = 14
    required: bool = False
    kind: str = "text"  # text | int | num | date | formula
    note: str = ""


# ------------------------------------------------------------------ common writer
def _sheet_from_cols(ws, cols: list[Col], rows: list[dict], formulas: dict[str, str]):
    for c, col in enumerate(cols, start=1):
        cell = ws.cell(row=1, column=c, value=col.header + (" *" if col.required else ""))
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL if col.kind != "formula" else PatternFill("solid", fgColor="375623")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        if col.note:
            cell.comment = Comment(col.note, "EOU Bond Tracker", width=300, height=120)
        ws.column_dimensions[get_column_letter(c)].width = col.width
    ws.row_dimensions[1].height = 45
    ws.freeze_panes = "A2"
    letter = {col.key: get_column_letter(i) for i, col in enumerate(cols, start=1)}
    last = max(len(rows) + 1, TEMPLATE_ROWS)
    first_key, desc_key = cols[0].key, _desc_key(cols)
    for r in range(2, last + 1):
        data = rows[r - 2] if r - 2 < len(rows) else {}
        # formulas stay blank on rows not yet in use (no first column and no description)
        blank_if_unused = f'IF(LEN(${letter[first_key]}{r}&${letter[desc_key]}{r})=0,"",{{}})'
        for c, col in enumerate(cols, start=1):
            cell = ws.cell(row=r, column=c)
            if col.kind == "formula":
                cell.value = "=" + blank_if_unused.format(formulas[col.key].format(r=r, **letter))
                cell.font = FORMULA_FONT
                cell.fill = CALC_FILL
                continue
            v = data.get(col.key)
            if v is None or v == "":
                continue
            cell.value = v
            if col.kind == "date":
                cell.number_format = "DD-MM-YYYY"
            elif col.kind == "num":
                cell.number_format = "#,##0.00"
    # highlight still-needed cells (yellow) on rows that are in use
    in_use = f'LEN(${letter[cols[0].key]}2&${letter[_desc_key(cols)]}2)>0'
    for col in cols:
        if col.required and col.kind != "formula":
            L = letter[col.key]
            ws.conditional_formatting.add(f"{L}2:{L}{last}",
                                          FormulaRule(formula=[f"AND({in_use},LEN({L}2)=0)"], fill=NEED_FILL))
    return letter, last


def _desc_key(cols):
    return next(c.key for c in cols if c.key == "description")


def _list_sheet(wb, title, headers, rows, widths):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        ws.cell(row=1, column=c).font = Font(bold=True, color="FFFFFF")
        ws.cell(row=1, column=c).fill = HEAD_FILL
        ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "A2"
    return ws


def _readme(wb, title, lines):
    ws = wb.active
    ws.title = "Read me"
    ws.column_dimensions["A"].width = 120
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    for i, (text, style) in enumerate(lines, start=3):
        cell = ws.cell(row=i, column=1, value=text)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        if style == "h":
            cell.font = Font(bold=True, size=12)
        elif style == "need":
            cell.fill = NEED_FILL
        elif style == "calc":
            cell.fill = CALC_FILL
            cell.font = FORMULA_FONT
    return ws


def _link(cell, sheet, row, text=None):
    cell.hyperlink = f"#'{sheet}'!A{row}"
    if text is not None:
        cell.value = text
    cell.style = "Hyperlink"


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ suggestions
_PART_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bP/?N\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})",
    r"\(([0-9]{5,7}(?:-[0-9]{2})?)\)",
    r"\b([0-9]{6}-[0-9]{2})\b",
    r"\(([A-Z0-9]{2,}[0-9][A-Z0-9\-]{2,})\)",
)]


def part_candidates(description: str) -> list[str]:
    out = []
    for pat in _PART_PATTERNS:
        for m in pat.findall(description or ""):
            m = m.upper().strip("-")
            if m not in out:
                out.append(m)
    return out


def _item_by_code(session: Session, code: str) -> Item | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    item = session.scalar(select(Item).where(func.upper(Item.code) == code))
    if item is None and re.fullmatch(r"\d{5,7}-\d{2}", code):
        item = session.scalar(select(Item).where(func.upper(Item.code) == code.split("-")[0]))
    return item


def suggest_part(session: Session, supplier_id: int | None, description: str) -> tuple[str, str]:
    m = masters.find_mapping(session, supplier_id, description)
    if m:
        return m.item.code, "Part No from saved mapping"
    cands = part_candidates(description)
    for c in cands:
        it = _item_by_code(session, c)
        if it:
            return it.code, "Part No found in the description - confirm"
    if cands:
        return cands[0], "NEW part no taken from the description - confirm"
    return "", ""


def suggest_export_tag(session: Session, description: str) -> tuple[str, str, str]:
    key = masters.normalize_description(description)
    m = session.scalar(select(ExportMapping).where(ExportMapping.normalized_description == key))
    if m:
        return ("FG", m.model.code, "Tag from saved mapping") if m.model_id else ("ITEM", m.item.code, "Tag from saved mapping")
    for model in session.scalars(select(FGModel).where(FGModel.active.is_(True))):
        for probe in (masters.normalize_description(model.code), masters.normalize_description(model.name)):
            if probe and len(probe) >= 4 and f" {probe} " in f" {key} ":
                return "FG", model.code, "Model found in the description - confirm"
    for c in part_candidates(description):
        it = _item_by_code(session, c)
        if it:
            return "ITEM", it.code, "Part No found in the description - confirm"
    return "", "", ""


# ------------------------------------------------------------------ value parsing
def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _n(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError as exc:
        raise ValueError(f"'{v}' is not a number") from exc


def _d(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    d = parse_date(str(v).strip().upper()) or parse_date(str(v).strip())
    if d is None:
        raise ValueError(f"'{v}' is not a date (use DD-MM-YYYY)")
    return d


def _read_rows(blob: bytes, sheet: str, cols: list[Col]) -> list[dict]:
    try:
        wb = load_workbook(io.BytesIO(blob), data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("This is not an Excel (.xlsx) file") from exc
    if sheet not in wb.sheetnames:
        raise ValidationError(f"Sheet '{sheet}' not found - use the template downloaded from the app")
    ws = wb[sheet]
    by_header = {c.header.lower(): c for c in cols}
    keys = []
    for cell in ws[1]:
        h = _s(cell.value).rstrip(" *").lower()
        keys.append(by_header[h].key if h in by_header else None)
    rows = []
    for r_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        rec = {k: v for k, v in zip(keys, row) if k}
        data_cols = [c.key for c in cols if c.kind != "formula"]
        if all(rec.get(k) in (None, "") for k in data_cols):
            continue
        rec["_row"] = r_idx
        rows.append(rec)
    return rows


@dataclass
class GroupOutcome:
    key: str
    rows: list[int]
    status: str = "pending"  # created | posted | skipped | error
    message: str = ""
    doc_id: int | None = None
    new_parts: list[str] = field(default_factory=list)
    new_suppliers: list[str] = field(default_factory=list)
    lines: int = 0
    value: float = 0.0


def _rows_label(rows):
    return ", ".join(str(r) for r in rows[:8]) + ("..." if len(rows) > 8 else "")


# =================================================================== IMPORTS
IMPORT_SHEET = "BoE Items"
IMPORT_COLS = [
    Col("be_no", "BE No", 12, True, "text", "Bill of Entry number"),
    Col("be_date", "BE Date", 12, True, "date"),
    Col("port", "Port of Import", 10, True, "text", "ICEGATE port code, e.g. INHYD4, INBOM4"),
    Col("import_type", "Import type (BOND / DUTY PAID)", 12, True, "text",
        "BOND = duty-saved, debited to the bond. DUTY PAID = cleared on payment of duty: no bond, never "
        "debited, and nothing is recredited when these goods are exported. Pre-filled from the BoE "
        "(BOND when the BoE carries a bond)."),
    Col("bond_no", "Bond No (tracker)", 18, False, "text",
        "BOND imports only: bond to debit - a bond number in the tracker (see sheet 'Bonds'). "
        "Pre-filled from the BoE's bond if it matches, else the active bond valid on the BE date."),
    Col("supplier", "Supplier", 24, True, "text", "Created in the tracker if it doesn't exist yet"),
    Col("invoice_no", "Invoice No", 14, True),
    Col("invoice_date", "Invoice Date", 12, True, "date"),
    Col("inv_sno", "Invoice S.No (on BE)", 9, True, "int", "INVSNO on the BE"),
    Col("item_sno", "Item S.No (on BE)", 9, True, "int", "ITEMSN on the BE"),
    Col("cth", "CTH", 11, False),
    Col("description", "Description (as on BE)", 50, True),
    Col("part_no", "Part No (internal code)", 16, True, "text",
        "TAG THE UNIQUE PART NO HERE. Existing codes are on sheet 'Parts'. A code that is not there "
        "yet is created as a new part when you upload. The tag is remembered for this description."),
    Col("part_status", "Part No status", 22, kind="formula"),
    Col("qty", "Qty", 10, True, "num"),
    Col("uqc", "UQC", 7, True),
    Col("qty_received", "Qty received (blank = all)", 11, False, "num"),
    Col("av", "Assessable value INR", 14, True, "num"),
    Col("bcd", "BCD", 12, False, "num", "Basic Customs Duty - forgone (BOND) or paid (DUTY PAID)"),
    Col("sws", "SWS", 11, False, "num", "Social Welfare Surcharge - forgone (BOND) or paid (DUTY PAID)"),
    Col("igst", "IGST", 12, False, "num"),
    Col("cess", "Cess", 10, False, "num", "Compensation cess, Health cess, AIDC ..."),
    Col("other", "Other duty", 10, False, "num"),
    Col("total", "Total duty", 13, kind="formula"),
    Col("ooc_date", "OOC Date", 12, False, "date", "Out-of-charge date (needed for Form III)"),
    Col("receipt_date", "Receipt Date", 12, False, "date", "Date goods reached the factory (needed for Form III)"),
    Col("notes", "Notes from extraction", 40, False),
]
IMPORT_FORMULAS = {
    "part_status": 'IF({part_no}{r}="","⚠ enter Part No",IF(COUNTIF(Parts!$A:$A,{part_no}{r})>0,'
                   '"✓ existing part","➕ new part - created on upload"))',
    "total": "SUM({bcd}{r}:{other}{r})",
}


def _default_bond(session: Session, parsed_bond: str | None, on: date | None) -> str:
    if parsed_bond:
        b = session.scalar(select(Bond).where(Bond.bond_no == parsed_bond))
        if b:
            return b.bond_no
    if on:
        valid = [b for b in session.scalars(select(Bond).where(Bond.status == "ACTIVE"))
                 if b.valid_from <= on <= b.valid_to]
        if len(valid) == 1:
            return valid[0].bond_no
    return parsed_bond or ""


def import_rows_from_boe(session: Session, results: list[BoeResult]) -> list[dict]:
    rows = []
    for r in results:
        if not r.success:
            continue
        import_type = "BOND" if r.bonds else "DUTY PAID"
        bond_no = _default_bond(session, r.bonds[0]["bond_no"] if r.bonds else None, r.be_date) if r.bonds else None
        for it in r.items:
            inv = r.invoice(it.invsno)
            sup_name = inv.supplier if inv else ""
            sup = session.scalar(select(Supplier).where(func.upper(Supplier.name) == (sup_name or "").upper()))
            code, why = suggest_part(session, sup.id if sup else None, it.description)
            notes = [n for n in (why, f"duty from {it.duty_source}" if it.duty_source else "duty not found",
                                 f"from {r.source_filename}") if n]
            rows.append(dict(be_no=r.be_no, be_date=r.be_date, port=r.port_code, import_type=import_type,
                             bond_no=bond_no,
                             supplier=sup.name if sup else sup_name, invoice_no=inv.invoice_no if inv else None,
                             invoice_date=inv.invoice_date if inv else None, inv_sno=it.invsno, item_sno=it.itemsn,
                             cth=it.cth, description=it.description, part_no=code, qty=it.qty, uqc=it.uqc,
                             av=it.assessable_value, bcd=it.bcd, sws=it.sws, igst=it.igst,
                             cess=it.cess, other=it.other, ooc_date=r.ooc_date, notes="; ".join(notes)))
    return rows


def build_import_template(session: Session, results: list[BoeResult] | None = None) -> bytes:
    results = results or []
    rows = import_rows_from_boe(session, results)
    wb = Workbook()
    _readme(wb, "Bill of Entry upload template - EOU Bond Tracker", [
        ("How to use", "h"),
        ("1. One row per Bill of Entry item. Rows from uploaded BoE PDFs are already filled in.", ""),
        ("2. Yellow cells are still needed - fill them in. Columns marked * are required.", "need"),
        ("3. Tag every row with its unique internal Part No in 'Part No (internal code)'. Existing codes are on the "
         "'Parts' sheet; a new code is created as a new part when you upload. The tag is remembered, so the same "
         "description is tagged automatically next time.", ""),
        ("4. Check the 'Check' sheet: the duty in this sheet must agree with each BoE's duty, which has been reconciled "
         "to the bond debit printed on the BoE (EOU bond = duty forgone; warehouse WH bond = 3 x duty; end-use RE "
         "bond = BCD + SWS forgone).", ""),
        ("5. Save, and upload on Imports -> Upload BoE / template. You'll see a preview before anything is created. "
         "Uploading again is safe: invoices already in the tracker are skipped.", ""),
        ("Green columns are formulas - don't type in them", "h"),
        ("Part No status = IF(Part No blank, '⚠ enter Part No', IF(COUNTIF(Parts!A:A, Part No) > 0, "
         "'✓ existing part', '➕ new part')).", "calc"),
        ("Total duty forgone = SUM(BCD : Other) on the same row. This total is debited to the bond.", "calc"),
        ("Where the extracted numbers come from (ICEGATE BE print)", "h"),
        ("BE No / Date / Port: page header. Bond: Part I 'F. PAYMENT - 1.BOND NO.'. Invoice No / date / supplier: Part II. "
         "CTH, description, Qty (C.QTY), UQC, assessable value: Part III. Duty, per item, from the first of: "
         "(a) Part IV 'G. SCHEME NOTIFICATION AND DUTY FOREGONE' (EOU BEs) - '8. BCD AMT FG' is BCD + SWS, '9. OTH AMT FG' "
         "is IGST; (b) Part III DUTY 'Amount' rows (warehousing / MOOWR BEs: the assessed duty kept under bond), incl. "
         "Health cess / AIDC as Cess; (c) Part III 'Duty Fg' rows (items exempted by notification). The 'Notes' column "
         "says which one was used.", ""),
        ("Qty received: leave blank when everything arrived. OOC Date and Receipt Date are used in the Form III return.", ""),
    ])
    ws = wb.create_sheet(IMPORT_SHEET)
    letter, last = _sheet_from_cols(ws, IMPORT_COLS, rows, IMPORT_FORMULAS)
    _list_sheet(wb, "Parts", ["Code", "Description", "UOM", "Type", "HS code"],
                [[i.code, i.description, i.uom, i.item_type, i.hs_code] for i in
                 session.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.code))],
                [18, 60, 8, 12, 12])
    _list_sheet(wb, "Bonds", ["Bond No", "Valid from", "Valid to", "Balance", "Status"],
                [[b.bond_no, b.valid_from, b.valid_to, float(b.balance), b.status] for b in
                 session.scalars(select(Bond).order_by(Bond.bond_no))], [22, 12, 12, 16, 10])
    dv = DataValidation(type="list", formula1="=Bonds!$A$2:$A$200", allow_blank=True, showErrorMessage=True,
                        errorTitle="Bond", error="Pick a bond that exists in the tracker")
    ws.add_data_validation(dv)
    dv.add(f"{letter['bond_no']}2:{letter['bond_no']}{last}")
    dvt = DataValidation(type="list", formula1='"BOND,DUTY PAID"', allow_blank=True)
    ws.add_data_validation(dvt)
    dvt.add(f"{letter['import_type']}2:{letter['import_type']}{last}")
    # Bond No is needed only on BOND rows: highlight it yellow there when blank
    B, T = letter["bond_no"], letter["import_type"]
    ws.conditional_formatting.add(f"{B}2:{B}{last}", FormulaRule(
        formula=[f'AND(LEN($A2&${letter["description"]}2)>0,UPPER(${T}2)<>"DUTY PAID",LEN({B}2)=0)'],
        fill=NEED_FILL))
    dv2 = DataValidation(type="list", formula1="=Parts!$A$2:$A$5000", allow_blank=True, showErrorMessage=True,
                         errorStyle="warning", errorTitle="New part",
                         error="This Part No is not in the item master yet - it will be created as a new part.")
    ws.add_data_validation(dv2)
    dv2.add(f"{letter['part_no']}2:{letter['part_no']}{last}")

    # Check sheet: duty per BE vs the bond debit printed on the BoE
    ck = _list_sheet(wb, "Check", ["BE No", "First row", "Duty per BoE (agrees with bond debit)",
                                   "Duty forgone in template", "Difference", "Status", "AV per BoE (18.TOT.ASS VAL)",
                                   "AV in template", "Items in template", "How the BoE's bond debit is made up"],
                     [], [12, 10, 20, 18, 12, 12, 18, 16, 10, 80])
    seen = []
    for i, row in enumerate(rows, start=2):
        if row["be_no"] not in [s[0] for s in seen]:
            seen.append((row["be_no"], i))
    by_be = {r.be_no: r for r in results if r.success}
    A, W, Q = letter["be_no"], letter["total"], letter["av"]
    for n, (be, first) in enumerate(seen, start=2):
        res = by_be.get(be)
        ck.cell(row=n, column=1, value=be)
        _link(ck.cell(row=n, column=2), IMPORT_SHEET, first, f"row {first} →")
        bc = (res.bond_check or {}) if res else {}
        ck.cell(row=n, column=3, value=bc.get("duty") if bc.get("ok") else None).number_format = "#,##0.00"
        ck.cell(row=n, column=10, value=(bc.get("text") or "no bond debit on the BoE")
                + ("" if bc.get("ok", True) else " - the BoE's items don't add up to its bond debit: check them"))
        ck.cell(row=n, column=4, value=f"=SUMIF('{IMPORT_SHEET}'!${A}:${A},A{n},'{IMPORT_SHEET}'!${W}:${W})").number_format = "#,##0.00"
        ck.cell(row=n, column=5, value=f'=IF(C{n}="","",D{n}-C{n})').number_format = "#,##0.00"
        ck.cell(row=n, column=6, value=f'=IF(C{n}="","no BoE total",IF(ABS(E{n})<=1+0.5*I{n},"✓ agrees","✗ check"))')
        ck.cell(row=n, column=7, value=res.total_assessable_value if res else None).number_format = "#,##0.00"
        ck.cell(row=n, column=8, value=f"=SUMIF('{IMPORT_SHEET}'!${A}:${A},A{n},'{IMPORT_SHEET}'!${Q}:${Q})").number_format = "#,##0.00"
        ck.cell(row=n, column=9, value=f"=COUNTIF('{IMPORT_SHEET}'!${A}:${A},A{n})")
    _list_sheet(wb, "Extraction log", ["File", "Result", "BE No", "Items", "Messages"],
                [[r.source_filename, "OK" if r.success else "FAILED", r.be_no, len(r.items),
                  "; ".join(r.errors + r.warnings)] for r in results], [36, 10, 12, 8, 100])
    wb.active = 1
    return _save(wb)


def read_import_template(blob: bytes) -> list[dict]:
    return _read_rows(blob, IMPORT_SHEET, IMPORT_COLS)


def apply_import_rows(session: Session, rows: list[dict], *, post: bool = False) -> list[GroupOutcome]:
    """Create one draft import per (BE No, invoice S.No) - each in its own savepoint,
    so one bad invoice doesn't stop the others. The caller commits or rolls back."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((_s(r.get("be_no")) or f"(no BE) {_s(r.get('invoice_no'))}", _s(r.get("inv_sno")) or "1"), []).append(r)
    out = []
    for (be, inv_sno), grp in groups.items():
        g = GroupOutcome(key=f"BE {be} / invoice {inv_sno}", rows=[r["_row"] for r in grp])
        out.append(g)
        try:
            with session.begin_nested():
                _apply_import_group(session, grp, g, post)
        except (BusinessError, ValueError) as exc:
            g.status, g.message = "error", str(exc)
            g.new_parts, g.new_suppliers = [], []
    return out


def _need(r: dict, key: str, label: str):
    v = r.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValidationError(f"row {r['_row']}: {label} is missing")
    return v


def _apply_import_group(session: Session, grp: list[dict], g: GroupOutcome, post: bool):
    h = grp[0]
    import_type = (_s(h.get("import_type")) or "BOND").upper().replace("_", " ")
    if import_type not in ("BOND", "DUTY PAID"):
        raise ValidationError(f"row {h['_row']}: Import type must be BOND or DUTY PAID")
    paid = import_type == "DUTY PAID"
    keys = [("be_no", "BE No"), ("be_date", "BE Date"), ("port", "Port of Import"), ("supplier", "Supplier"),
            ("invoice_no", "Invoice No"), ("invoice_date", "Invoice Date"), ("import_type", "Import type")]
    if not paid:
        keys.append(("bond_no", "Bond No"))
    for key, label in keys:
        if key != "import_type":
            _need(h, key, label)
        for r in grp[1:]:
            if _s(r.get(key)).upper() != _s(h.get(key)).upper() and key != "supplier":
                raise ValidationError(f"row {r['_row']}: {label} differs from row {h['_row']} of the same invoice")
    bond = None
    if not paid:
        bond = session.scalar(select(Bond).where(Bond.bond_no == _s(h["bond_no"])))
        if not bond:
            raise ValidationError(f"row {h['_row']}: bond '{_s(h['bond_no'])}' is not in the tracker (see sheet Bonds)")
    sup_name = _s(h["supplier"])
    sup = session.scalar(select(Supplier).where(func.upper(Supplier.name) == sup_name.upper()))
    if not sup:
        sup = masters.create_supplier(session, name=sup_name)
        g.new_suppliers.append(sup_name)
    inv_no = _s(h["invoice_no"])
    existing = session.scalar(select(ImportInvoice).where(ImportInvoice.supplier_id == sup.id,
                                                          ImportInvoice.invoice_no == inv_no))
    if existing:
        g.status, g.doc_id = "skipped", existing.id
        g.message = f"invoice {inv_no} from {sup.name} is already in the tracker ({existing.status})"
        return
    lines = []
    for r in grp:
        desc = _s(_need(r, "description", "Description"))
        code = _s(_need(r, "part_no", "Part No")).upper()
        item = _item_by_code(session, code)
        if item is None:
            item = masters.create_item(session, code=code, description=desc[:255], uom=_s(r.get("uqc")) or "NOS",
                                       hs_code=_s(r.get("cth")) or None)
            g.new_parts.append(code)
        duty = {k: _n(r.get(src)) for k, src in (("bcd_forgone", "bcd"), ("sws_forgone", "sws"),
                                                  ("igst_forgone", "igst"), ("cess_forgone", "cess"),
                                                  ("other_forgone", "other"))}
        if all(v is None for v in duty.values()):
            raise ValidationError(f"row {r['_row']}: enter the duty {'paid' if paid else 'forgone'} "
                                  "(BCD / SWS / IGST ...; 0 if none)")
        lines.append(dict(description=desc, item_id=item.id, qty=_n(_need(r, "qty", "Qty")),
                          qty_received=_n(r.get("qty_received")), assessable_value=_n(_need(r, "av", "Assessable value")),
                          be_item_serial=int(_n(_need(r, "item_sno", "Item S.No"))), hs_code=_s(r.get("cth")) or None,
                          **{k: (v or 0) for k, v in duty.items()}))
    inv = import_svc.create_import(
        session, invoice_no=inv_no, invoice_date=_d(h["invoice_date"]), supplier_id=sup.id,
        bond_id=bond.id if bond else None, import_type="DUTY_PAID" if paid else "BOND",
        lines=lines, be_no=_s(h["be_no"]), be_date=_d(h["be_date"]), port_of_import=_s(h["port"]),
        be_invoice_serial=int(_n(h.get("inv_sno")) or 1), clearance_date=_d(h.get("ooc_date")),
        receipt_date=_d(h.get("receipt_date")))
    g.doc_id, g.lines = inv.id, len(inv.lines)
    g.status = "created"
    if paid:
        g.value = float(inv.total_duty)
        g.message = (f"draft DUTY PAID import {inv_no}: {len(inv.lines)} line(s), duty paid {inv.total_duty:,.2f} "
                     "- no bond debit")
    else:
        g.value = float(inv.total_dsv)
        g.message = f"draft import {inv_no}: {len(inv.lines)} line(s), duty forgone {inv.total_dsv:,.2f}"
    if post:
        import_svc.post_import(session, inv.id)
        g.status = "posted"
        g.message += " - posted" if paid else f" - posted, bond {bond.bond_no} debited"


# =================================================================== EXPORTS
EXPORT_SHEET = "SB Items"
EXPORT_COLS = [
    Col("sb_no", "SB No", 12, True),
    Col("sb_date", "SB Date", 12, True, "date"),
    Col("port", "Port of export", 10, True, "text", "ICEGATE port code, e.g. INBOM4"),
    Col("invsn", "Invoice S.No (INVSN)", 9, True, "int"),
    Col("invoice_no", "Invoice No", 18, True, "text", "Export invoice number (unique in the tracker)"),
    Col("invoice_date", "Invoice Date", 12, False, "date"),
    Col("itemsn", "Item S.No (ITEMSN)", 9, True, "int"),
    Col("hs_code", "HS code", 11, False),
    Col("description", "Description (as on SB)", 50, True),
    Col("qty", "Qty", 10, True, "num"),
    Col("uqc", "UQC", 7, False),
    Col("fob", "FOB value INR", 14, False, "num"),
    Col("line_type", "Type (FG / ITEM)", 9, True, "text",
        "FG = finished detector (drawn from finished-goods stock). ITEM = accessory or other duty-saved goods "
        "exported as-is (cables, spare batteries ...), drawn from import stock."),
    Col("code", "Model / Part No", 16, True, "text",
        "TAG HERE: model code for FG lines (sheet 'Models'), part no for ITEM lines (sheet 'Parts'). "
        "The tag is remembered for this description."),
    Col("tag_status", "Tag status", 20, kind="formula"),
    Col("available", "Stock available (on download)", 12, False, "num",
        "Duty-saved / duty-paid stock of this model or part on the day the template was made, for reference"),
    Col("stock_check", "Stock check", 16, kind="formula"),
    Col("export_item_name", "Export item name (optional)", 26, False, "text",
        "Name to show in the tracker; blank = the SB description"),
    Col("consignee", "Consignee", 20, False),
    Col("country", "Country", 12, False),
    Col("notes", "Notes from extraction", 40, False),
]
EXPORT_FORMULAS = {
    "tag_status": 'IF({code}{r}="","⚠ tag model / part",IF({line_type}{r}="FG",'
                  'IF(COUNTIF(Models!$A:$A,{code}{r})>0,"✓ model","✗ unknown model"),'
                  'IF({line_type}{r}="ITEM",IF(COUNTIF(Parts!$A:$A,{code}{r})>0,"✓ part","✗ unknown part"),'
                  '"⚠ Type must be FG or ITEM")))',
    "stock_check": 'IF(OR({qty}{r}="",{available}{r}=""),"",IF({qty}{r}>{available}{r},"✗ more than stock",'
                   '"✓ within stock"))',
}


def _available(session: Session, line_type: str, code: str, on: date | None = None):
    from .services.stock import available_fg_qty, available_import_qty

    on = on or date.today()
    if line_type == "FG":
        m = session.scalar(select(FGModel).where(func.upper(FGModel.code) == (code or "").upper()))
        return float(available_fg_qty(session, m.id, on)) if m else None
    if line_type == "ITEM":
        it = _item_by_code(session, code)
        return float(available_import_qty(session, it.id, on)) if it else None
    return None


def stock_rows(session: Session) -> list[dict]:
    """One row per finished model and per part with stock, for exports entered by hand."""
    from .services.stock import available_fg_qty, available_import_qty

    on, rows = date.today(), []
    for m in session.scalars(select(FGModel).where(FGModel.active.is_(True)).order_by(FGModel.code)):
        q = available_fg_qty(session, m.id, on)
        if q > 0:
            rows.append(dict(line_type="FG", code=m.code, description=m.name, available=float(q), uqc="NOS",
                             notes="stock row - fill in SB details and Qty if exported, else leave Qty blank"))
    for it in session.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.item_type.desc(), Item.code)):
        q = available_import_qty(session, it.id, on)
        if q > 0:
            rows.append(dict(line_type="ITEM", code=it.code, description=it.description, available=float(q),
                             uqc=it.uom, hs_code=it.hs_code,
                             notes="stock row - fill in SB details and Qty if exported, else leave Qty blank"))
    return rows


def export_rows_from_sb(session: Session, results) -> list[dict]:
    rows = []
    for res in results:
        if not res.success:
            continue
        for li in res.line_items:
            t, code, why = suggest_export_tag(session, li.description)
            rows.append(dict(sb_no=li.sb_no, sb_date=parse_date((li.sb_date or "").upper()) or li.sb_date,
                             port=li.port_code, invsn=int(li.invsn) if li.invsn else 1, invoice_no=li.inv_no,
                             invoice_date=parse_date(li.inv_date or "") or li.inv_date, itemsn=int(li.item_serial_no),
                             hs_code=li.hsn_sac, description=li.description, qty=li.quantity, uqc=li.uqc,
                             fob=li.fob_value_inr, line_type=t, code=code,
                             available=_available(session, t, code) if code else None,
                             notes="; ".join(n for n in (why, f"from {res.source_filename}") if n)))
    return rows


def build_export_template(session: Session, results=None, with_stock: bool = False) -> bytes:
    """From SB PDFs (results), or - for exports entered by hand - pre-filled with
    the stock on hand (with_stock): one row per model / part with stock."""
    results = results or []
    rows = export_rows_from_sb(session, results) + (stock_rows(session) if with_stock else [])
    wb = Workbook()
    _readme(wb, "Shipping Bill upload template - EOU Bond Tracker", [
        ("How to use", "h"),
        ("1. One row per shipping bill item. Rows from uploaded SB PDFs are already filled in.", ""),
        ("2. Yellow cells are still needed - fill them in. Columns marked * are required.", "need"),
        ("3. Tag every row: Type FG + model code (finished detectors, sheet 'Models') or Type ITEM + Part No "
         "(accessories and other goods exported as-is, sheet 'Parts'). Tags are remembered for next time.", ""),
        ("Entering exports by hand? The blank template lists every finished model and part in stock, with the "
         "quantity available. Fill in SB No, SB Date, Invoice No, Invoice S.No, Item S.No and Qty on the rows "
         "you exported (copy a row for a second shipment). Rows left without SB No and Qty are ignored.", ""),
        ("4. Save and upload on Exports -> Upload SB / template. The preview shows the bond recredit per shipment "
         "and any stock shortage before anything is posted. Uploading again is safe: invoices already in the tracker "
         "are skipped.", ""),
        ("Green columns are formulas", "h"),
        ("Tag status checks the tag: COUNTIF(Models!A:A, code) for FG lines, COUNTIF(Parts!A:A, code) for ITEM lines.", "calc"),
        ("Stock check = IF(Qty > Stock available, '✗ more than stock', '✓ within stock'). The upload itself checks "
         "the stock again on the SB date.", "calc"),
        ("'Check' sheet: item count and FOB per SB = COUNTIF / SUMIF over this sheet, compared with what was extracted "
         "from the SB PDF.", "calc"),
        ("Where the extracted numbers come from", "h"),
        ("Extracted with the Shipping Bill Extractor's parser: Port / SB No / SB Date from the header; Invoice No & Dt from "
         "Part II 'A.REF'; INVSN, ITEMSN, HS code, description, quantity, UQC and FOB (INR) from Part III 'Item details'.", ""),
    ])
    ws = wb.create_sheet(EXPORT_SHEET)
    letter, last = _sheet_from_cols(ws, EXPORT_COLS, rows, EXPORT_FORMULAS)
    _list_sheet(wb, "Models", ["Model code", "Name"],
                [[m.code, m.name] for m in session.scalars(select(FGModel).where(FGModel.active.is_(True))
                                                           .order_by(FGModel.code))], [18, 50])
    _list_sheet(wb, "Parts", ["Code", "Description", "UOM", "Type"],
                [[i.code, i.description, i.uom, i.item_type] for i in
                 session.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.code))], [18, 60, 8, 12])
    dv = DataValidation(type="list", formula1='"FG,ITEM"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"{letter['line_type']}2:{letter['line_type']}{last}")
    ck = _list_sheet(wb, "Check", ["SB No", "First row", "Items extracted from PDF", "Items in template",
                                   "FOB extracted from PDF", "FOB in template", "Difference", "Untagged rows"],
                     [], [12, 10, 14, 12, 16, 16, 12, 12])
    A, F, N = letter["sb_no"], letter["fob"], letter["code"]
    n = 2
    for res in results:
        if not res.success:
            continue
        sb = res.line_items[0].sb_no
        first = next((i for i, r in enumerate(rows, start=2) if r["sb_no"] == sb), 2)
        ck.cell(row=n, column=1, value=sb)
        _link(ck.cell(row=n, column=2), EXPORT_SHEET, first, f"row {first} →")
        ck.cell(row=n, column=3, value=len(res.line_items))
        ck.cell(row=n, column=4, value=f"=COUNTIF('{EXPORT_SHEET}'!${A}:${A},A{n})")
        ck.cell(row=n, column=5, value=round(sum(li.fob_value_inr or 0 for li in res.line_items), 2)).number_format = "#,##0.00"
        ck.cell(row=n, column=6, value=f"=SUMIF('{EXPORT_SHEET}'!${A}:${A},A{n},'{EXPORT_SHEET}'!${F}:${F})").number_format = "#,##0.00"
        ck.cell(row=n, column=7, value=f"=F{n}-E{n}").number_format = "#,##0.00"
        ck.cell(row=n, column=8, value=f'=COUNTIFS(\'{EXPORT_SHEET}\'!${A}:${A},A{n},\'{EXPORT_SHEET}\'!${N}:${N},"")')
        n += 1
    _list_sheet(wb, "Extraction log", ["File", "Result", "SB No", "Items", "IGST reconciled", "Messages"],
                [[res.source_filename, "OK" if res.success else "FAILED",
                  res.line_items[0].sb_no if res.line_items else None, len(res.line_items),
                  {True: "yes", False: "NO", None: "n/a"}[res.igst_reconciled], "; ".join(res.errors + res.warnings)]
                 for res in results], [36, 10, 12, 8, 12, 100])
    wb.active = 1
    return _save(wb)


def read_export_template(blob: bytes) -> list[dict]:
    return _read_rows(blob, EXPORT_SHEET, EXPORT_COLS)


def apply_export_rows(session: Session, rows: list[dict]) -> list[GroupOutcome]:
    """Post one export shipment per (SB No, INVSN), oldest SB first, each in its
    own savepoint. The caller commits (confirm) or rolls back (preview)."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if not _s(r.get("sb_no")) and r.get("qty") in (None, ""):
            continue  # unused stock row of a hand-entry template
        groups.setdefault((_s(r.get("sb_no")), _s(r.get("invsn")) or "1"), []).append(r)

    def sort_key(k):
        try:
            return (_d(groups[k][0].get("sb_date")) or date.max, k)
        except ValueError:
            return (date.max, k)
    out = []
    for key in sorted(groups, key=sort_key):
        grp = groups[key]
        g = GroupOutcome(key=f"SB {key[0] or '?'} / invoice {key[1]}", rows=[r["_row"] for r in grp])
        out.append(g)
        try:
            with session.begin_nested():
                _apply_export_group(session, grp, g)
        except (BusinessError, ValueError) as exc:
            g.status, g.message = "error", str(exc)
    return out


def _apply_export_group(session: Session, grp: list[dict], g: GroupOutcome):
    h = grp[0]
    for key, label in (("sb_no", "SB No"), ("sb_date", "SB Date"), ("invoice_no", "Invoice No")):
        _need(h, key, label)
    inv_no = _s(h["invoice_no"])
    existing = session.scalar(select(ExportShipment).where(ExportShipment.invoice_no == inv_no))
    if existing:
        g.status, g.doc_id = "skipped", existing.id
        g.message = f"export invoice {inv_no} is already in the tracker"
        return
    lines = []
    for r in grp:
        desc = _s(_need(r, "description", "Description"))
        lt = _s(_need(r, "line_type", "Type (FG / ITEM)")).upper()
        code = _s(_need(r, "code", "Model / Part No")).upper()
        if lt == "FG":
            m = session.scalar(select(FGModel).where(func.upper(FGModel.code) == code))
            if not m:
                raise ValidationError(f"row {r['_row']}: model '{code}' is not in the tracker (sheet Models)")
            target = dict(model_id=m.id)
        elif lt == "ITEM":
            it = _item_by_code(session, code)
            if not it:
                raise ValidationError(f"row {r['_row']}: part '{code}' is not in the tracker (sheet Parts)")
            target = dict(item_id=it.id)
        else:
            raise ValidationError(f"row {r['_row']}: Type must be FG or ITEM")
        _save_export_mapping(session, desc, **target)
        lines.append(dict(line_type=lt, qty=_n(_need(r, "qty", "Qty")), fob_value=_n(r.get("fob")) or 0,
                          export_item_name=(_s(r.get("export_item_name")) or desc)[:255],
                          sb_invoice_serial=int(_n(r.get("invsn")) or 1),
                          sb_item_serial=int(_n(r.get("itemsn")) or 0) or None,
                          hs_code=_s(r.get("hs_code")) or None, **target))
    sh = export_svc.post_export(
        session, invoice_no=inv_no, export_date=_d(h["sb_date"]), lines=lines, shipping_bill_no=_s(h["sb_no"]),
        shipping_bill_date=_d(h["sb_date"]), consignee=_s(h.get("consignee")) or None,
        country=_s(h.get("country")) or None, port_of_export=_s(h.get("port")) or None)
    g.doc_id, g.lines, g.value = sh.id, len(sh.lines), float(sh.total_recredit)
    g.status = "posted"
    g.message = f"export {inv_no}: {len(sh.lines)} line(s), bond recredit {sh.total_recredit:,.2f}"


def _save_export_mapping(session: Session, description: str, model_id=None, item_id=None):
    key = masters.normalize_description(description)
    if not key:
        return
    m = session.scalar(select(ExportMapping).where(ExportMapping.normalized_description == key))
    if m is None:
        session.add(ExportMapping(normalized_description=key, sample_description=description[:512],
                                  model_id=model_id, item_id=item_id))
    else:
        m.model_id, m.item_id = model_id, item_id
    session.flush()


# =================================================================== BOMs
BOM_SHEET = "BOM Lines"
BOM_COLS = [
    Col("model_code", "Model code", 14, True, "text",
        "Finished-goods model, e.g. 4343-GEN5 (sheet 'Models'). A new code is created as a new model "
        "when 'Model name' is filled in."),
    Col("model_name", "Model name (new models only)", 28, False),
    Col("effective_from", "Effective from", 12, True, "date",
        "Date this BOM version applies from. Production on or after this date uses it. Use a NEW date to "
        "change a BOM - existing versions are never overwritten."),
    Col("part_no", "Component Part No", 16, True, "text",
        "Unique internal code of the component (sheet 'Parts'). A code that isn't there yet is created as "
        "a new part - then fill in Description and UOM."),
    Col("part_status", "Part No status", 22, kind="formula"),
    Col("description", "Component description", 40, False, "text", "Needed only for a new part"),
    Col("uom", "UOM", 7, False, "text", "Needed only for a new part (default NOS)"),
    Col("item_type", "Type (COMPONENT / ACCESSORY)", 13, False, "text", "Only for a new part (default COMPONENT)"),
    Col("qty", "Qty per finished unit", 12, True, "num", "Quantity of this component in ONE finished unit"),
    Col("remarks", "Version remarks", 30, False, "text", "e.g. ECN number - taken from the first row of the version"),
]
BOM_FORMULAS = {
    "part_status": 'IF({part_no}{r}="","⚠ enter Part No",IF(COUNTIF(Parts!$A:$A,{part_no}{r})>0,'
                   '"✓ existing part","➕ new part - created on upload"))',
}


def build_bom_template(session: Session) -> bytes:
    """Template pre-filled with the current (latest) BOM of every active model."""
    rows, current = [], {}
    for m in session.scalars(select(FGModel).where(FGModel.active.is_(True)).order_by(FGModel.code)):
        if not m.boms:
            continue
        bom = m.boms[-1]
        current[m.code] = bom
        for ln in bom.lines:
            rows.append(dict(model_code=m.code, effective_from=bom.effective_from, part_no=ln.item.code,
                             description=ln.item.description, uom=ln.item.uom, item_type=ln.item.item_type,
                             qty=float(ln.qty_per_unit), remarks=bom.remarks))
    wb = Workbook()
    _readme(wb, "Bill of Material upload template - EOU Bond Tracker", [
        ("How to use", "h"),
        ("1. One row per component of a finished good. The current BOM of every model is already filled in.", ""),
        ("2. To CHANGE a BOM: edit its rows and put a NEW 'Effective from' date on all of them (e.g. the ECN date). "
         "Uploading creates the next version; earlier versions stay as they are, so past production is unaffected.", ""),
        ("3. To ADD a model: new rows with the model code, its name, an effective date and its components.", ""),
        ("4. Components are tagged by their unique Part No (sheet 'Parts'). A new Part No is created as a new part - "
         "give it a description and UOM.", ""),
        ("5. Upload on Masters -> Models & BOMs -> Upload BOMs. You'll see a preview first. Rows that are unchanged "
         "(same model, date and components as a version already in the tracker) are skipped.", ""),
        ("Yellow cells are still needed. Green columns are formulas", "need"),
        ("Part No status = IF(COUNTIF(Parts!A:A, Part No) > 0, '✓ existing part', '➕ new part').", "calc"),
        ("'Summary' sheet: components per model = COUNTIF over this sheet, compared with the version in the tracker.", "calc"),
    ])
    ws = wb.create_sheet(BOM_SHEET)
    letter, last = _sheet_from_cols(ws, BOM_COLS, rows, BOM_FORMULAS)
    dv = DataValidation(type="list", formula1="=Models!$A$2:$A$500", allow_blank=True, showErrorMessage=True,
                        errorStyle="warning", errorTitle="New model",
                        error="This model is not in the tracker yet - fill in 'Model name' to create it.")
    ws.add_data_validation(dv)
    dv.add(f"{letter['model_code']}2:{letter['model_code']}{last}")
    dv2 = DataValidation(type="list", formula1="=Parts!$A$2:$A$5000", allow_blank=True, showErrorMessage=True,
                         errorStyle="warning", errorTitle="New part",
                         error="This Part No is not in the item master yet - it will be created as a new part.")
    ws.add_data_validation(dv2)
    dv2.add(f"{letter['part_no']}2:{letter['part_no']}{last}")
    dv3 = DataValidation(type="list", formula1='"COMPONENT,ACCESSORY"', allow_blank=True)
    ws.add_data_validation(dv3)
    dv3.add(f"{letter['item_type']}2:{letter['item_type']}{last}")
    _list_sheet(wb, "Models", ["Model code", "Name", "Current BOM", "Effective from", "Components"],
                [[m.code, m.name, f"v{m.boms[-1].version}" if m.boms else "none",
                  m.boms[-1].effective_from if m.boms else None, len(m.boms[-1].lines) if m.boms else 0]
                 for m in session.scalars(select(FGModel).where(FGModel.active.is_(True)).order_by(FGModel.code))],
                [16, 40, 12, 14, 12])
    _list_sheet(wb, "Parts", ["Code", "Description", "UOM", "Type"],
                [[i.code, i.description, i.uom, i.item_type] for i in
                 session.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.code))], [18, 60, 8, 12])
    sm = _list_sheet(wb, "Summary", ["Model code", "First row", "Components in template", "Components in current BOM",
                                     "Total qty per unit in template", "Current version"], [], [16, 10, 14, 14, 16, 14])
    A, Q = letter["model_code"], letter["qty"]
    for n, code in enumerate(sorted({r["model_code"] for r in rows}), start=2):
        first = next(i for i, r in enumerate(rows, start=2) if r["model_code"] == code)
        sm.cell(row=n, column=1, value=code)
        _link(sm.cell(row=n, column=2), BOM_SHEET, first, f"row {first} →")
        sm.cell(row=n, column=3, value=f"=COUNTIF('{BOM_SHEET}'!${A}:${A},A{n})")
        sm.cell(row=n, column=4, value=len(current[code].lines))
        sm.cell(row=n, column=5, value=f"=SUMIF('{BOM_SHEET}'!${A}:${A},A{n},'{BOM_SHEET}'!${Q}:${Q})")
        sm.cell(row=n, column=6, value=f"v{current[code].version} from {current[code].effective_from:%d-%m-%Y}")
    wb.active = 1
    return _save(wb)


def read_bom_template(blob: bytes) -> list[dict]:
    return _read_rows(blob, BOM_SHEET, BOM_COLS)


def apply_bom_rows(session: Session, rows: list[dict]) -> list[GroupOutcome]:
    """One new BOM version per (model, effective date), each in its own savepoint."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        try:
            eff = _d(r.get("effective_from"))
        except ValueError:
            eff = None
        groups.setdefault((_s(r.get("model_code")).upper(), eff), []).append(r)
    out = []
    for (code, eff), grp in groups.items():
        g = GroupOutcome(key=f"{code or '?'} from {eff:%d-%m-%Y}" if eff else f"{code or '?'} (no date)",
                         rows=[r["_row"] for r in grp])
        out.append(g)
        try:
            with session.begin_nested():
                _apply_bom_group(session, code, eff, grp, g)
        except (BusinessError, ValueError) as exc:
            g.status, g.message = "error", str(exc)
            g.new_parts, g.new_suppliers = [], []
    return out


def _apply_bom_group(session: Session, code: str, eff, grp: list[dict], g: GroupOutcome):
    h = grp[0]
    _need(h, "model_code", "Model code")
    if eff is None:
        _d(_need(h, "effective_from", "Effective from"))  # raises a clear message for a bad date
    model = session.scalar(select(FGModel).where(func.upper(FGModel.code) == code))
    if model is None:
        name = _s(h.get("model_name"))
        if not name:
            raise ValidationError(f"row {h['_row']}: model '{code}' is not in the tracker - fill in 'Model name' "
                                  "to create it")
        model = masters.create_model(session, code=code, name=name)
        g.new_suppliers.append(f"model {code}")  # shown in the 'new' column of the preview
    lines, seen = [], {}
    for r in grp:
        part = _s(_need(r, "part_no", "Component Part No")).upper()
        qty = _n(_need(r, "qty", "Qty per finished unit"))
        if qty is None or qty <= 0:
            raise ValidationError(f"row {r['_row']}: Qty per finished unit must be more than 0")
        item = _item_by_code(session, part)
        if item is None:
            desc = _s(r.get("description"))
            if not desc:
                raise ValidationError(f"row {r['_row']}: '{part}' is a new part - give it a description")
            item = masters.create_item(session, code=part, description=desc[:255], uom=_s(r.get("uom")) or "NOS",
                                       item_type=(_s(r.get("item_type")).upper() or "COMPONENT"))
            g.new_parts.append(part)
        if item.id in seen:
            raise ValidationError(f"rows {seen[item.id]} and {r['_row']}: {item.code} appears twice in this BOM - "
                                  "put the total quantity on one row")
        seen[item.id] = r["_row"]
        lines.append(dict(item_id=item.id, qty_per_unit=qty))
    wanted = {(ln["item_id"], round(float(ln["qty_per_unit"]), 4)) for ln in lines}

    def same(bom):
        return {(ln.item_id, round(float(ln.qty_per_unit), 4)) for ln in bom.lines} == wanted

    existing = session.scalar(select(BOM).where(BOM.model_id == model.id, BOM.effective_from == eff))
    if existing is not None:
        if same(existing):
            g.status, g.doc_id = "skipped", existing.id
            g.message = f"same as v{existing.version} already in the tracker"
            return
        raise ValidationError(f"{model.code} already has BOM v{existing.version} effective {eff:%d-%m-%Y} with "
                              "different components. BOM versions are never changed - use a new 'Effective from' date")
    latest = model.boms[-1] if model.boms else None
    if latest is not None and latest.effective_from < eff and same(latest):
        g.status, g.doc_id = "skipped", latest.id
        g.message = f"components unchanged from current v{latest.version} - no new version needed"
        return
    bom = masters.create_bom_version(session, model_id=model.id, effective_from=eff, lines=lines,
                                     remarks=_s(h.get("remarks")) or None)
    g.status, g.doc_id, g.lines = "created", bom.id, len(bom.lines)
    g.message = f"{model.code} BOM v{bom.version} effective {eff:%d-%m-%Y}: {len(bom.lines)} component(s)"
    if latest is not None and latest.effective_from > eff:
        g.message += f" (note: v{latest.version} from {latest.effective_from:%d-%m-%Y} stays the latest)"
