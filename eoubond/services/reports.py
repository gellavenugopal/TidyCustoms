"""Reports. Each returns a Report (title, columns, rows, totals) that the web
layer renders as an HTML table and the Excel exporter writes as a sheet."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (AuditLog, Bond, BondLedgerEntry, ConsumptionAllocation, DisposalAllocation, DTAClearance,
                      ExportLine, ExportShipment, FGLot, ImportInvoice, ImportLine, ImportLot, Item, ProductionBatch)
from ..numbers import ZERO, money, qty


@dataclass
class Column:
    key: str
    label: str
    kind: str = "text"  # text | money | qty | date | rate


@dataclass
class Report:
    title: str
    columns: list[Column]
    rows: list[dict]
    subtitle: str = ""
    totals: dict = field(default_factory=dict)
    sheet: str = ""


def _sum(rows, *keys):
    return {k: sum((r[k] or ZERO for r in rows), ZERO) for k in keys}


# ------------------------------------------------------------------ stock by bond
def stock_by_bond(session: Session, bond_id: int | None = None) -> Report:
    """Duty-saved stock still under bond: raw material in import lots, plus inputs
    already built into finished goods that are still in FG stock."""
    rows: dict[tuple, dict] = {}

    def row(bond, item):
        label = bond.bond_no if bond else "Duty paid (no bond)"
        key = (label, item.code)
        return rows.setdefault(key, dict(bond=label, item=item.code, description=item.description,
                                         uom=item.uom, rm_qty=ZERO, rm_av=ZERO, rm_dsv=ZERO,
                                         fg_qty=ZERO, fg_dsv=ZERO, total_dsv=ZERO))

    lots = select(ImportLot).where(ImportLot.qty_available > 0)
    cons = select(ConsumptionAllocation).where(ConsumptionAllocation.qty_remaining > 0)
    if bond_id:
        lots = lots.where(ImportLot.bond_id == bond_id)
        cons = cons.where(ConsumptionAllocation.bond_id == bond_id)
    for lot in session.scalars(lots):
        r = row(lot.bond, lot.item)
        r["rm_qty"] += lot.qty_available
        r["rm_av"] += lot.av_available
        r["rm_dsv"] += lot.dsv_available
    for c in session.scalars(cons):
        r = row(c.bond, c.item)
        r["fg_qty"] += c.qty_remaining
        r["fg_dsv"] += c.dsv_remaining
    out = sorted(rows.values(), key=lambda r: (r["bond"], r["item"]))
    for r in out:
        r["total_dsv"] = r["rm_dsv"] + r["fg_dsv"]
    cols = [Column("bond", "Bond"), Column("item", "Item code"), Column("description", "Description"),
            Column("uom", "UOM"), Column("rm_qty", "RM qty", "qty"), Column("rm_av", "RM assessable value", "money"),
            Column("rm_dsv", "RM duty saved", "money"), Column("fg_qty", "Qty inside FG stock", "qty"),
            Column("fg_dsv", "Duty saved inside FG", "money"), Column("total_dsv", "Total duty saved", "money")]
    return Report("Stock by bond", cols, out, subtitle=f"As on {date.today():%d-%m-%Y}",
                  totals=_sum(out, "rm_av", "rm_dsv", "fg_dsv", "total_dsv"), sheet="Stock by bond")


def fg_stock(session: Session) -> Report:
    out = []
    for lot in session.scalars(select(FGLot).where(FGLot.qty_available > 0).order_by(FGLot.lot_date, FGLot.id)):
        dsv = sum((c.dsv_remaining for c in session.scalars(
            select(ConsumptionAllocation).where(ConsumptionAllocation.fg_lot_id == lot.id))), ZERO)
        out.append(dict(model=lot.model.code, batch=lot.batch.batch_no, date=lot.lot_date,
                        produced=lot.qty_produced, available=lot.qty_available, dsv=dsv))
    cols = [Column("model", "Model"), Column("batch", "Batch"), Column("date", "Produced", "date"),
            Column("produced", "Qty produced", "qty"), Column("available", "Qty in stock", "qty"),
            Column("dsv", "Duty saved in stock", "money")]
    return Report("Finished goods stock", cols, out, totals=_sum(out, "dsv"), sheet="FG stock")


# ------------------------------------------------------------------ ledger
def _ordered_entries(session, bond_id, date_to=None):
    stmt = select(BondLedgerEntry).where(BondLedgerEntry.bond_id == bond_id)
    if date_to:
        stmt = stmt.where(BondLedgerEntry.entry_date <= date_to)
    return list(session.scalars(stmt.order_by(BondLedgerEntry.entry_date, BondLedgerEntry.id)))


def bond_ledger(session: Session, bond_id: int, date_from: date | None = None,
                date_to: date | None = None) -> Report:
    """Running balance in date order, starting from the bond amount."""
    bond = session.get(Bond, bond_id)
    running = bond.bond_amount
    opening = running
    out = []
    for e in _ordered_entries(session, bond_id, date_to):
        running = money(running - e.debit + e.credit)
        if date_from and e.entry_date < date_from:
            opening = running
            continue
        out.append(dict(date=e.entry_date, type=e.entry_type, source=e.source_type, ref=e.source_ref,
                        narration=e.narration or "", debit=e.debit, credit=e.credit,
                        memo=e.memo_value, balance=running, user=e.created_by))
    cols = [Column("date", "Date", "date"), Column("type", "Entry"), Column("source", "Source"),
            Column("ref", "Reference"), Column("narration", "Narration"), Column("debit", "Debit", "money"),
            Column("credit", "Credit", "money"), Column("memo", "Memo value", "money"),
            Column("balance", "Balance", "money"), Column("user", "By")]
    sub = (f"Bond amount {bond.bond_amount:,.2f} | validity {bond.valid_from:%d-%m-%Y} to "
           f"{bond.valid_to:%d-%m-%Y} | opening {opening:,.2f} | closing {running:,.2f}")
    return Report(f"Bond ledger - {bond.bond_no}", cols, out, subtitle=sub,
                  totals=_sum(out, "debit", "credit", "memo"), sheet=f"Ledger {bond.bond_no}"[:31])


def monthly_summary(session: Session, bond_id: int | None = None) -> Report:
    bonds = [session.get(Bond, bond_id)] if bond_id else list(session.scalars(select(Bond).order_by(Bond.bond_no)))
    out = []
    for bond in bonds:
        balance = bond.bond_amount
        months: dict[str, dict] = {}
        for e in _ordered_entries(session, bond.id):
            key = e.entry_date.strftime("%Y-%m")
            m = months.setdefault(key, dict(bond=bond.bond_no, month=key, opening=balance,
                                            debits=ZERO, credits=ZERO, memo=ZERO, closing=balance))
            m["debits"] += e.debit
            m["credits"] += e.credit
            m["memo"] += e.memo_value
            balance = money(balance - e.debit + e.credit)
            m["closing"] = balance
        out.extend(months[k] for k in sorted(months))
    cols = [Column("bond", "Bond"), Column("month", "Month"), Column("opening", "Opening", "money"),
            Column("debits", "Debits (imports)", "money"), Column("credits", "Credits (exports/DTA)", "money"),
            Column("memo", "Consumed into FG (memo)", "money"), Column("closing", "Closing balance", "money")]
    return Report("Monthly bond summary", cols, out, totals=_sum(out, "debits", "credits", "memo"),
                  sheet="Monthly summary")


# ------------------------------------------------------------------ DTA
def pending_dta_duty(session: Session) -> Report:
    out = [dict(no=c.clearance_no, date=c.clearance_date, customer=c.customer or "", av=c.total_av,
                bcd=c.total_bcd, sws=c.total_sws, duty=c.total_duty, dsv=c.total_dsv,
                days=(date.today() - c.clearance_date).days)
           for c in session.scalars(select(DTAClearance).where(DTAClearance.status == "PENDING")
                                    .order_by(DTAClearance.clearance_date))]
    cols = [Column("no", "Clearance"), Column("date", "Date", "date"), Column("customer", "Customer"),
            Column("av", "Assessable value", "money"), Column("bcd", "BCD", "money"), Column("sws", "SWS", "money"),
            Column("duty", "Duty payable", "money"), Column("dsv", "Duty saved (bond)", "money"),
            Column("days", "Days pending")]
    return Report("Pending duty on DTA clearances", cols, out,
                  subtitle="BCD + SWS only. GST is charged separately on the domestic supply invoice.",
                  totals=_sum(out, "av", "bcd", "sws", "duty", "dsv"), sheet="Pending DTA duty")


# ------------------------------------------------------------------ registers
HEADS = (("bcd", "BCD", "bcd_forgone"), ("sws", "SWS", "sws_forgone"), ("igst", "IGST", "igst_forgone"),
         ("cess", "Cess", "cess_forgone"), ("other", "Other duty", "other_forgone"))


def _heads_of(line: ImportLine, fraction) -> dict:
    """The duty heads of an import line, scaled to a fraction of its quantity."""
    if line.has_duty_split:
        vals = {k: getattr(line, attr) for k, _l, attr in HEADS}
    else:  # only a total was entered
        vals = {k: ZERO for k, *_r in HEADS}
        vals["other"] = line.duty_total
    return {k: money(v * fraction) for k, v in vals.items()}


def import_register(session: Session) -> Report:
    """Every import line with its duty, head by head. On a BOND import the heads are
    duty forgone (debited to the bond); on a DUTY PAID import they are duty paid."""
    out = []
    for inv in session.scalars(select(ImportInvoice).order_by(ImportInvoice.invoice_date, ImportInvoice.id)):
        for l in inv.lines:
            heads = _heads_of(l, 1)
            out.append(dict(date=inv.invoice_date, invoice=inv.invoice_no, supplier=inv.supplier.name,
                            type="Duty paid" if inv.duty_paid else "Under bond", bond=inv.bond_label,
                            be=inv.be_no or "", status=inv.status, line=l.line_no, description=l.description,
                            item=l.item.code if l.item else "UNMAPPED", qty=l.qty, av=l.assessable_value, **heads,
                            total=money(sum(heads.values())),
                            forgone=ZERO if inv.duty_paid else l.duty_saved_value,
                            paid=money(sum(heads.values())) if inv.duty_paid else ZERO))
    cols = [Column("date", "Date", "date"), Column("invoice", "Invoice"), Column("supplier", "Supplier"),
            Column("type", "Import type"), Column("bond", "Bond"), Column("be", "BE no"), Column("status", "Status"),
            Column("line", "Line"), Column("description", "Supplier description"), Column("item", "Item code"),
            Column("qty", "Qty", "qty"), Column("av", "Assessable value", "money"),
            *[Column(k, label, "money") for k, label, _a in HEADS], Column("total", "Total duty", "money"),
            Column("forgone", "Duty forgone (bond debit)", "money"), Column("paid", "Duty paid", "money")]
    return Report("Import register", cols, out,
                  subtitle="Duty heads are duty forgone on imports under bond and duty paid on duty-paid imports.",
                  totals=_sum(out, "av", *[k for k, *_r in HEADS], "total", "forgone", "paid"), sheet="Imports")


def export_register(session: Session) -> Report:
    """Every export line with the duty contained in the imported inputs it used
    (FIFO trace), head by head, split into duty forgone (recredited to the bond)
    and duty paid (duty-paid inputs - no recredit)."""
    out = []
    for sh in session.scalars(select(ExportShipment).order_by(ExportShipment.export_date, ExportShipment.id)):
        for l in sh.lines:
            heads = {k: ZERO for k, *_r in HEADS}
            paid = ZERO
            for a in session.scalars(select(DisposalAllocation).where(DisposalAllocation.export_line_id == l.id)):
                line = a.import_lot.import_line
                part = _heads_of(line, (a.qty / line.qty) if line.qty else 0)
                for k, v in part.items():
                    heads[k] += v
                if a.import_lot.duty_paid:
                    paid += sum(part.values())
            total = money(sum(heads.values()))
            out.append(dict(date=sh.export_date, invoice=sh.invoice_no, sb=sh.shipping_bill_no or "",
                            type=l.line_type, code=l.model.code if l.model else l.item.code,
                            name=l.export_item_name, qty=l.qty, fob=l.fob_value, **heads, total=total,
                            dsv=l.dsv_recredited, paid=money(paid)))
    cols = [Column("date", "Date", "date"), Column("invoice", "Invoice"), Column("sb", "Shipping bill"),
            Column("type", "Type"), Column("code", "Code"), Column("name", "Export item"), Column("qty", "Qty", "qty"),
            Column("fob", "FOB value", "money"), *[Column(k, label, "money") for k, label, _a in HEADS],
            Column("total", "Total duty in inputs", "money"), Column("dsv", "Bond recredit (duty forgone)", "money"),
            Column("paid", "Duty-paid inputs (no recredit)", "money")]
    return Report("Export register", cols, out,
                  subtitle="Duty heads = duty in the imported inputs of each export line, traced FIFO to the import.",
                  totals=_sum(out, "fob", *[k for k, *_r in HEADS], "total", "dsv", "paid"), sheet="Exports")


def production_register(session: Session) -> Report:
    out = [dict(date=b.production_date, batch=b.batch_no, model=b.model.code, bom=f"v{b.bom.version}",
                qty=b.qty, av=b.total_av, dsv=b.total_dsv)
           for b in session.scalars(select(ProductionBatch).order_by(ProductionBatch.production_date,
                                                                     ProductionBatch.id))]
    cols = [Column("date", "Date", "date"), Column("batch", "Batch"), Column("model", "Model"), Column("bom", "BOM"),
            Column("qty", "Qty", "qty"), Column("av", "Inputs AV", "money"), Column("dsv", "Inputs duty saved", "money")]
    return Report("Production register", cols, out, totals=_sum(out, "av", "dsv"), sheet="Production")


def audit_trail(session: Session, table: str | None = None, limit: int = 500) -> Report:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if table:
        stmt = stmt.where(AuditLog.table_name == table)
    out = [dict(ts=a.ts.strftime("%Y-%m-%d %H:%M:%S"), user=a.username, action=a.action, table=a.table_name,
                id=a.record_id, old=a.old_values or "", new=a.new_values or "")
           for a in session.scalars(stmt)]
    cols = [Column("ts", "When (UTC)"), Column("user", "Who"), Column("action", "Action"), Column("table", "Table"),
            Column("id", "Record"), Column("old", "Old values"), Column("new", "New values")]
    return Report("Audit trail", cols, out, subtitle=f"Latest {limit} changes", sheet="Audit")
