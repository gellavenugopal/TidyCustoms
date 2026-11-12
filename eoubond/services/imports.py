"""Import entry: draft -> resolve descriptions -> post (creates lots, debits bond)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit_context import current_username
from ..config import get_rules
from ..models import Bond, ImportInvoice, ImportLine, ImportLot, Supplier, _now
from ..numbers import money, qty
from . import bonds
from .errors import BusinessError, UnmappedDescriptions, ValidationError
from .masters import find_mapping, normalize_description, save_mapping
from .stock import share

DUTY_SPLIT = ("bcd_forgone", "sws_forgone", "igst_forgone", "cess_forgone", "other_forgone")


IMPORT_TYPES = ("BOND", "DUTY_PAID")


def create_import(session: Session, *, invoice_no: str, invoice_date: date, supplier_id: int,
                  bond_id: int | None, lines: list[dict], be_no: str | None = None,
                  be_date: date | None = None, currency: str | None = None,
                  port_of_import: str | None = None, be_invoice_serial=None,
                  clearance_date: date | None = None, receipt_date: date | None = None,
                  import_type: str = "BOND") -> ImportInvoice:
    """lines: [{description, qty, assessable_value, duty_saved_value, item_id?,
    be_item_serial?, qty_received?, bcd_forgone?, sws_forgone?, igst_forgone?,
    cess_forgone?, other_forgone?}].

    Each description is resolved through the mapping table; an explicitly chosen
    item_id is saved as a new supplier mapping. When the duty split is given,
    duty_saved_value is its total (a total entered as well must agree to Rs 1).

    import_type BOND: duty-saved under the bond - the bond is debited when posted.
    import_type DUTY_PAID: cleared on payment of duty. No bond; the duty heads hold
    the duty PAID; duty-saved value is 0, so the bond is never debited and an export
    of these goods recredits nothing."""
    invoice_no = (invoice_no or "").strip()
    import_type = (import_type or "BOND").upper().replace(" ", "_")
    if import_type not in IMPORT_TYPES:
        raise ValidationError("Import type must be BOND or DUTY PAID")
    paid = import_type == "DUTY_PAID"
    if not invoice_no:
        raise ValidationError("Invoice number is required")
    if not session.get(Supplier, supplier_id):
        raise ValidationError("Select a supplier")
    if paid:
        bond_id = None
    elif not bond_id or not session.get(Bond, bond_id):
        raise ValidationError("Select a bond (or mark the import as duty paid)")
    if session.scalar(select(ImportInvoice).where(ImportInvoice.supplier_id == supplier_id,
                                                  ImportInvoice.invoice_no == invoice_no)):
        raise ValidationError(f"Invoice {invoice_no} from this supplier is already entered")
    if not lines:
        raise ValidationError("Add at least one line")

    inv = ImportInvoice(invoice_no=invoice_no, invoice_date=invoice_date, supplier_id=supplier_id,
                        import_type=import_type, bond_id=bond_id, be_no=be_no or None, be_date=be_date, currency=currency or None,
                        port_of_import=(port_of_import or "").strip().upper() or get_rules().form3_default_port_of_import or None,
                        be_invoice_serial=int(be_invoice_serial or 1), clearance_date=clearance_date,
                        receipt_date=receipt_date, status="DRAFT")
    for n, ln in enumerate(lines, start=1):
        desc = (ln.get("description") or "").strip()
        q = qty(ln.get("qty"))
        if not desc or q <= 0:
            raise ValidationError(f"Line {n}: description and a positive quantity are required")
        av, dsv = money(ln.get("assessable_value")), money(ln.get("duty_saved_value"))
        split = {k: money(ln.get(k)) for k in DUTY_SPLIT}
        if av < 0 or dsv < 0 or any(v < 0 for v in split.values()):
            raise ValidationError(f"Line {n}: values cannot be negative")
        if any(split.values()):
            total = money(sum(split.values()))
            if dsv and abs(dsv - total) > 1:
                raise ValidationError(f"Line {n}: duty split adds up to {total}, "
                                      f"but the total entered is {dsv}")
            dsv = total
        if paid:
            if dsv and not any(split.values()):
                split["other_forgone"] = dsv  # keep the duty paid on record
            dsv = money(0)  # nothing is saved under bond
        received = ln.get("qty_received")
        received = None if received in (None, "") else qty(received)
        if received is not None and not (0 <= received <= q):
            raise ValidationError(f"Line {n}: quantity received must be between 0 and the quantity imported")
        serial = ln.get("be_item_serial")
        item_id = ln.get("item_id") or None
        if item_id:
            save_mapping(session, description=desc, item_id=int(item_id), supplier_id=supplier_id)
        else:
            m = find_mapping(session, supplier_id, desc)
            item_id = m.item_id if m else None
        inv.lines.append(ImportLine(line_no=n, description=desc, normalized_description=normalize_description(desc),
                                    item_id=item_id, qty=q, assessable_value=av, duty_saved_value=dsv,
                                    be_item_serial=int(serial) if serial not in (None, "") else n,
                                    hs_code=(str(ln.get("hs_code") or "").strip() or None),
                                    qty_received=received, **split))
    session.add(inv)
    session.flush()
    return inv


def unmapped_lines(inv: ImportInvoice) -> list[ImportLine]:
    return [l for l in inv.lines if not l.item_id]


def resolve_line(session: Session, line_id: int, item_id: int, *, scope: str = "supplier") -> ImportLine:
    """User resolves a description to an item code; the resolution is saved to the
    mapping table (scope 'supplier' or 'global') and applied to every other
    unmapped draft line with the same description."""
    line = session.get(ImportLine, line_id)
    if not line or line.invoice.status != "DRAFT":
        raise ValidationError("Only lines on a draft import can be re-mapped")
    supplier_id = line.invoice.supplier_id if scope == "supplier" else None
    save_mapping(session, description=line.description, item_id=item_id, supplier_id=supplier_id)
    line.item_id = item_id
    for other in session.scalars(select(ImportLine).join(ImportInvoice).where(
            ImportInvoice.status == "DRAFT", ImportLine.item_id.is_(None),
            ImportLine.normalized_description == line.normalized_description)):
        if scope == "global" or other.invoice.supplier_id == line.invoice.supplier_id:
            other.item_id = item_id
    session.flush()
    return line


def post_import(session: Session, invoice_id: int) -> ImportInvoice:
    inv = session.get(ImportInvoice, invoice_id)
    if not inv:
        raise ValidationError("Import not found")
    if inv.status != "DRAFT":
        raise BusinessError(f"Import {inv.invoice_no} is already {inv.status}")
    missing = unmapped_lines(inv)
    if missing:
        raise UnmappedDescriptions(missing)
    total = money(sum(l.duty_saved_value for l in inv.lines))
    if not inv.duty_paid:
        # bonds.debit validates status, validity (by invoice date) and balance.
        bonds.debit(session, inv.bond_id, total, entry_date=inv.invoice_date, entry_type="IMPORT_DEBIT",
                    source_type="IMPORT", source_id=inv.id, source_ref=inv.invoice_no,
                    narration=f"Import from {inv.supplier.name}" + (f", BE {inv.be_no}" if inv.be_no else ""))
    for l in inv.lines:
        # Only goods actually received become usable stock [A-F3-6]. The bond stays
        # debited with the full duty forgone; the short-received share is never recredited.
        rq = l.received
        if rq <= 0:
            continue
        dsv = share(l.duty_saved_value, l.qty, rq)
        av = share(l.assessable_value, l.qty, rq)
        session.add(ImportLot(import_line_id=l.id, bond_id=inv.bond_id, duty_paid=inv.duty_paid, item_id=l.item_id,
                              lot_date=inv.invoice_date, qty_received=rq, qty_available=rq,
                              dsv_total=dsv, dsv_available=dsv, av_total=av, av_available=av))
    inv.status = "POSTED"
    inv.posted_at = _now()
    inv.posted_by = current_username()
    session.flush()
    return inv


def cancel_import(session: Session, invoice_id: int, on: date) -> ImportInvoice:
    """Reverse a posted import whose lots are completely untouched."""
    inv = session.get(ImportInvoice, invoice_id)
    if not inv or inv.status != "POSTED":
        raise BusinessError("Only a posted import can be cancelled")
    lots = list(session.scalars(select(ImportLot).join(ImportLine).where(ImportLine.invoice_id == inv.id)))
    if any(l.qty_available != l.qty_received for l in lots):
        raise BusinessError("Stock from this import has already been consumed; it cannot be cancelled")
    total = money(sum(l.duty_saved_value for l in inv.lines))  # reverse the full debit
    for l in lots:
        session.delete(l)
    session.flush()
    if not inv.duty_paid:
        bonds.credit(session, inv.bond_id, total, entry_date=on, entry_type="IMPORT_REVERSAL",
                     source_type="IMPORT", source_id=inv.id, source_ref=inv.invoice_no,
                     narration="Cancellation of import")
    inv.status = "CANCELLED"
    session.flush()
    return inv


def delete_draft(session: Session, invoice_id: int) -> None:
    inv = session.get(ImportInvoice, invoice_id)
    if not inv or inv.status != "DRAFT":
        raise BusinessError("Only drafts can be deleted")
    session.delete(inv)
    session.flush()
