"""Items, suppliers, models, BOM versions and description mappings."""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import BOM, BOMLine, DescriptionMapping, FGModel, Item, Supplier
from ..numbers import D
from .errors import ValidationError


# ------------------------------------------------------------------ items
def create_item(session: Session, *, code: str, description: str, uom: str = "NOS",
                item_type: str = "COMPONENT", hs_code: str | None = None,
                bcd_rate=None, sws_rate=None) -> Item:
    code = (code or "").strip().upper()
    if not code or not (description or "").strip():
        raise ValidationError("Item code and description are required")
    if session.scalar(select(Item).where(Item.code == code)):
        raise ValidationError(f"Item code {code} already exists")
    if item_type not in ("COMPONENT", "ACCESSORY"):
        raise ValidationError("Item type must be COMPONENT or ACCESSORY")
    item = Item(code=code, description=description.strip(), uom=(uom or "NOS").upper(),
                item_type=item_type, hs_code=hs_code or None,
                bcd_rate=None if bcd_rate in (None, "") else D(bcd_rate),
                sws_rate=None if sws_rate in (None, "") else D(sws_rate))
    session.add(item)
    session.flush()
    return item


def update_item(session: Session, item_id: int, **fields) -> Item:
    item = session.get(Item, item_id)
    if not item:
        raise ValidationError("Item not found")
    for key in ("description", "uom", "item_type", "hs_code", "active"):
        if key in fields and fields[key] is not None:
            setattr(item, key, fields[key])
    for key in ("bcd_rate", "sws_rate"):
        if key in fields:
            setattr(item, key, None if fields[key] in (None, "") else D(fields[key]))
    session.flush()
    return item


def delete_row(session: Session, obj) -> None:
    """Delete a master row; refuse if anything references it."""
    try:
        with session.begin_nested():
            session.delete(obj)
            session.flush()
    except IntegrityError as exc:
        raise ValidationError("This record is in use and cannot be deleted; deactivate it instead") from exc


def create_supplier(session: Session, *, name: str, country: str | None = None) -> Supplier:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Supplier name is required")
    if session.scalar(select(Supplier).where(Supplier.name == name)):
        raise ValidationError(f"Supplier {name} already exists")
    s = Supplier(name=name, country=country or None)
    session.add(s)
    session.flush()
    return s


def create_model(session: Session, *, code: str, name: str) -> FGModel:
    code = (code or "").strip().upper()
    if not code or not (name or "").strip():
        raise ValidationError("Model code and name are required")
    if session.scalar(select(FGModel).where(FGModel.code == code)):
        raise ValidationError(f"Model {code} already exists")
    m = FGModel(code=code, name=name.strip())
    session.add(m)
    session.flush()
    return m


# ------------------------------------------------------------------ BOMs
def create_bom_version(session: Session, *, model_id: int, effective_from: date,
                       lines: list[dict], remarks: str | None = None) -> BOM:
    """lines: [{item_id, qty_per_unit}]. Version number = previous max + 1.
    BOMs are immutable once created; a change is a new version."""
    if not lines:
        raise ValidationError("A BOM needs at least one component")
    seen = set()
    for ln in lines:
        if ln["item_id"] in seen:
            raise ValidationError("A component can appear only once per BOM version")
        seen.add(ln["item_id"])
        if D(ln["qty_per_unit"]) <= 0:
            raise ValidationError("Quantity per unit must be positive")
    existing = list(session.scalars(select(BOM).where(BOM.model_id == model_id)))
    if any(b.effective_from == effective_from for b in existing):
        raise ValidationError("A BOM version with this effective date already exists")
    version = max((b.version for b in existing), default=0) + 1
    bom = BOM(model_id=model_id, version=version, effective_from=effective_from, remarks=remarks)
    bom.lines = [BOMLine(item_id=ln["item_id"], qty_per_unit=D(ln["qty_per_unit"])) for ln in lines]
    session.add(bom)
    session.flush()
    return bom


def effective_bom(session: Session, model_id: int, on: date) -> BOM:
    bom = session.scalar(select(BOM).where(BOM.model_id == model_id, BOM.effective_from <= on)
                         .order_by(BOM.effective_from.desc()).limit(1))
    if not bom:
        model = session.get(FGModel, model_id)
        raise ValidationError(f"No BOM for {model.code if model else model_id} effective on {on:%d-%m-%Y}")
    return bom


# ------------------------------------------------------------------ mappings
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_description(text: str) -> str:
    s = (text or "").upper()
    if get_rules().mapping_strip_punctuation:
        s = _PUNCT.sub(" ", s).replace("_", " ")
    return _SPACE.sub(" ", s).strip()


def find_mapping(session: Session, supplier_id: int | None, description: str) -> DescriptionMapping | None:
    """Supplier-specific mapping first, then a global one."""
    key = normalize_description(description)
    if supplier_id:
        m = session.scalar(select(DescriptionMapping).where(
            DescriptionMapping.scope == str(supplier_id), DescriptionMapping.normalized_description == key))
        if m:
            return m
    return session.scalar(select(DescriptionMapping).where(
        DescriptionMapping.scope == "GLOBAL", DescriptionMapping.normalized_description == key))


def save_mapping(session: Session, *, description: str, item_id: int,
                 supplier_id: int | None = None) -> DescriptionMapping:
    """Create or update the mapping for (scope, normalised description)."""
    if not session.get(Item, item_id):
        raise ValidationError("Unknown item code")
    key = normalize_description(description)
    if not key:
        raise ValidationError("Description is empty")
    scope = str(supplier_id) if supplier_id else "GLOBAL"
    m = session.scalar(select(DescriptionMapping).where(
        DescriptionMapping.scope == scope, DescriptionMapping.normalized_description == key))
    if m:
        m.item_id = item_id
    else:
        m = DescriptionMapping(scope=scope, supplier_id=supplier_id, normalized_description=key,
                               sample_description=description.strip(), item_id=item_id)
        session.add(m)
    session.flush()
    return m
