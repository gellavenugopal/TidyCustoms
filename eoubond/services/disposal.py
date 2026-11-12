"""Shared stock draw for exports and DTA clearances.

FG line   -> FIFO over finished-goods lots of the model, releasing each lot's
             share of its consumed import lots.
ITEM line -> FIFO directly over import lots of the item (accessories such as
             cables / spare batteries, or any other duty-saved goods).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from ..models import FGModel, Item
from ..numbers import qty
from .errors import ValidationError
from .stock import LotTake, check_lines_available, draw_fg_lots, draw_import_lots


def clean_lines(session: Session, lines: list[dict]) -> list[dict]:
    out = []
    for n, ln in enumerate(lines, start=1):
        lt = (ln.get("line_type") or "").upper()
        q = qty(ln.get("qty"))
        if lt not in ("FG", "ITEM"):
            raise ValidationError(f"Line {n}: choose a finished model or an item")
        if q <= 0:
            raise ValidationError(f"Line {n}: quantity must be positive")
        if lt == "FG":
            m = session.get(FGModel, int(ln.get("model_id") or 0))
            if not m:
                raise ValidationError(f"Line {n}: select a model")
            out.append({**ln, "line_type": "FG", "model_id": m.id, "item_id": None, "qty": q,
                        "_label": m.name})
        else:
            it = session.get(Item, int(ln.get("item_id") or 0))
            if not it:
                raise ValidationError(f"Line {n}: select an item")
            out.append({**ln, "line_type": "ITEM", "item_id": it.id, "model_id": None, "qty": q,
                        "_label": it.description})
    if not out:
        raise ValidationError("Add at least one line")
    return out


def draw_for_line(session: Session, ln: dict, on: date) -> list[LotTake]:
    if ln["line_type"] == "FG":
        parts: list[LotTake] = []
        for t in draw_fg_lots(session, ln["model_id"], ln["qty"], on):
            parts.extend(t.parts)
        return parts
    return draw_import_lots(session, ln["item_id"], ln["qty"], on)


def precheck(session: Session, lines: list[dict], on: date) -> None:
    check_lines_available(session, lines, on)
