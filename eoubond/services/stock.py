"""FIFO allocation engine for duty-saved stock.

Two kinds of FIFO unit:
  * ImportLot - raw material / accessory as imported under bond;
  * FGLot     - finished detectors from one production batch. Each FG lot
                carries the ConsumptionAllocation rows that say which import
                lots (and how much duty-saved value) went into it.

FIFO order is (lot_date, id): oldest lot first, ties broken by entry order.
Values are taken pro rata to quantity; when a draw empties a lot the exact
remaining value is taken, so rounding never leaves stray paise behind.

ASSUMPTIONS (README section 5): A-FIFO-2 ordering and lot-date eligibility,
A-FIFO-3 pro-rata lot splitting, A-FIFO-4 FG layer release, A-FIFO-5
accessories drawn as-is from import lots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import Bond, ConsumptionAllocation, FGLot, FGModel, ImportLot, Item
from ..numbers import ZERO, money, qty
from .errors import InsufficientStock


@dataclass
class LotTake:
    """A quantity taken from one import lot."""

    lot: ImportLot
    qty: Decimal
    dsv: Decimal
    av: Decimal
    consumption: ConsumptionAllocation | None = None
    fg_lot: FGLot | None = None
    fg_qty: Decimal | None = None


@dataclass
class FGTake:
    fg_lot: FGLot
    fg_qty: Decimal
    parts: list[LotTake] = field(default_factory=list)


def _lock(session: Session, stmt):
    # FOR UPDATE on PostgreSQL; SQLite already serialises via BEGIN IMMEDIATE.
    if session.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return stmt


def share(available_value: Decimal, available_qty: Decimal, take_qty: Decimal) -> Decimal:
    """Pro-rata value of take_qty out of available_qty; exact when emptying."""
    if take_qty >= available_qty:
        return available_value
    if available_qty == 0:
        return ZERO
    return money(available_value * take_qty / available_qty)


def fifo_split(available: list[Decimal], required: Decimal) -> tuple[list[Decimal], Decimal]:
    """Pure FIFO: how much to take from each bucket (in order). Returns
    (takes, shortfall). takes[i] <= available[i]; sum(takes) + shortfall == required."""
    remaining = qty(required)
    takes = []
    for avail in available:
        t = min(avail, remaining) if remaining > 0 else ZERO
        takes.append(t)
        remaining -= t
    return takes, max(remaining, ZERO)


# ------------------------------------------------------------------ import lots
def import_lots_query(item_id: int, as_of: date | None, bond_id: int | None = None):
    stmt = select(ImportLot).where(ImportLot.item_id == item_id, ImportLot.qty_available > 0)
    if as_of is not None and get_rules().lot_must_predate_use:
        stmt = stmt.where(ImportLot.lot_date <= as_of)
    if bond_id:
        stmt = stmt.where(ImportLot.bond_id == bond_id)
    return stmt.order_by(ImportLot.lot_date, ImportLot.id)


def available_import_qty(session: Session, item_id: int, as_of: date | None, bond_id: int | None = None) -> Decimal:
    stmt = import_lots_query(item_id, as_of, bond_id).with_only_columns(
        func.coalesce(func.sum(ImportLot.qty_available), 0)
    ).order_by(None)
    return qty(session.execute(stmt).scalar_one())


def draw_import_lots(session: Session, item_id: int, required: Decimal, as_of: date | None,
                     bond_id: int | None = None) -> list[LotTake]:
    """Take `required` quantity of an item from import lots, oldest first."""
    required = qty(required)
    lots = list(session.scalars(_lock(session, import_lots_query(item_id, as_of, bond_id))))
    takes_q, shortfall = fifo_split([l.qty_available for l in lots], required)
    if shortfall > 0:
        item = session.get(Item, item_id)
        raise InsufficientStock([dict(label=item.code if item else item_id, required=required,
                                      available=required - shortfall, shortfall=shortfall)])
    takes: list[LotTake] = []
    for lot, tq in zip(lots, takes_q):
        if tq <= 0:
            continue
        dsv = share(lot.dsv_available, lot.qty_available, tq)
        av = share(lot.av_available, lot.qty_available, tq)
        lot.qty_available = qty(lot.qty_available - tq)
        lot.dsv_available = money(lot.dsv_available - dsv)
        lot.av_available = money(lot.av_available - av)
        takes.append(LotTake(lot=lot, qty=tq, dsv=dsv, av=av))
    session.flush()  # CHECK constraints fire here as a second line of defence
    return takes


# ------------------------------------------------------------------ FG lots
def fg_lots_query(model_id: int, as_of: date | None):
    stmt = select(FGLot).where(FGLot.model_id == model_id, FGLot.qty_available > 0)
    if as_of is not None and get_rules().lot_must_predate_use:
        stmt = stmt.where(FGLot.lot_date <= as_of)
    return stmt.order_by(FGLot.lot_date, FGLot.id)


def available_fg_qty(session: Session, model_id: int, as_of: date | None) -> Decimal:
    stmt = fg_lots_query(model_id, as_of).with_only_columns(
        func.coalesce(func.sum(FGLot.qty_available), 0)
    ).order_by(None)
    return qty(session.execute(stmt).scalar_one())


def draw_fg_lots(session: Session, model_id: int, required: Decimal, as_of: date | None) -> list[FGTake]:
    """Take finished units FIFO across FG lots, releasing the proportional share
    of each lot's underlying import-lot consumption."""
    required = qty(required)
    lots = list(session.scalars(_lock(session, fg_lots_query(model_id, as_of))))
    takes_q, shortfall = fifo_split([l.qty_available for l in lots], required)
    if shortfall > 0:
        model = session.get(FGModel, model_id)
        raise InsufficientStock([dict(label=model.code if model else model_id, required=required,
                                      available=required - shortfall, shortfall=shortfall)])
    out: list[FGTake] = []
    for lot, k in zip(lots, takes_q):
        if k <= 0:
            continue
        take = FGTake(fg_lot=lot, fg_qty=k)
        emptying = k >= lot.qty_available
        cons = session.scalars(_lock(session, select(ConsumptionAllocation).where(
            ConsumptionAllocation.fg_lot_id == lot.id).order_by(ConsumptionAllocation.id)))
        for c in cons:
            if emptying:
                cq, cd, ca = c.qty_remaining, c.dsv_remaining, c.av_remaining
            else:
                cq = qty(c.qty_remaining * k / lot.qty_available)
                cd = share(c.dsv_remaining, lot.qty_available, k)
                ca = share(c.av_remaining, lot.qty_available, k)
            if cq == 0 and cd == 0 and ca == 0:
                continue
            c.qty_remaining = qty(c.qty_remaining - cq)
            c.dsv_remaining = money(c.dsv_remaining - cd)
            c.av_remaining = money(c.av_remaining - ca)
            take.parts.append(LotTake(lot=c.import_lot, qty=cq, dsv=cd, av=ca, consumption=c,
                                      fg_lot=lot, fg_qty=k))
        lot.qty_available = qty(lot.qty_available - k)
        out.append(take)
    session.flush()
    return out


