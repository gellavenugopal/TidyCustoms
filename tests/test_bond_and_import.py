import json
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from eoubond import config
from eoubond.models import AuditLog, Bond, BondLedgerEntry, DescriptionMapping, ImportLot
from eoubond.services import bonds, imports, masters
from eoubond.services.errors import BondError, BusinessError, UnmappedDescriptions
from tests.conftest import D


def test_import_debits_bond_and_creates_lots(world, session):
    w = world
    inv = w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 10, 200000, 50000), w.line(w.csi, 10, 100000, 25000)])
    assert inv.status == "POSTED"
    assert w.bond.balance == D("925000.00")
    e = session.query(BondLedgerEntry).one()
    assert (e.entry_type, e.source_type, e.source_id, e.debit, e.balance_after) == \
        ("IMPORT_DEBIT", "IMPORT", inv.id, D("75000.00"), D("925000.00"))
    assert session.query(ImportLot).count() == 2


def test_import_exceeding_balance_rejected(world, session):
    w = world
    inv = w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 600000)], bond=w.bond2, post=False)
    with pytest.raises(BondError, match="exceeds the balance"):
        imports.post_import(session, inv.id)
    session.refresh(w.bond2)
    assert w.bond2.balance == D("500000.00") and inv.status == "DRAFT"


def test_import_outside_validity_rejected(world, session):
    w = world
    inv = w.import_("INV-1", date(2027, 1, 5), [w.line(w.tft, 1, 10, 100)], post=False)
    with pytest.raises(BondError, match="outside the validity"):
        imports.post_import(session, inv.id)


def test_validity_rule_is_configurable(world, session):
    config.set_rules(reject_import_outside_validity=False)
    w = world
    w.import_("INV-1", date(2027, 1, 5), [w.line(w.tft, 1, 10, 100)])
    assert w.bond.balance == D("999900.00")


def test_closed_bond_rejects_import(world, session):
    w = world
    bonds.update_bond(session, w.bond.id, status="CLOSED")
    inv = w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 100)], post=False)
    with pytest.raises(BondError, match="CLOSED"):
        imports.post_import(session, inv.id)


def test_unmapped_description_blocks_posting_then_mapping_is_reused(world, session):
    w = world
    lines = [dict(description="  Flat-panel TFT  array, 43*43cm ", qty=2, assessable_value=1000, duty_saved_value=200)]
    inv = imports.create_import(session, invoice_no="INV-9", invoice_date=date(2026, 2, 1),
                                supplier_id=w.sup.id, bond_id=w.bond.id, lines=lines)
    with pytest.raises(UnmappedDescriptions):
        imports.post_import(session, inv.id)
    imports.resolve_line(session, inv.lines[0].id, w.tft.id)
    imports.post_import(session, inv.id)
    m = session.query(DescriptionMapping).filter_by(item_id=w.tft.id).one()
    assert m.normalized_description == "FLAT PANEL TFT ARRAY 43 43CM"

    # Same description, different spacing / case / punctuation -> resolves automatically
    inv2 = imports.create_import(session, invoice_no="INV-10", invoice_date=date(2026, 2, 2), supplier_id=w.sup.id,
                                 bond_id=w.bond.id, lines=[dict(description="FLAT PANEL TFT ARRAY 43-43cm", qty=1,
                                                                assessable_value=1, duty_saved_value=1)])
    assert inv2.lines[0].item_id == w.tft.id


def test_supplier_mapping_wins_over_global(world, session):
    w = world
    other = masters.create_supplier(session, name="Other Ltd")
    masters.save_mapping(session, description="Panel", item_id=w.csi.id)  # global
    masters.save_mapping(session, description="Panel", item_id=w.tft.id, supplier_id=w.sup.id)
    assert masters.find_mapping(session, w.sup.id, "panel").item_id == w.tft.id
    assert masters.find_mapping(session, other.id, "panel").item_id == w.csi.id


def test_cancel_untouched_import_reverses_debit(world, session):
    w = world
    inv = w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 100)])
    imports.cancel_import(session, inv.id, date(2026, 2, 3))
    assert w.bond.balance == D("1000000.00") and inv.status == "CANCELLED"
    assert session.query(ImportLot).count() == 0


def test_cannot_cancel_consumed_import(world, session):
    from eoubond.services.stock import draw_import_lots
    w = world
    inv = w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 2, 10, 100)])
    draw_import_lots(session, w.tft.id, D(1), date(2026, 2, 2))
    with pytest.raises(BusinessError):
        imports.cancel_import(session, inv.id, date(2026, 2, 3))


def test_duplicate_invoice_rejected(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 100)])
    with pytest.raises(BusinessError):
        w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 100)])


def test_db_check_blocks_negative_bond_balance(world, session):
    w = world
    w.bond.balance = D(-1)
    with pytest.raises(IntegrityError):
        session.flush()


def test_bond_amount_enhancement_moves_balance(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 900000)])
    bonds.update_bond(session, w.bond.id, bond_amount=1_500_000)
    assert w.bond.balance == D("600000.00")
    with pytest.raises(BondError):
        bonds.update_bond(session, w.bond.id, bond_amount=800_000)


def test_audit_trail_records_old_and_new(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 1, 10, 100)])
    upd = [a for a in session.query(AuditLog).filter_by(table_name="bonds", action="UPDATE")]
    assert upd, "bond balance change must be audited"
    last = upd[-1]
    assert last.username == "tester"
    assert json.loads(last.old_values)["balance"] == "1000000.00"
    assert json.loads(last.new_values)["balance"] == "999900.00"
    assert session.query(AuditLog).filter_by(table_name="bond_ledger", action="CREATE").count() == 1
    masters.delete_row(session, masters.create_item(session, code="TMP", description="temp"))
    d = session.query(AuditLog).filter_by(table_name="items", action="DELETE").one()
    assert json.loads(d.old_values)["code"] == "TMP"
