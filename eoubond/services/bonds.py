"""Bond master and bond ledger postings.

Balance model: balance = bond amount - debits + credits.
  * Import posting      -> DEBIT  (duty-saved value of goods brought in)
  * Export              -> CREDIT (duty-saved value of inputs exported)
  * DTA clearance       -> CREDIT (when duty is paid; see rules.dta_recredit_on)
  * Production          -> zero-value MEMO entry (value moves RM -> FG)
  * Import cancellation -> CREDIT reversal
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_rules
from ..models import Bond, BondLedgerEntry
from ..numbers import ZERO, money
from .errors import BondError, ValidationError


def create_bond(session: Session, *, bond_no: str, bond_amount, valid_from: date, valid_to: date,
                remarks: str | None = None) -> Bond:
    bond_no = (bond_no or "").strip()
    amount = money(bond_amount)
    if not bond_no:
        raise ValidationError("Bond number is required")
    if amount <= 0:
        raise ValidationError("Bond amount must be positive")
    if valid_to < valid_from:
        raise ValidationError("Bond validity end date is before the start date")
    if session.scalar(select(Bond).where(Bond.bond_no == bond_no)):
        raise ValidationError(f"Bond {bond_no} already exists")
    bond = Bond(bond_no=bond_no, bond_amount=amount, balance=amount, valid_from=valid_from,
                valid_to=valid_to, remarks=remarks, status="ACTIVE")
    session.add(bond)
    session.flush()
    return bond


def update_bond(session: Session, bond_id: int, *, bond_amount=None, valid_from=None, valid_to=None,
                remarks=None, status=None) -> Bond:
    """Changing the bond amount (e.g. an enhanced bond) moves the balance by the
    same delta; the balance can't go negative."""
    bond = lock_bond(session, bond_id)
    if bond_amount is not None:
        new_amount = money(bond_amount)
        new_balance = money(bond.balance + (new_amount - bond.bond_amount))
        if new_amount <= 0 or new_balance < 0:
            raise BondError(f"Cannot reduce bond {bond.bond_no} below its utilised amount "
                            f"({money(bond.bond_amount - bond.balance)})")
        bond.bond_amount, bond.balance = new_amount, new_balance
    if valid_from is not None:
        bond.valid_from = valid_from
    if valid_to is not None:
        bond.valid_to = valid_to
    if bond.valid_to < bond.valid_from:
        raise ValidationError("Bond validity end date is before the start date")
    if remarks is not None:
        bond.remarks = remarks
    if status is not None:
        if status not in ("ACTIVE", "CLOSED"):
            raise ValidationError("Status must be ACTIVE or CLOSED")
        bond.status = status
    session.flush()
    return bond


def lock_bond(session: Session, bond_id: int) -> Bond:
    session.flush()
    stmt = select(Bond).where(Bond.id == bond_id).execution_options(populate_existing=True)
    if session.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    bond = session.scalar(stmt)
    if bond is None:
        raise BondError(f"Bond {bond_id} not found")
    return bond


def is_valid_on(bond: Bond, on: date) -> bool:
    return bond.valid_from <= on <= bond.valid_to


def _entry(session, bond, *, entry_date, entry_type, source_type, source_id, source_ref,
           debit=ZERO, credit=ZERO, memo=ZERO, narration=None) -> BondLedgerEntry:
    e = BondLedgerEntry(bond_id=bond.id, entry_date=entry_date, entry_type=entry_type,
                        source_type=source_type, source_id=source_id, source_ref=source_ref,
                        debit=money(debit), credit=money(credit), memo_value=money(memo),
                        balance_after=bond.balance, narration=narration)
    session.add(e)
    session.flush()
    return e


def debit(session: Session, bond_id: int, amount, *, entry_date: date, entry_type: str,
          source_type: str, source_id: int, source_ref: str, narration: str | None = None) -> BondLedgerEntry:
    amount = money(amount)
    bond = lock_bond(session, bond_id)
    if bond.status != "ACTIVE":
        raise BondError(f"Bond {bond.bond_no} is {bond.status}")
    if get_rules().reject_import_outside_validity and not is_valid_on(bond, entry_date):
        raise BondError(f"{entry_date:%d-%m-%Y} is outside the validity of bond {bond.bond_no} "
                        f"({bond.valid_from:%d-%m-%Y} to {bond.valid_to:%d-%m-%Y})")
    if amount > bond.balance:
        raise BondError(f"Duty-saved value {amount} exceeds the balance of bond {bond.bond_no} "
                        f"({bond.balance})")
    bond.balance = money(bond.balance - amount)
    return _entry(session, bond, entry_date=entry_date, entry_type=entry_type, source_type=source_type,
                  source_id=source_id, source_ref=source_ref, debit=amount, narration=narration)


def credit(session: Session, bond_id: int, amount, *, entry_date: date, entry_type: str,
           source_type: str, source_id: int, source_ref: str, narration: str | None = None) -> BondLedgerEntry:
    amount = money(amount)
    bond = lock_bond(session, bond_id)
    if not get_rules().allow_credit_after_expiry and entry_date > bond.valid_to:
        raise BondError(f"Bond {bond.bond_no} expired on {bond.valid_to:%d-%m-%Y}; credits not allowed")
    if bond.balance + amount > bond.bond_amount:
        raise BondError(f"Credit {amount} would take bond {bond.bond_no} above its amount")
    bond.balance = money(bond.balance + amount)
    return _entry(session, bond, entry_date=entry_date, entry_type=entry_type, source_type=source_type,
                  source_id=source_id, source_ref=source_ref, credit=amount, narration=narration)


def memo(session: Session, bond_id: int, value, *, entry_date: date, entry_type: str, source_type: str,
         source_id: int, source_ref: str, narration: str | None = None) -> BondLedgerEntry:
    bond = lock_bond(session, bond_id)
    return _entry(session, bond, entry_date=entry_date, entry_type=entry_type, source_type=source_type,
                  source_id=source_id, source_ref=source_ref, memo=value, narration=narration)


def active_bonds(session: Session) -> list[Bond]:
    return list(session.scalars(select(Bond).where(Bond.status == "ACTIVE").order_by(Bond.valid_from)))
