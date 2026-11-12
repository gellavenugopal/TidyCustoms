"""Figures for the dashboard: KPIs, bond utilisation, monthly debits vs credits,
duty saved in stock by item, stock composition, alerts and recent activity."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Bond, BondLedgerEntry, DTAClearance, ImportInvoice, ImportLine, ImportLot)
from ..numbers import ZERO
from . import reports


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def _months_back(today: date, n: int) -> list[str]:
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def build(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    bonds = list(session.scalars(select(Bond).order_by(Bond.status, Bond.valid_to)))
    active = [b for b in bonds if b.status == "ACTIVE"]
    amount = sum((b.bond_amount for b in active), ZERO)
    balance = sum((b.balance for b in active), ZERO)
    bond_rows = []
    for b in bonds:
        used = b.bond_amount - b.balance
        bond_rows.append(dict(bond=b, used=used, pct=float(used / b.bond_amount) if b.bond_amount else 0.0,
                              days_left=(b.valid_to - today).days))

    # monthly debits / credits, all bonds, last 12 months
    months = _months_back(today, 12)
    agg = {m: dict(debit=ZERO, credit=ZERO) for m in months}
    since = date(int(months[0][:4]), int(months[0][5:]), 1)
    for e in session.scalars(select(BondLedgerEntry).where(BondLedgerEntry.entry_date >= since)):
        key = e.entry_date.strftime("%Y-%m")
        if key in agg:
            agg[key]["debit"] += e.debit
            agg[key]["credit"] += e.credit

    # stock
    stock = reports.stock_by_bond(session).rows
    by_item: dict[str, dict] = {}
    rm_dsv = fg_dsv = ZERO
    for r in stock:
        if r["bond"] == "Duty paid (no bond)":
            continue
        d = by_item.setdefault(r["item"], dict(item=r["item"], description=r["description"], dsv=ZERO))
        d["dsv"] += r["total_dsv"]
        rm_dsv += r["rm_dsv"]
        fg_dsv += r["fg_dsv"]
    top_items = sorted((d for d in by_item.values() if d["dsv"] > 0), key=lambda d: -d["dsv"])[:8]
    paid_av = session.scalar(select(func.coalesce(func.sum(ImportLot.av_available), 0))
                             .where(ImportLot.duty_paid.is_(True))) or 0

    pending = list(session.scalars(select(DTAClearance).where(DTAClearance.status == "PENDING")
                                   .order_by(DTAClearance.clearance_date)))
    fy = fy_start(today)
    fy_debit = session.scalar(select(func.coalesce(func.sum(BondLedgerEntry.debit), 0))
                              .where(BondLedgerEntry.entry_date >= fy)) or 0
    fy_credit = session.scalar(select(func.coalesce(func.sum(BondLedgerEntry.credit), 0))
                               .where(BondLedgerEntry.entry_date >= fy)) or 0
    drafts = list(session.scalars(select(ImportInvoice).where(ImportInvoice.status == "DRAFT")
                                  .order_by(ImportInvoice.invoice_date)))
    unmapped = session.scalar(select(func.count(ImportLine.id)).join(ImportInvoice).where(
        ImportInvoice.status == "DRAFT", ImportLine.item_id.is_(None))) or 0

    alerts = []
    for r in bond_rows:
        b = r["bond"]
        if b.status != "ACTIVE":
            continue
        if r["days_left"] < 0:
            alerts.append(("crit", "!", f"Bond {b.bond_no} expired on {b.valid_to:%d-%m-%Y}"))
        elif r["days_left"] <= 60:
            alerts.append(("warn", "!", f"Bond {b.bond_no} expires in {r['days_left']} days ({b.valid_to:%d-%m-%Y})"))
        if r["pct"] >= 0.9:
            alerts.append(("crit", "!", f"Bond {b.bond_no} is {r['pct']:.0%} utilised"))
        elif r["pct"] >= 0.75:
            alerts.append(("warn", "!", f"Bond {b.bond_no} is {r['pct']:.0%} utilised"))
    if unmapped:
        alerts.append(("warn", "?", f"{unmapped} draft import line(s) not mapped to a Part No"))
    if pending:
        alerts.append(("warn", "₹", f"{len(pending)} DTA clearance(s) awaiting duty payment"))
    if not alerts:
        alerts.append(("ok", "✓", "Nothing needs attention"))

    recent = list(session.scalars(select(BondLedgerEntry).order_by(BondLedgerEntry.entry_date.desc(),
                                                                  BondLedgerEntry.id.desc()).limit(8)))
    return dict(
        amount=amount, balance=balance, used=amount - balance,
        util=float((amount - balance) / amount) if amount else 0.0,
        stock_dsv=rm_dsv + fg_dsv, rm_dsv=rm_dsv, fg_dsv=fg_dsv, paid_av=Decimal(str(paid_av)),
        pending=pending, pending_duty=sum((c.total_duty for c in pending), ZERO),
        fy=fy, fy_debit=Decimal(str(fy_debit)), fy_credit=Decimal(str(fy_credit)),
        bonds=bond_rows, months=months, monthly=agg, top_items=top_items,
        drafts=drafts, unmapped=unmapped, alerts=alerts, recent=recent)
