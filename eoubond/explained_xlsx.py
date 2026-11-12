"""Working workbooks: every report figure is an Excel formula over the source
rows on another sheet, with links between sheets, so anyone opening the file
can follow how each number is made.

Formulas are deliberately simple (SUM, SUMIF, VLOOKUP, ROUND, IF, TEXT and
"&"): each sheet that is summarised has one key column (e.g. Bond|Month) and
the summary uses SUMIF on that key.

  bond_workbook()  Read me, Bonds (reconciliation), Monthly summary, Ledger,
                   Stock by bond, Lots, Inputs in FG, DTA lines, DTA inputs
  form3_workbook() Read me, Details of Goods (formulas), Movements
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_rules
from .models import (Bond, BondLedgerEntry, ConsumptionAllocation, DisposalAllocation, DTAClearance, DTALine,
                     ImportInvoice, ImportLine, ImportLot)
from .numbers import ZERO

HEAD = PatternFill("solid", fgColor="1F3A5F")
CALC = PatternFill("solid", fgColor="E2EFDA")
TOTAL = PatternFill("solid", fgColor="D9E1F2")
FONT_CALC = Font(color="375623")
MONEY, QTY, DATE = "#,##0.00", "#,##0.###", "DD-MM-YYYY"


def _f(v):
    return float(v) if isinstance(v, Decimal) else v


class _Sheet:
    """Helper: header row, value / formula cells, column formats, links."""

    def __init__(self, wb, title, cols, widths=None, note=None):
        self.ws = wb.create_sheet(title)
        self.title = title
        self.cols = cols  # [(key, header, fmt, is_formula)]
        self.L = {k: get_column_letter(i) for i, (k, *_r) in enumerate(cols, start=1)}
        start = 1
        if note:
            self.ws.cell(row=1, column=1, value=note).font = Font(italic=True, color="595959")
            start = 2
        self.head = start
        for i, (k, h, fmt, is_f) in enumerate(cols, start=1):
            c = self.ws.cell(row=start, column=i, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="375623") if is_f else HEAD
            c.alignment = Alignment(wrap_text=True, vertical="center")
            self.ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(k, 14)
        self.ws.row_dimensions[start].height = 32
        self.ws.freeze_panes = self.ws.cell(row=start + 1, column=1)
        self.row = start

    def add(self, values: dict, formulas: dict | None = None, links: dict | None = None, fill=None) -> int:
        self.row += 1
        r = self.row
        for i, (k, _h, fmt, is_f) in enumerate(self.cols, start=1):
            cell = self.ws.cell(row=r, column=i)
            if formulas and k in formulas:
                cell.value = "=" + formulas[k].format(r=r, p=r - 1, **self.L)
                cell.fill = fill or CALC
                cell.font = FONT_CALC
            else:
                cell.value = _f(values.get(k))
                if fill:
                    cell.fill = fill
            if fmt:
                cell.number_format = fmt
            if links and k in links and links[k]:
                sheet, row = links[k]
                cell.hyperlink = f"#'{sheet}'!A{row}"
                cell.font = Font(color="0563C1", underline="single")
        return r

    def col(self, key) -> str:
        return self.L[key]

    def ref(self, key) -> str:  # whole-column reference for SUMIF etc.
        return f"'{self.title}'!${self.L[key]}:${self.L[key]}"


def _readme(wb, title, lines, links=None):
    ws = wb.active
    ws.title = "Read me"
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 110
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Prepared {date.today():%d-%m-%Y} by EOU Bond Tracker. Green cells are formulas; blue underlined cells are links."
    ws["A2"].font = Font(italic=True)
    r = 4
    for sheet, text in lines:
        a = ws.cell(row=r, column=1, value=sheet)
        if sheet and (links is None or sheet in links):
            a.hyperlink = f"#'{sheet}'!A1"
            a.font = Font(color="0563C1", underline="single", bold=True)
        b = ws.cell(row=r, column=2, value=text)
        b.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    return ws


def _bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =================================================================== bonds & stock
def bond_workbook(session: Session) -> bytes:
    rules = get_rules()
    wb = Workbook()
    bonds = list(session.scalars(select(Bond).order_by(Bond.bond_no)))
    _readme(wb, "Bond, stock and DTA working", [
        ("Bonds", "One row per bond. Debits / Credits = SUMIF over the Ledger. Balance = Bond amount - Debits + Credits; it "
                  "is checked against the tracker's own balance. Reconciliation: Utilised (Amount - Balance) = duty saved still "
                  "in stock + duty saved on DTA clearances awaiting recredit + duty saved on goods not received."),
        ("Monthly summary", "Opening = bond amount for the first month, else the previous month's closing. Debits / Credits / Memo "
                            "= SUMIF(Ledger key 'Bond|Month'). Closing = Opening - Debits + Credits."),
        ("Ledger", "Every bond ledger entry in date order. Month = TEXT(Date,\"yyyy-mm\"). Balance = previous balance (or the "
                   "bond amount on the bond's first row) - Debit + Credit."),
        ("Stock by bond", "Raw material still in stock = SUMIF over Lots (key 'Bond|Item'); inputs already built into unsold "
                          "finished goods = SUMIF over 'Inputs in FG'. Total duty saved = RM + in FG."),
        ("Lots", "One row per import lot (BE item). Available = Received - Used, for quantity, duty saved and assessable value."),
        ("Inputs in FG", "Import-lot inputs inside finished detectors that are still in stock (not yet exported / sold)."),
        ("DTA lines", "Each DTA clearance line. BCD and SWS = SUMIF over 'DTA inputs' (key 'Clearance|Line')"
                      + (", rounded to the rupee per line" if rules.round_duty_to_rupee else "")
                      + ". Duty = BCD + SWS (GST is not part of it). Checked against the tracker's figure."),
        ("DTA inputs", "The imported inputs in each DTA line (FIFO). BCD = ROUND(AV x BCD rate %, 2); SWS = ROUND(BCD x SWS rate %, 2)."),
    ])

    # ---------------- Ledger
    led = _Sheet(wb, "Ledger", [("bond", "Bond", None, False), ("date", "Date", DATE, False),
                                ("month", "Month", None, True), ("key", "Key (Bond|Month)", None, True),
                                ("type", "Entry", None, False), ("ref", "Reference", None, False),
                                ("debit", "Debit", MONEY, False), ("credit", "Credit", MONEY, False),
                                ("memo", "Memo value", MONEY, False), ("balance", "Balance", MONEY, True),
                                ("narration", "Narration", None, False), ("by", "By", None, False)],
                 dict(bond=18, key=24, type=18, ref=16, narration=50))
    first_led_row, month_first = {}, {}
    entries = session.scalars(select(BondLedgerEntry).join(Bond).order_by(Bond.bond_no, BondLedgerEntry.entry_date,
                                                                         BondLedgerEntry.id))
    for e in entries:
        r = led.add(dict(bond=e.bond.bond_no, date=e.entry_date, type=e.entry_type, ref=f"{e.source_type} {e.source_ref}",
                         debit=e.debit, credit=e.credit, memo=e.memo_value, narration=e.narration, by=e.created_by),
                    dict(month='TEXT({date}{r},"yyyy-mm")', key='{bond}{r}&"|"&{month}{r}',
                         balance="IF({bond}{r}<>{bond}{p},VLOOKUP({bond}{r},Bonds!$A:$B,2,0),{balance}{p})"
                                 "-{debit}{r}+{credit}{r}"))
        first_led_row.setdefault(e.bond.bond_no, r)
        month_first.setdefault((e.bond.bond_no, e.entry_date.strftime("%Y-%m")), r)

    # ---------------- Lots / Inputs in FG / Stock by bond
    lots = _Sheet(wb, "Lots", [("id", "Lot", None, False), ("bond", "Bond", None, False), ("item", "Item", None, False),
                               ("desc", "Description", None, False), ("be", "BE No", None, False),
                               ("be_date", "BE / invoice date", DATE, False), ("inv", "Invoice", None, False),
                               ("q_in", "Qty received", QTY, False), ("q_used", "Qty used", QTY, False),
                               ("q_av", "Qty available", QTY, True), ("d_in", "Duty saved of lot", MONEY, False),
                               ("d_used", "Duty saved used", MONEY, False), ("d_av", "Duty saved available", MONEY, True),
                               ("a_in", "AV of lot", MONEY, False), ("a_used", "AV used", MONEY, False),
                               ("a_av", "AV available", MONEY, True), ("key", "Key (Bond|Item)", None, True)],
                  dict(bond=18, desc=40, key=26))
    for lot, line, inv in session.execute(select(ImportLot, ImportLine, ImportInvoice).join(
            ImportLine, ImportLot.import_line_id == ImportLine.id).join(ImportInvoice).order_by(ImportLot.id)):
        lots.add(dict(id=lot.id, bond=lot.bond_label, item=lot.item.code, desc=line.description, be=inv.be_no,
                      be_date=inv.be_date or inv.invoice_date, inv=f"{inv.invoice_no}/{line.line_no}",
                      q_in=lot.qty_received, q_used=lot.qty_received - lot.qty_available, d_in=lot.dsv_total,
                      d_used=lot.dsv_total - lot.dsv_available, a_in=lot.av_total, a_used=lot.av_total - lot.av_available),
                 dict(q_av="{q_in}{r}-{q_used}{r}", d_av="{d_in}{r}-{d_used}{r}", a_av="{a_in}{r}-{a_used}{r}",
                      key='{bond}{r}&"|"&{item}{r}'))
    fgs = _Sheet(wb, "Inputs in FG", [("batch", "Batch", None, False), ("model", "Model", None, False),
                                      ("bond", "Bond", None, False), ("item", "Input item", None, False),
                                      ("qty", "Qty in FG stock", QTY, False), ("dsv", "Duty saved in FG stock", MONEY, False),
                                      ("key", "Key (Bond|Item)", None, True)], dict(bond=18, key=26))
    for c in session.scalars(select(ConsumptionAllocation).where(ConsumptionAllocation.qty_remaining > 0)
                             .order_by(ConsumptionAllocation.id)):
        fgs.add(dict(batch=c.batch.batch_no, model=c.batch.model.code, bond=c.bond_label, item=c.item.code,
                     qty=c.qty_remaining, dsv=c.dsv_remaining), dict(key='{bond}{r}&"|"&{item}{r}'))
    stock = _Sheet(wb, "Stock by bond", [("bond", "Bond", None, False), ("item", "Item", None, False),
                                         ("desc", "Description", None, False), ("key", "Key (Bond|Item)", None, True),
                                         ("rm_qty", "RM qty", QTY, True), ("rm_av", "RM assessable value", MONEY, True),
                                         ("rm_dsv", "RM duty saved", MONEY, True), ("fg_qty", "Qty inside FG", QTY, True),
                                         ("fg_dsv", "Duty saved inside FG", MONEY, True),
                                         ("tot", "Total duty saved", MONEY, True)], dict(bond=18, desc=40, key=26))
    combos = {}
    for lot in session.scalars(select(ImportLot)):
        combos[(lot.bond_label, lot.item.code)] = lot.item.description
    for c in session.scalars(select(ConsumptionAllocation).where(ConsumptionAllocation.qty_remaining > 0)):
        combos[(c.bond_label, c.item.code)] = c.item.description
    first_stock = stock.row + 1
    for (b, i), desc in sorted(combos.items()):
        stock.add(dict(bond=b, item=i, desc=desc), dict(
            key='{bond}{r}&"|"&{item}{r}', rm_qty=f"SUMIF({lots.ref('key')},{{key}}{{r}},{lots.ref('q_av')})",
            rm_av=f"SUMIF({lots.ref('key')},{{key}}{{r}},{lots.ref('a_av')})",
            rm_dsv=f"SUMIF({lots.ref('key')},{{key}}{{r}},{lots.ref('d_av')})",
            fg_qty=f"SUMIF({fgs.ref('key')},{{key}}{{r}},{fgs.ref('qty')})",
            fg_dsv=f"SUMIF({fgs.ref('key')},{{key}}{{r}},{fgs.ref('dsv')})", tot="{rm_dsv}{r}+{fg_dsv}{r}"))
    last_stock = stock.row
    if last_stock >= first_stock:
        stock.add(dict(bond="Total"), {k: f"SUM({{{k}}}{first_stock}:{{{k}}}{last_stock})"
                                       for k in ("rm_av", "rm_dsv", "fg_dsv", "tot")}, fill=TOTAL)

    # ---------------- DTA inputs / lines
    din = _Sheet(wb, "DTA inputs", [("clr", "Clearance", None, False), ("line", "Line", None, False),
                                    ("key", "Key (Clearance|Line)", None, True), ("item", "Input", None, False),
                                    ("ref", "Import inv/line", None, False), ("bond", "Bond", None, False),
                                    ("qty", "Qty", QTY, False), ("av", "Assessable value", MONEY, False),
                                    ("bcd_rate", "BCD rate %", "0.00", False), ("bcd", "BCD", MONEY, True),
                                    ("sws_rate", "SWS rate % of BCD", "0.00", False), ("sws", "SWS", MONEY, True),
                                    ("dsv", "Duty saved (bond)", MONEY, False)], dict(key=20, bond=18))
    dln = _Sheet(wb, "DTA lines", [("clr", "Clearance", None, False), ("date", "Date", DATE, False),
                                   ("line", "Line", None, False), ("key", "Key (Clearance|Line)", None, True),
                                   ("goods", "Goods", None, False), ("status", "Status", None, False),
                                   ("av", "Assessable value", MONEY, True), ("bcd", "BCD", MONEY, True),
                                   ("sws", "SWS", MONEY, True), ("duty", "Duty payable", MONEY, True),
                                   ("tracker", "Duty per tracker", MONEY, False), ("check", "Check", None, True),
                                   ("inputs", "Inputs", None, False)], dict(key=20, goods=36))
    rnd = (lambda e: f"ROUND({e},0)") if rules.round_duty_to_rupee else (lambda e: e)
    for clr in session.scalars(select(DTAClearance).order_by(DTAClearance.clearance_date, DTAClearance.id)):
        for line in clr.lines:
            first_in = din.row + 1
            for a in session.scalars(select(DisposalAllocation).where(DisposalAllocation.dta_line_id == line.id)
                                     .order_by(DisposalAllocation.id)):
                din.add(dict(clr=clr.clearance_no, line=line.line_no, item=a.item.code, ref=a.import_lot.source_ref,
                             bond=a.bond_label, qty=a.qty, av=a.av, bcd_rate=a.bcd_rate, sws_rate=a.sws_rate,
                             dsv=a.dsv),
                        dict(key='{clr}{r}&"|"&{line}{r}', bcd="ROUND({av}{r}*{bcd_rate}{r}/100,2)",
                             sws="ROUND({bcd}{r}*{sws_rate}{r}/100,2)"))
            dln.add(dict(clr=clr.clearance_no, date=clr.clearance_date, line=line.line_no, goods=line.description,
                         status=clr.status, tracker=line.duty, inputs="inputs →"),
                    dict(key='{clr}{r}&"|"&{line}{r}',
                         av=f"SUMIF({din.ref('key')},{{key}}{{r}},{din.ref('av')})",
                         bcd=rnd(f"SUMIF({din.ref('key')},{{key}}{{r}},{din.ref('bcd')})"),
                         sws=rnd(f"SUMIF({din.ref('key')},{{key}}{{r}},{din.ref('sws')})"),
                         duty="{bcd}{r}+{sws}{r}", check='IF(ABS({duty}{r}-{tracker}{r})<0.02,"✓","✗ differs")'),
                    links=dict(inputs=("DTA inputs", first_in)))
    if dln.row > dln.head:
        a, b = dln.head + 1, dln.row
        S, J = dln.col("status"), dln.col("duty")
        dln.add(dict(clr="Pending duty (status PENDING)"),
                dict(duty=f'SUMIF({S}{a}:{S}{b},"PENDING",{J}{a}:{J}{b})'), fill=TOTAL)

    # ---------------- Monthly summary
    mon = _Sheet(wb, "Monthly summary", [("bond", "Bond", None, False), ("month", "Month", None, False),
                                         ("key", "Key (Bond|Month)", None, True), ("open", "Opening", MONEY, True),
                                         ("debit", "Debits (imports)", MONEY, True),
                                         ("credit", "Credits (exports / DTA)", MONEY, True),
                                         ("memo", "Consumed into FG (memo)", MONEY, True),
                                         ("close", "Closing", MONEY, True), ("go", "Ledger", None, False)],
                 dict(bond=18, key=26))
    for (b, m), r in sorted(month_first.items()):
        mon.add(dict(bond=b, month=m, go="entries →"),
                dict(key='{bond}{r}&"|"&{month}{r}',
                     open="IF({bond}{r}<>{bond}{p},VLOOKUP({bond}{r},Bonds!$A:$B,2,0),{close}{p})",
                     debit=f"SUMIF({led.ref('key')},{{key}}{{r}},{led.ref('debit')})",
                     credit=f"SUMIF({led.ref('key')},{{key}}{{r}},{led.ref('credit')})",
                     memo=f"SUMIF({led.ref('key')},{{key}}{{r}},{led.ref('memo')})",
                     close="{open}{r}-{debit}{r}+{credit}{r}"),
                links=dict(go=("Ledger", r)))

    # ---------------- Bonds (first data sheet after Read me)
    pending_dta = {}  # duty saved on DTA lines not yet credited to the bond
    for a, clr in session.execute(select(DisposalAllocation, DTAClearance).join(
            DTALine, DisposalAllocation.dta_line_id == DTALine.id).join(DTAClearance)
            .where(DTAClearance.bond_recredited.is_(False), DisposalAllocation.bond_id.is_not(None))):
        pending_dta[a.bond_id] = pending_dta.get(a.bond_id, ZERO) + a.dsv
    not_received = {}
    for line, inv in session.execute(select(ImportLine, ImportInvoice).join(ImportInvoice)
                                     .where(ImportInvoice.status == "POSTED", ImportInvoice.import_type == "BOND")):
        lot = session.scalar(select(ImportLot).where(ImportLot.import_line_id == line.id))
        short = line.duty_saved_value - (lot.dsv_total if lot else ZERO)
        if short:
            not_received[inv.bond_id] = not_received.get(inv.bond_id, ZERO) + short
    bs = _Sheet(wb, "Bonds", [("bond", "Bond No", None, False), ("amount", "Bond amount", MONEY, False),
                              ("from", "Valid from", DATE, False), ("to", "Valid to", DATE, False),
                              ("debits", "Total debits", MONEY, True), ("credits", "Total credits", MONEY, True),
                              ("balance", "Balance", MONEY, True), ("tracker", "Balance per tracker", MONEY, False),
                              ("check", "Check", None, True), ("utilised", "Utilised (amount - balance)", MONEY, True),
                              ("stock", "Duty saved in stock (RM + FG)", MONEY, True),
                              ("dta", "DTA awaiting recredit", MONEY, False), ("short", "Goods not received", MONEY, False),
                              ("diff", "Unexplained", MONEY, True), ("go", "Ledger", None, False)],
                dict(bond=18, utilised=16, stock=18))
    for b in bonds:
        bs.add(dict(bond=b.bond_no, amount=b.bond_amount, **{"from": b.valid_from}, to=b.valid_to, tracker=b.balance,
                    dta=pending_dta.get(b.id, ZERO), short=not_received.get(b.id, ZERO), go="entries →"),
               dict(debits=f"SUMIF({led.ref('bond')},{{bond}}{{r}},{led.ref('debit')})",
                    credits=f"SUMIF({led.ref('bond')},{{bond}}{{r}},{led.ref('credit')})",
                    balance="{amount}{r}-{debits}{r}+{credits}{r}",
                    check='IF(ABS({balance}{r}-{tracker}{r})<0.01,"✓ agrees","✗ differs")',
                    utilised="{amount}{r}-{balance}{r}",
                    stock=f"SUMIF({stock.ref('bond')},{{bond}}{{r}},{stock.ref('tot')})",
                    diff="{utilised}{r}-{stock}{r}-{dta}{r}-{short}{r}"),
               links=dict(go=("Ledger", first_led_row[b.bond_no]) if b.bond_no in first_led_row else None))
    order = ["Read me", "Bonds", "Monthly summary", "Ledger", "Stock by bond", "Lots", "Inputs in FG", "DTA lines",
             "DTA inputs"]
    wb._sheets = [wb[n] for n in order]
    return _bytes(wb)


# =================================================================== Form III
def form3_workbook(form) -> bytes:
    from .services import form3 as f3

    wb = Workbook()
    H = f3.DETAILS_HEADERS
    _readme(wb, f"Form III working - {f3.quarter_label(form.end)}", [
        ("Details of Goods", "Same 29 columns as the filing file, with the arithmetic as formulas: "
                             "M = I+J+K+L; R = N-Q; S/T/U/V = SUMIF over 'Movements' for this BE item and column; "
                             "Y = U+V+W+X; Z = Q-S-T-Y; AC = Z+AA+AB. Column AD is the key (BE No|Invoice S.No|Item S.No), "
                             "column AE links to the movements of the row."),
        ("Movements", "Every export / DTA movement (traced FIFO to the BE item) up to the quarter end, with the Form III "
                      "column it counts in: S cleared on payment, T re-exported, U used for goods supplied in India, "
                      "V used for goods exported."),
        ("", "File the separate .xls download (values only) - this working file is for review."),
    ])
    mv = _Sheet(wb, "Movements", [("key", "Key (BE|Inv|Item)", None, False), ("date", "Date", DATE, False),
                                  ("doc", "Document", None, False), ("ref", "Reference", None, False),
                                  ("lt", "Line type", None, False), ("col", "Form III column", None, False),
                                  ("qty", "Qty", QTY, False), ("note", "Note", None, False),
                                  ("k2", "Key|Column", None, True), ("back", "Row", None, False)],
                dict(key=22, doc=14, ref=18, note=40, k2=26))
    first_mv = {}
    det_row = {k: i + 3 for i, k in enumerate(form.keys)}  # header on row 2
    for m in form.movements:
        r = mv.add(dict(key=m["key"], date=m["date"], doc=m["document"], ref=m["ref"], lt=m["line_type"],
                        col=m["column"], qty=m["qty"], note=m.get("note"), back="← row"),
                   dict(k2='{key}{r}&"|"&{col}{r}'), links=dict(back=("Details of Goods", det_row.get(m["key"], 3))))
        first_mv.setdefault(m["key"], r)
    cols = [(f"c{i}", h, None, i in (12, 17, 18, 19, 20, 21, 24, 25, 28)) for i, h in enumerate(H)]
    cols += [("key", "Key (BE|Inv|Item)", None, True), ("go", "Movements", None, False)]
    det = _Sheet(wb, "Details of Goods", cols, note="Formulas in green. File the values-only .xls, not this sheet.")
    L = det.L
    for row, key in zip(form.sheets["Details of Goods"], form.keys):
        vals = {f"c{i}": v for i, v in enumerate(row)}
        vals["go"] = "movements →" if key in first_mv else ""
        sumif = lambda c: f'SUMIF(Movements!$I:$I,{L["key"]}{{r}}&"|{c}",Movements!$G:$G)'  # noqa: E731
        det.add(vals, {
            "c12": f"{L['c8']}{{r}}+{L['c9']}{{r}}+{L['c10']}{{r}}+{L['c11']}{{r}}",
            "c17": f"{L['c13']}{{r}}-{L['c16']}{{r}}",
            "c18": sumif("S"), "c19": sumif("T"), "c20": sumif("U"), "c21": sumif("V"),
            "c24": f"{L['c20']}{{r}}+{L['c21']}{{r}}+{L['c22']}{{r}}+{L['c23']}{{r}}",
            "c25": f"{L['c16']}{{r}}-{L['c18']}{{r}}-{L['c19']}{{r}}-{L['c24']}{{r}}",
            "c28": f"{L['c25']}{{r}}+{L['c26']}{{r}}+{L['c27']}{{r}}",
            "key": f'{L["c0"]}{{r}}&"|"&{L["c3"]}{{r}}&"|"&{L["c4"]}{{r}}',
        }, links=dict(go=("Movements", first_mv[key]) if key in first_mv else None))
        det.ws.cell(row=det.row, column=16).number_format = "d-mmm-yy"
    wb._sheets = [wb["Read me"], wb["Details of Goods"], wb["Movements"]]
    return _bytes(wb)