# ------------------------------------------------------------------ line checks
def check_lines_available(session: Session, lines: list[dict], as_of: date) -> None:
    """Aggregate FG / ITEM requirements of a document and raise one
    InsufficientStock listing every shortage, before anything is mutated."""
    need: dict[tuple[str, int], Decimal] = {}
    for ln in lines:
        key = ("FG", ln["model_id"]) if ln["line_type"] == "FG" else ("ITEM", ln["item_id"])
        need[key] = need.get(key, ZERO) + qty(ln["qty"])
    shortages = []
    for (kind, target), required in need.items():
        if kind == "FG":
            avail = available_fg_qty(session, target, as_of)
            label = session.get(FGModel, target).code
        else:
            avail = available_import_qty(session, target, as_of)
            label = session.get(Item, target).code
        if avail < required:
            shortages.append(dict(label=label, required=required, available=avail,
                                  shortfall=required - avail))
    if shortages:
        raise InsufficientStock(shortages)


def bond_totals(takes: list[LotTake]) -> dict[int, Decimal]:
    """Duty-saved value per bond. Duty-paid lots (no bond) are left out: they were
    never debited, so they are never recredited or memo-posted."""
    totals: dict[int, Decimal] = {}
    for t in takes:
        if t.lot.bond_id is None:
            continue
        totals[t.lot.bond_id] = totals.get(t.lot.bond_id, ZERO) + t.dsv
    return totals
