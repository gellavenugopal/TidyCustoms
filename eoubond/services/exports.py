"""Export shipments: draw stock FIFO and recredit each bond by the duty-saved
value of the imported materials that left the country."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import DisposalAllocation, ExportLine, ExportShipment
from ..numbers import ZERO, money
from . import bonds
from .disposal import clean_lines, draw_for_line, precheck
from .errors import ValidationError
from .stock import bond_totals


def post_export(session: Session, *, invoice_no: str, export_date: date, lines: list[dict],
                shipping_bill_no: str | None = None, shipping_bill_date: date | None = None,
                consignee: str | None = None, country: str | None = None,
                port_of_export: str | None = None) -> ExportShipment:
    """lines: [{line_type: FG|ITEM, model_id | item_id, qty, export_item_name?, fob_value?}]"""
    invoice_no = (invoice_no or "").strip()
    if not invoice_no:
        raise ValidationError("Export invoice number is required")
    if session.scalar(select(ExportShipment).where(ExportShipment.invoice_no == invoice_no)):
        raise ValidationError(f"Export invoice {invoice_no} already exists")
    lines = clean_lines(session, lines)
    precheck(session, lines, export_date)

    sh = ExportShipment(invoice_no=invoice_no, export_date=export_date, shipping_bill_no=shipping_bill_no or None,
                        shipping_bill_date=shipping_bill_date, consignee=consignee or None,
                        country=country or None,
                        port_of_export=(port_of_export or "").strip().upper()
                        or get_rules().form3_default_port_of_export or None)
    session.add(sh)
    session.flush()
    all_takes = []
    for n, ln in enumerate(lines, start=1):
        el = ExportLine(shipment_id=sh.id, line_no=n, line_type=ln["line_type"], model_id=ln["model_id"],
                        item_id=ln["item_id"], qty=ln["qty"],
                        export_item_name=(ln.get("export_item_name") or "").strip() or ln["_label"],
                        fob_value=money(ln.get("fob_value")),
                        sb_invoice_serial=ln.get("sb_invoice_serial"), sb_item_serial=ln.get("sb_item_serial"),
                        hs_code=ln.get("hs_code"))
        session.add(el)
        session.flush()
        takes = draw_for_line(session, ln, export_date)
        for t in takes:
            session.add(DisposalAllocation(
                export_line_id=el.id, fg_lot_id=t.fg_lot.id if t.fg_lot else None, fg_qty=t.fg_qty,
                consumption_id=t.consumption.id if t.consumption else None, import_lot_id=t.lot.id,
                bond_id=t.lot.bond_id, item_id=t.lot.item_id, qty=t.qty, dsv=t.dsv, av=t.av))
        el.dsv_recredited = money(sum((t.dsv for t in takes), ZERO))
        all_takes.extend(takes)
    session.flush()

    for b_id, value in sorted(bond_totals(all_takes).items()):
        if value > 0:
            bonds.credit(session, b_id, value, entry_date=export_date, entry_type="EXPORT_CREDIT",
                         source_type="EXPORT", source_id=sh.id, source_ref=sh.invoice_no,
                         narration=f"Export" + (f", SB {sh.shipping_bill_no}" if sh.shipping_bill_no else ""))
    sh.total_recredit = money(sum((t.dsv for t in all_takes), ZERO))
    session.flush()
    return sh
