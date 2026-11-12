"""DTA (domestic) clearance of duty-saved goods.

Payable customs duty = BCD + SWS only, computed on the assessable value of the
duty-saved imported inputs actually consumed (traced FIFO):
    BCD = AV x BCD rate %            (item rate, else config default)
    SWS = BCD x SWS rate %           (item rate, else config default)
GST/IGST is deliberately excluded - it is charged on the domestic supply
invoice in the normal way and is not part of this computation.

ASSUMPTIONS (README section 5): A-DTA-1..6 - default rates, SWS on BCD,
per-line rupee rounding, duty on inputs' AV at clearance-date item rates
(snapshotted per trace row), no GST/interest/ADD; A-BOND-3 recredit timing.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import DisposalAllocation, DTAClearance, DTALine, Item
from ..numbers import ZERO, money, rupee
from . import bonds
from .disposal import clean_lines, draw_for_line, precheck
from .errors import BusinessError, ValidationError


def rates_for(item: Item) -> tuple[Decimal, Decimal]:
    rules = get_rules()
    bcd = item.bcd_rate if item.bcd_rate is not None else rules.default_bcd_rate
    sws = item.sws_rate if item.sws_rate is not None else rules.sws_rate_on_bcd
    return Decimal(bcd), Decimal(sws)


def compute_duty(av, bcd_rate, sws_rate) -> tuple[Decimal, Decimal]:
    """BCD on assessable value, SWS on BCD. Returned to the paisa (unrounded)."""
    bcd = money(Decimal(av) * Decimal(bcd_rate) / 100)
    sws = money(bcd * Decimal(sws_rate) / 100)
    return bcd, sws


def post_dta(session: Session, *, clearance_no: str, clearance_date: date, lines: list[dict],
             customer: str | None = None, be_no: str | None = None) -> DTAClearance:
    """lines: [{line_type: FG|ITEM, model_id | item_id, qty, description?, sale_value?}]"""
    clearance_no = (clearance_no or "").strip()
    if not clearance_no:
        raise ValidationError("Clearance number is required")
    if session.scalar(select(DTAClearance).where(DTAClearance.clearance_no == clearance_no)):
        raise ValidationError(f"Clearance {clearance_no} already exists")
    lines = clean_lines(session, lines)
    precheck(session, lines, clearance_date)
    rules = get_rules()

    clr = DTAClearance(clearance_no=clearance_no, clearance_date=clearance_date, customer=customer or None,
                       be_no=be_no or None, status="PENDING")
    session.add(clr)
    session.flush()
    for n, ln in enumerate(lines, start=1):
        dl = DTALine(clearance_id=clr.id, line_no=n, line_type=ln["line_type"], model_id=ln["model_id"],
                     item_id=ln["item_id"], qty=ln["qty"],
                     description=(ln.get("description") or "").strip() or ln["_label"],
                     sale_value=money(ln.get("sale_value")))
        session.add(dl)
        session.flush()
        bcd_sum = sws_sum = av_sum = dsv_sum = ZERO
        for t in draw_for_line(session, ln, clearance_date):
            if t.lot.duty_paid:  # duty already paid at import - nothing more is payable
                bcd_rate = sws_rate = Decimal("0")
            else:
                bcd_rate, sws_rate = rates_for(t.lot.item)
            bcd, sws = compute_duty(t.av, bcd_rate, sws_rate)
            session.add(DisposalAllocation(
                dta_line_id=dl.id, fg_lot_id=t.fg_lot.id if t.fg_lot else None, fg_qty=t.fg_qty,
                consumption_id=t.consumption.id if t.consumption else None, import_lot_id=t.lot.id,
                bond_id=t.lot.bond_id, item_id=t.lot.item_id, qty=t.qty, dsv=t.dsv, av=t.av,
                bcd_rate=bcd_rate, sws_rate=sws_rate, bcd=bcd, sws=sws))
            bcd_sum += bcd
            sws_sum += sws
            av_sum += t.av
            dsv_sum += t.dsv
        if rules.round_duty_to_rupee:
            bcd_sum, sws_sum = rupee(bcd_sum), rupee(sws_sum)
        dl.assessable_value, dl.dsv = money(av_sum), money(dsv_sum)
        dl.bcd, dl.sws = money(bcd_sum), money(sws_sum)
        dl.duty = money(bcd_sum + sws_sum)
    session.flush()
    clr.total_av = money(sum(l.assessable_value for l in clr.lines))
    clr.total_bcd = money(sum(l.bcd for l in clr.lines))
    clr.total_sws = money(sum(l.sws for l in clr.lines))
    clr.total_duty = money(sum(l.duty for l in clr.lines))
    clr.total_dsv = money(sum(l.dsv for l in clr.lines))
    session.flush()
    if rules.dta_recredit_on == "clearance":
        _recredit(session, clr, clearance_date)
    return clr


def _recredit(session: Session, clr: DTAClearance, on: date) -> None:
    totals: dict[int, Decimal] = {}
    for a in session.scalars(select(DisposalAllocation).join(DTALine).where(DTALine.clearance_id == clr.id)):
        if a.bond_id is not None:  # duty-paid inputs were never debited to a bond
            totals[a.bond_id] = totals.get(a.bond_id, ZERO) + a.dsv
    for b_id, value in sorted(totals.items()):
        if value > 0:
            bonds.credit(session, b_id, value, entry_date=on, entry_type="DTA_CREDIT", source_type="DTA",
                         source_id=clr.id, source_ref=clr.clearance_no,
                         narration=f"DTA clearance, duty Rs {clr.total_duty}"
                                   + (f", challan {clr.challan_no}" if clr.challan_no else ""))
    clr.bond_recredited = True
    session.flush()


def mark_paid(session: Session, clearance_id: int, *, challan_no: str, paid_date: date) -> DTAClearance:
    clr = session.get(DTAClearance, clearance_id)
    if not clr:
        raise ValidationError("Clearance not found")
    if clr.status == "PAID":
        raise BusinessError("Duty on this clearance is already marked paid")
    if not (challan_no or "").strip():
        raise ValidationError("Challan / payment reference is required")
    clr.status, clr.challan_no, clr.paid_date = "PAID", challan_no.strip(), paid_date
    session.flush()
    if get_rules().dta_recredit_on == "payment" and not clr.bond_recredited:
        _recredit(session, clr, paid_date)
    return clr


def computation_rows(session: Session, clr: DTAClearance) -> list[dict]:
    """Component-level trace for the payment computation sheet."""
    rows = []
    for line in clr.lines:
        for a in session.scalars(select(DisposalAllocation).where(DisposalAllocation.dta_line_id == line.id)
                                 .order_by(DisposalAllocation.id)):
            lot = a.import_lot
            rows.append(dict(line_no=line.line_no, line=line.description, item=a.item.code,
                             import_ref=lot.source_ref, bond=a.bond_label, qty=a.qty, av=a.av,
                             bcd_rate=a.bcd_rate, bcd=a.bcd, sws_rate=a.sws_rate, sws=a.sws,
                             duty=money((a.bcd or 0) + (a.sws or 0)), dsv=a.dsv))
    return rows
