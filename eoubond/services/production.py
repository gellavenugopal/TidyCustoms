"""BOM-driven production: consume every BOM component FIFO from duty-saved
import lots and create a finished-goods lot that remembers what it consumed."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import BOM, ConsumptionAllocation, FGLot, FGModel, ProductionBatch
from ..numbers import ZERO, money, qty
from . import bonds
from .errors import InsufficientStock, ValidationError
from .masters import effective_bom
from .stock import available_import_qty, bond_totals, draw_import_lots


def _resolve_bom(session, model_id, on, bom_id) -> BOM:
    if bom_id:
        bom = session.get(BOM, bom_id)
        if not bom or bom.model_id != model_id:
            raise ValidationError("BOM version does not belong to this model")
        if bom.effective_from > on:
            raise ValidationError(f"BOM v{bom.version} is effective only from {bom.effective_from:%d-%m-%Y}")
        return bom
    return effective_bom(session, model_id, on)


def requirements(bom: BOM, units: Decimal) -> list[tuple]:
    return [(ln.item, qty(ln.qty_per_unit * units)) for ln in bom.lines]


def availability(session: Session, *, model_id: int, qty_units, production_date: date,
                 bom_id: int | None = None, bond_id: int | None = None) -> tuple[BOM, list[dict]]:
    """Preview: required vs available per component, without changing anything."""
    bom = _resolve_bom(session, model_id, production_date, bom_id)
    rows = []
    for item, need in requirements(bom, qty(qty_units)):
        have = available_import_qty(session, item.id, production_date, bond_id)
        rows.append(dict(item=item, required=need, available=have, shortfall=max(need - have, ZERO)))
    return bom, rows


def produce(session: Session, *, batch_no: str, model_id: int, qty_units, production_date: date,
            bom_id: int | None = None, bond_id: int | None = None,
            remarks: str | None = None) -> ProductionBatch:
    batch_no = (batch_no or "").strip()
    units = qty(qty_units)
    if not batch_no:
        raise ValidationError("Batch number is required")
    if units <= 0:
        raise ValidationError("Quantity produced must be positive")
    if not session.get(FGModel, model_id):
        raise ValidationError("Select a model")
    if session.scalar(select(ProductionBatch).where(ProductionBatch.batch_no == batch_no)):
        raise ValidationError(f"Batch {batch_no} already exists")
    if get_rules().production_scope == "bond" and not bond_id:
        raise ValidationError("Rules require production to name the bond it consumes from")

    bom, rows = availability(session, model_id=model_id, qty_units=units,
                             production_date=production_date, bom_id=bom_id, bond_id=bond_id)
    short = [dict(label=r["item"].code, required=r["required"], available=r["available"],
                  shortfall=r["shortfall"]) for r in rows if r["shortfall"] > 0]
    if short:
        raise InsufficientStock(short)  # nothing has been touched yet

    batch = ProductionBatch(batch_no=batch_no, production_date=production_date, model_id=model_id,
                            bom_id=bom.id, qty=units, restrict_bond_id=bond_id, remarks=remarks)
    session.add(batch)
    session.flush()
    fg = FGLot(batch_id=batch.id, model_id=model_id, lot_date=production_date,
               qty_produced=units, qty_available=units)
    session.add(fg)
    session.flush()

    all_takes = []
    for item, need in requirements(bom, units):
        takes = draw_import_lots(session, item.id, need, production_date, bond_id)
        for t in takes:
            session.add(ConsumptionAllocation(
                batch_id=batch.id, fg_lot_id=fg.id, import_lot_id=t.lot.id, bond_id=t.lot.bond_id,
                item_id=item.id, qty=t.qty, dsv=t.dsv, av=t.av,
                qty_remaining=t.qty, dsv_remaining=t.dsv, av_remaining=t.av))
        all_takes.extend(takes)
    batch.total_dsv = money(sum((t.dsv for t in all_takes), ZERO))
    batch.total_av = money(sum((t.av for t in all_takes), ZERO))
    session.flush()

    if get_rules().consumption_memo_entries:
        for b_id, value in bond_totals(all_takes).items():
            bonds.memo(session, b_id, value, entry_date=production_date, entry_type="CONSUMPTION_MEMO",
                       source_type="PRODUCTION", source_id=batch.id, source_ref=batch.batch_no,
                       narration=f"{units:f}".rstrip("0").rstrip(".") + f" x {batch.model.code}"
                                 + f" (BOM v{bom.version}) - RM moved to FG")
    return batch
