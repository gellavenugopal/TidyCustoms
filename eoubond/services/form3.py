"""Quarterly Form III return, in the layout of the filed template (Form3_Q4.xls).

One "Details of Goods" row per Bill of Entry item (= one import lot). The
quantity columns are CUMULATIVE from import to the quarter end, which is what
the filed return shows: in every row
    N = Q + R            imported = received + not received
    Y = U + V + W + X    used for intended purpose
    Z = Q - S - T - Y    pending in stock
    AC = Z + AA + AB
A BE item is listed when it was imported in the quarter or still had stock
pending at the start of the quarter.                                [A-F3-4]

Mapping from the tracker's movements (traced FIFO per import lot):
    DTA clearance of an ITEM as such           -> S  cleared on payment of duty
    export of an ITEM as such                  -> V  (or T re-export)  [A-F3-2]
    DTA clearance of finished goods            -> U  used for goods supplied domestically
    export of finished goods                   -> V  used for goods exported
    inputs inside unsold finished goods        -> Z  pending (or V)    [A-F3-3]
Job work, unit transfer, end-use supply and output services are not tracked,
so W, X, AA, AB are 0 and those sheets carry headers only.          [A-F3-5]
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import (ConsumptionAllocation, DisposalAllocation, DTAClearance, DTALine, ExportLine,
                      ExportShipment, ImportInvoice, ImportLine, ImportLot, ProductionBatch)
from ..numbers import ZERO, money, qty

SHEETS = ("Details of Goods", "Re-Export", "Goods cleared on payment", "Job Worker Unit", "Unit Transfer",
          "End Use Recipient")
_BE = ["BE Number", "BE Date(DD-MMM-YY)", "Port of Import", "Invoice Serial Number", "Item Serial Number"]
DETAILS_HEADERS = _BE + [
    "Description of Imported goods", "Unit Quantity Code (UQC)", "Value Of Goods", "BCD Forgone", "IGST Forgone",
    "Cess Forgone", "Other Custom Duties Forgone", "Total Duty Forgone", "Quantity of Import",
    "Date of clearance(DD-MMM-YY)", "Date of Receipt of Goods at importer's permises(DD-MMM-YY)",
    "Quantity Received at importer's permises", "Quantity Not Received at importer's permises",
    "Quantity of imported goods cleared on payment of duty", "Quantity of imported goods which are Re-Exported",
    "Quantity of goods use for manufacturing goods that are supply domestically",
    "Quantity of goods use for manufacturing goods that are exported",
    "Quantity of goods supplied to end-use recipient", "Quantity of Goods Used for Provision of Output Service",
    "Quantity of Goods used for Intended Purpose",
    "Quantity of Goods Pending in stock in the importer’s premises (A)",
    "Quantity of Goods pending in stock- B with Job Worker (B)",
    "Quantity of Goods pending in Stock in other Units of the Importer (C)",
    "Quantity of goods pending in stock (A+B+C)"]
REEXPORT_HEADERS = _BE + [
    "Unit Quantity Code (UQC)", "Description of re-exported Goods", "SB No.", "SB Date(DD-MMM-YY)",
    "Port of Export", "SB Invoice Serial No.", "SB Item Serial No.", "Quantity Re-exported", "Actual quatity"]
CLEARED_HEADERS = _BE + [
    "Unit Quantity Code (UQC)", "Quantity Cleared for home consumption on Payment of Duty", "Challan Number",
    "Challan Date(DD-MMM-YY)", "BCD", "Other Custom Duties", "IGST", "CESS", "Interest",
    "Total duty + interest paid for clearance for home consumption", "Capital Goods cleared after depreciation?",
    "Remarks"]
JOBWORK_HEADERS = _BE + [
    "Unit Quantity Code (UQC)", "Quantity Transferred to the Unit", "GSTIN of Job Worker", "PAN of Job Worker",
    "Delivery Challan/E-Way Bill Number", "Delivery Challan/E-Way Bill Date(DD-MMM-YY)",
    "Quantity used for intended purpose and removed directly from the JW premises",
    "Quantity used for Intended Purpose and received back at Importer’s premises",
    "Quantity used for intermediate product and received back at Importer’s premises",
    "Quantity received at the Importer’s premises from the JW without processing"]
UNIT_TRANSFER_HEADERS = _BE + [
    "Unit Quantity Code (UQC)", "Quantity Transferred to the Unit", "GSTIN of Unit Transfer", "PAN of Unit Transfer",
    "Delivery Challan/E-Way Bill Number", "Delivery Challan/E-Way Bill Date(DD-MMM-YY)",
    "Quantity used for Intended Purpose and removed directly from the unit",
    "Quantity used for Intermediate Product and received back at Importer’s premises",
    "Quantity used for Intended Purpose and received back at Importer’s premises",
    "Quantity received at the Importer’s premises from the unit without processing"]
END_USE_HEADERS = _BE + [
    "Unit Quantity Code (UQC)", "Quantity Supplied to End Use Recipient", "GSTIN of End Use Recipient",
    "Date of Goods supplied to End use Recipient(DD-MMM-YY)", "IEC of the End use recipient",
    "Delivery Challan/E-Way Bill Number", "Delivery Challan/E-Way Bill Date(DD-MMM-YY)", "Nature of Supply",
    "Replenishment Authorization Detail", "Goods were exported by", "Port of Export", "SB No.",
    "SB Date(DD-MMM-YY)", "SB Invoice Serial No.", "SB Item Serial No.", "Description of Goods Exported",
    "Quantity Exported", "Quantity of Imported inputs utilized in the goods exported", "Export Under Replenishment",
    "Remarks", "BRC Received"]
HEADERS = dict(zip(SHEETS, (DETAILS_HEADERS, REEXPORT_HEADERS, CLEARED_HEADERS, JOBWORK_HEADERS,
                            UNIT_TRANSFER_HEADERS, END_USE_HEADERS)))
DATE_COLUMNS = {"Details of Goods": {15}}  # P = real date cell (d-mmm-yy); other dates are text, as filed


def ddmmmyy(d: date | None) -> str:
    return d.strftime("%d-%b-%y").upper() if d else ""


def quarter_start(end: date) -> date:
    return date(end.year, end.month - 2, 1)


def quarter_end_for(d: date) -> date:
    m = ((d.month - 1) // 3 + 1) * 3
    return (date(d.year + (m == 12), 1 if m == 12 else m + 1, 1)) - timedelta(days=1)


def quarter_label(end: date) -> str:
    """Both conventions, since the filed file name ('Q4' for Oct-Dec) is calendar based."""
    cal_q = (end.month - 1) // 3 + 1
    fy_q = (cal_q + 2) % 4 + 1
    fy = end.year if end.month > 3 else end.year - 1
    return (f"{quarter_start(end):%b %Y} - {end:%b %Y} (calendar Q{cal_q} {end.year} / "
            f"FY {fy}-{str(fy + 1)[-2:]} Q{fy_q})")


@dataclass
class Form3:
    start: date
    end: date
    sheets: dict[str, list[list]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # for the working file: key per "Details of Goods" row, and the movements behind S/T/U/V
    keys: list[str] = field(default_factory=list)
    movements: list[dict] = field(default_factory=list)


def _num(v: Decimal):
    v = Decimal(v)
    return int(v) if v == v.to_integral_value() else float(v)


def _be_no(inv: ImportInvoice):
    s = (inv.be_no or "").strip()
    return int(s) if s.isdigit() else s


def _be_cols(inv: ImportInvoice, line: ImportLine) -> list:
    return [_be_no(inv), ddmmmyy(inv.be_date or inv.invoice_date), inv.port_of_import or "",
            inv.be_invoice_serial or 1, line.be_item_serial or line.line_no]


def _movements(session: Session, upto: date):
    """Per import lot: list of (date, kind, line_type, allocation, doc_line) for exports and DTA."""
    out = defaultdict(list)
    for a, el, sh in session.execute(
            select(DisposalAllocation, ExportLine, ExportShipment).join(
                ExportLine, DisposalAllocation.export_line_id == ExportLine.id).join(ExportShipment)
            .where(ExportShipment.export_date <= upto)):
        out[a.import_lot_id].append((sh.export_date, "EXPORT", el.line_type, a, el))
    for a, dl, clr in session.execute(
            select(DisposalAllocation, DTALine, DTAClearance).join(
                DTALine, DisposalAllocation.dta_line_id == DTALine.id).join(DTAClearance)
            .where(DTAClearance.clearance_date <= upto)):
        out[a.import_lot_id].append((clr.clearance_date, "DTA", dl.line_type, a, dl))
    return out


def _consumed(session: Session, upto: date) -> dict[int, Decimal]:
    rows = session.execute(select(ConsumptionAllocation.import_lot_id, func.sum(ConsumptionAllocation.qty))
                           .join(ProductionBatch).where(ProductionBatch.production_date <= upto)
                           .group_by(ConsumptionAllocation.import_lot_id))
    return {lot_id: qty(q) for lot_id, q in rows}


def _usage(lot: ImportLot, moves, consumed: dict[int, Decimal]) -> dict[str, Decimal]:
    rules = get_rules()
    u = dict(S=ZERO, T=ZERO, U=ZERO, V=ZERO)
    for _d, kind, ltype, a, _l in moves.get(lot.id, []):
        if kind == "DTA":
            u["S" if ltype == "ITEM" else "U"] += a.qty
        elif ltype == "ITEM":
            u["T" if rules.form3_item_export_as == "re_export" else "V"] += a.qty
        elif rules.form3_fg_stock_as == "pending":
            u["V"] += a.qty
    if rules.form3_fg_stock_as == "export":  # everything consumed and not sold domestically counts as export use
        u["V"] += consumed.get(lot.id, ZERO) - u["U"]
    return {k: qty(v) for k, v in u.items()}


def _movement_rows(session: Session, lot: ImportLot, moves, end: date) -> list[dict]:
    """The individual movements that make up S/T/U/V for one lot (same rules as _usage)."""
    rules = get_rules()
    out = []
    for d, kind, ltype, a, doc in moves.get(lot.id, []):
        ref = doc.shipment.invoice_no if kind == "EXPORT" else doc.clearance.clearance_no
        doc_name = "Export" if kind == "EXPORT" else "DTA clearance"
        if kind == "DTA":
            col = "S" if ltype == "ITEM" else "U"
        elif ltype == "ITEM":
            col = "T" if rules.form3_item_export_as == "re_export" else "V"
        elif rules.form3_fg_stock_as == "pending":
            col = "V"
        else:
            col = None  # counted at consumption (below)
        if col:
            out.append(dict(date=d, document=doc_name, ref=ref, line_type=ltype, column=col, qty=a.qty))
        if rules.form3_fg_stock_as == "export" and kind == "DTA" and ltype == "FG":
            out.append(dict(date=d, document=doc_name, ref=ref, line_type=ltype, column="V", qty=-a.qty,
                            note="moved from V to U: the detector was sold in India"))
    if rules.form3_fg_stock_as == "export":
        for c, b in session.execute(select(ConsumptionAllocation, ProductionBatch).join(ProductionBatch)
                                    .where(ConsumptionAllocation.import_lot_id == lot.id,
                                           ProductionBatch.production_date <= end)):
            out.append(dict(date=b.production_date, document="Production", ref=b.batch_no, line_type="FG",
                            column="V", qty=c.qty))
    return sorted(out, key=lambda m: m["date"])


def _pending(lot: ImportLot, u: dict) -> Decimal:
    return qty(lot.qty_received - u["S"] - u["T"] - u["U"] - u["V"])


def build(session: Session, quarter_end: date) -> Form3:
    rules = get_rules()
    start, end = quarter_start(quarter_end), quarter_end
    f = Form3(start=start, end=end, sheets={s: [] for s in SHEETS})
    moves_end, moves_open = _movements(session, end), _movements(session, start - timedelta(days=1))
    cons_end, cons_open = _consumed(session, end), _consumed(session, start - timedelta(days=1))

    lots = session.execute(select(ImportLot, ImportLine, ImportInvoice).join(
        ImportLine, ImportLot.import_line_id == ImportLine.id).join(ImportInvoice)
        .where(ImportInvoice.status == "POSTED", ImportInvoice.import_type == "BOND")  # duty-paid goods aren't in bond
        .order_by(func.coalesce(ImportInvoice.be_date, ImportInvoice.invoice_date), ImportInvoice.be_no,
                  ImportInvoice.be_invoice_serial, ImportLine.line_no)).all()
    seen_be_missing = set()
    for lot, line, inv in lots:
        be_date = inv.be_date or inv.invoice_date
        if be_date > end:
            continue
        in_quarter = be_date >= start
        if not in_quarter and _pending(lot, _usage(lot, moves_open, cons_open)) <= 0:
            continue  # fully used before the quarter began
        u = _usage(lot, moves_end, cons_end)
        z = _pending(lot, u)
        if inv.id not in seen_be_missing:
            missing = [n for n, v in (("BE number", inv.be_no), ("BE date", inv.be_date),
                                      ("port of import", inv.port_of_import), ("date of clearance", inv.clearance_date),
                                      ("date of receipt", inv.receipt_date)) if not v]
            if missing:
                f.warnings.append(f"Import {inv.invoice_no}: {', '.join(missing)} not entered"
                                  + (" (BE/invoice date used for dates)" if "BE number" not in missing else ""))
            seen_be_missing.add(inv.id)
        if not line.has_duty_split:
            f.warnings.append(f"Import {inv.invoice_no} line {line.line_no}: duty forgone not split into "
                              "BCD/SWS/IGST - total reported under Other Custom Duties")
            bcd, igst, cess, other = ZERO, ZERO, ZERO, line.duty_saved_value
        else:
            sws = line.sws_forgone
            bcd = line.bcd_forgone + (sws if rules.form3_sws_in_bcd_column else ZERO)
            other = line.other_forgone + (ZERO if rules.form3_sws_in_bcd_column else sws)
            igst, cess = line.igst_forgone, line.cess_forgone
        n, q = line.qty, lot.qty_received
        y = u["U"] + u["V"]
        f.sheets["Details of Goods"].append(_be_cols(inv, line) + [
            line.description, lot.item.uom, _num(line.assessable_value), _num(bcd), _num(igst), _num(cess),
            _num(other), _num(money(bcd + igst + cess + other)), _num(n),
            ddmmmyy(inv.clearance_date or be_date), inv.receipt_date or inv.clearance_date or be_date,
            _num(q), _num(qty(n - q)), _num(u["S"]), _num(u["T"]), _num(u["U"]), _num(u["V"]), 0, 0, _num(y),
            _num(z), 0, 0, _num(z)])
        row = f.sheets["Details of Goods"][-1]
        key = f"{row[0]}|{row[3]}|{row[4]}"  # BE No | invoice serial | item serial (= A&"|"&D&"|"&E in Excel)
        f.keys.append(key)
        for m in _movement_rows(session, lot, moves_end, end):
            f.movements.append(dict(key=key, **m))

        # transaction sheets: events dated inside the quarter
        for d, kind, ltype, a, doc in moves_end.get(lot.id, []):
            if not (start <= d <= end) or ltype != "ITEM":
                continue
            if kind == "EXPORT" and rules.form3_item_export_as == "re_export":
                sh = doc.shipment
                f.sheets["Re-Export"].append(_be_cols(inv, line) + [
                    lot.item.uom, doc.export_item_name, sh.shipping_bill_no or "", ddmmmyy(sh.shipping_bill_date or d),
                    sh.port_of_export or "", doc.sb_invoice_serial or 1, doc.sb_item_serial or doc.line_no,
                    _num(a.qty), _num(a.qty)])
                if not sh.shipping_bill_no:
                    f.warnings.append(f"Export {sh.invoice_no}: shipping bill number missing")
            elif kind == "DTA":
                clr = doc.clearance
                bcd_p = (a.bcd or ZERO) + ((a.sws or ZERO) if rules.form3_sws_in_bcd_column else ZERO)
                oth_p = ZERO if rules.form3_sws_in_bcd_column else (a.sws or ZERO)
                f.sheets["Goods cleared on payment"].append(_be_cols(inv, line) + [
                    lot.item.uom, _num(a.qty), clr.challan_no or "", ddmmmyy(clr.paid_date), _num(bcd_p), _num(oth_p),
                    0, 0, 0, _num(money(bcd_p + oth_p)), "No",
                    f"DTA clearance {clr.clearance_no}; IGST charged on the domestic supply"])
                if clr.status != "PAID":
                    f.warnings.append(f"DTA clearance {clr.clearance_no}: duty not yet marked paid (no challan)")
    if not f.sheets["Details of Goods"]:
        f.warnings.append("No Bill of Entry items with stock in this quarter")
    return f


def to_xls(form: Form3) -> bytes:
    """Write the six sheets as a .xls workbook, matching the filed template."""
    import io

    import xlwt

    wb = xlwt.Workbook(encoding="utf-8")
    head = xlwt.easyxf("font: bold on; align: wrap on, vert centre")
    datef = xlwt.easyxf(num_format_str="d-mmm-yy")
    for name in SHEETS:
        ws = wb.add_sheet(name)
        for c, h in enumerate(HEADERS[name]):
            ws.write(0, c, h, head)
            ws.col(c).width = 256 * min(max(len(h) // 2, 12), 40)
        for r, row in enumerate(form.sheets[name], start=1):
            for c, v in enumerate(row):
                if c in DATE_COLUMNS.get(name, ()) and isinstance(v, date):
                    ws.write(r, c, v, datef)
                else:
                    ws.write(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
