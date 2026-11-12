"""Duty-paid imports: never debited to a bond, nothing recredited when the goods
are exported, no DTA duty, not on Form III; duty heads in the registers; the
export template pre-filled with stock on hand."""
import io
from datetime import date

import pytest
from openpyxl import load_workbook
from sqlalchemy.exc import IntegrityError

from eoubond import doc_templates as dt
from eoubond.explained_xlsx import bond_workbook
from eoubond.models import BondLedgerEntry, ImportInvoice, ImportLot
from eoubond.services import dta, exports, form3, imports, production, reports
from eoubond.services.errors import ValidationError
from tests.conftest import D


def _paid(w, s, no, d, item, q, av, bcd, sws, igst):
    inv = imports.create_import(s, invoice_no=no, invoice_date=d, supplier_id=w.sup.id, bond_id=None,
                                import_type="DUTY_PAID", be_no="9" + no[-3:], be_date=d, lines=[dict(
                                    description=item.description, item_id=item.id, qty=q, assessable_value=av,
                                    bcd_forgone=bcd, sws_forgone=sws, igst_forgone=igst)])
    imports.post_import(s, inv.id)
    return inv


def test_duty_paid_import_does_not_touch_the_bond(world, session):
    w = world
    inv = _paid(w, session, "DP-001", date(2026, 2, 1), w.cab, 10, 1000, 100, 10, 199.8)
    assert inv.status == "POSTED" and inv.bond_id is None and inv.duty_paid
    assert inv.total_dsv == 0 and inv.total_duty == D("309.80")
    lot = session.query(ImportLot).one()
    assert lot.duty_paid and lot.bond_id is None and lot.dsv_total == 0 and lot.qty_received == 10
    assert w.bond.balance == D(1_000_000) and session.query(BondLedgerEntry).count() == 0
    imports.cancel_import(session, inv.id, date(2026, 2, 2))  # no bond credit either
    assert session.query(BondLedgerEntry).count() == 0


def test_bond_import_still_needs_a_bond(world, session):
    with pytest.raises(ValidationError, match="Select a bond"):
        imports.create_import(session, invoice_no="X", invoice_date=date(2026, 2, 1), supplier_id=world.sup.id,
                              bond_id=None, lines=[dict(description="x", item_id=world.cab.id, qty=1,
                                                        assessable_value=1, duty_saved_value=1)])


def test_database_rejects_inconsistent_duty_paid_rows(world, session):
    w = world
    _paid(w, session, "DP-001", date(2026, 2, 1), w.cab, 10, 1000, 100, 10, 199.8)
    lot = session.query(ImportLot).one()
    lot.dsv_total = D(5)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    inv = ImportInvoice(invoice_no="Z", invoice_date=date(2026, 1, 1), supplier_id=w.sup.id, import_type="BOND")
    session.add(inv)
    with pytest.raises(IntegrityError):
        session.flush()


def test_export_mixing_bonded_and_duty_paid_stock_recredits_only_the_bonded_part(world, session):
    w = world
    w.import_("B-001", date(2026, 2, 1), [w.line(w.cab, 4, 400, 400)])  # older lot, under bond
    _paid(w, session, "DP-001", date(2026, 2, 5), w.cab, 6, 600, 60, 6, 119.88)
    bal = w.bond.balance
    sh = exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 1),
                             lines=[dict(line_type="ITEM", item_id=w.cab.id, qty=7)])  # 4 bonded + 3 duty paid
    assert sh.total_recredit == D("400.00") and w.bond.balance == bal + D("400.00")
    reg = reports.export_register(session).rows[0]
    assert reg["dsv"] == D("400.00") and reg["paid"] == D("92.94")  # 3/6 of the duty paid on DP-001
    sh2 = exports.post_export(session, invoice_no="EX-2", export_date=date(2026, 3, 2),
                              lines=[dict(line_type="ITEM", item_id=w.cab.id, qty=3)])  # duty paid only
    assert sh2.total_recredit == 0
    assert session.query(BondLedgerEntry).filter_by(entry_type="EXPORT_CREDIT").count() == 1


def test_production_and_fg_export_with_a_duty_paid_component(world, session):
    w = world
    w.import_("B-001", date(2026, 2, 1), [w.line(w.tft, 2, 2000, 500), w.line(w.csi, 2, 1000, 200),
                                          w.line(w.pcb, 4, 400, 80)])
    _paid(w, session, "DP-001", date(2026, 2, 2), w.bat, 2, 300, 30, 3, 59.94)  # battery paid duty
    b = production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=2, production_date=date(2026, 3, 1))
    assert b.total_dsv == D("780.00")  # battery contributes no duty saved
    memo = session.query(BondLedgerEntry).filter_by(entry_type="CONSUMPTION_MEMO").one()
    assert memo.memo_value == D("780.00")
    sh = exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 5),
                             lines=[dict(line_type="FG", model_id=w.m4343.id, qty=2)])
    assert sh.total_recredit == D("780.00")
    stock = {r["bond"] for r in reports.stock_by_bond(session).rows}
    assert stock == set()  # everything went out


def test_dta_of_duty_paid_goods_owes_no_customs_duty(world, session):
    w = world
    _paid(w, session, "DP-001", date(2026, 2, 1), w.bat, 5, 5000, 500, 50, 999)
    clr = dta.post_dta(session, clearance_no="D-1", clearance_date=date(2026, 3, 1),
                       lines=[dict(line_type="ITEM", item_id=w.bat.id, qty=2)])
    assert clr.total_duty == 0
    dta.mark_paid(session, clr.id, challan_no="-", paid_date=date(2026, 3, 2))
    assert session.query(BondLedgerEntry).filter_by(entry_type="DTA_CREDIT").count() == 0


def test_reports_and_form3_with_duty_paid(world, session):
    w = world
    w.import_("B-001", date(2026, 2, 1), [w.line(w.cab, 4, 400, 400)])
    _paid(w, session, "DP-001", date(2026, 2, 5), w.cab, 6, 600, 60, 6, 119.88)
    reg = {r["invoice"]: r for r in reports.import_register(session).rows}
    assert (reg["B-001"]["type"], reg["B-001"]["forgone"], reg["B-001"]["paid"]) == ("Under bond", D("400.00"), 0)
    assert (reg["DP-001"]["type"], reg["DP-001"]["bond"]) == ("Duty paid", "Duty paid (no bond)")
    assert (reg["DP-001"]["bcd"], reg["DP-001"]["sws"], reg["DP-001"]["igst"], reg["DP-001"]["paid"]) == \
        (D("60.00"), D("6.00"), D("119.88"), D("185.88"))
    stock = {r["bond"]: r for r in reports.stock_by_bond(session).rows}
    assert stock["Duty paid (no bond)"]["rm_qty"] == 6 and stock["Duty paid (no bond)"]["rm_dsv"] == 0
    f = form3.build(session, date(2026, 3, 31))
    assert len(f.sheets["Details of Goods"]) == 1  # the duty-paid BE is not a bonded BE item
    bond_workbook(session)  # builds with duty-paid stock present


def test_import_template_duty_paid_row(world, session):
    session.commit()
    blob = dt.build_import_template(session)
    rows = [dict(_row=2, be_no="111", be_date=date(2026, 2, 1), port="INMAA1", import_type="DUTY PAID",
                 supplier="Paid Supplier", invoice_no="PI-1", invoice_date=date(2026, 1, 30), inv_sno=1, item_sno=1,
                 description="Tether cable", part_no="CAB-TETHER", qty=5, uqc="NOS", av=500, bcd=50, sws=5, igst=99.9)]
    (g,) = dt.apply_import_rows(session, rows, post=True)
    assert g.status == "posted" and "no bond debit" in g.message, g.message
    assert world.bond.balance == D(1_000_000)
    wb = load_workbook(io.BytesIO(blob))
    assert "Import type (BOND / DUTY PAID) *" in [c.value for c in wb["BoE Items"][1]]


def test_export_template_prefilled_with_stock(world, session):
    w = world
    w.import_("B-001", date(2026, 2, 1), [w.line(w.cab, 4, 400, 400), w.line(w.bat, 3, 300, 30)])
    session.commit()
    blob = dt.build_export_template(session, with_stock=True)
    rows = dt.read_export_template(blob)
    stock = {(r["line_type"], r["code"]): r["available"] for r in rows}
    assert stock == {("ITEM", "BAT-LI"): 3, ("ITEM", "CAB-TETHER"): 4}
    # fill one row, leave the other unused -> only the filled one is posted
    for r in rows:
        if r["code"] == "CAB-TETHER":
            r.update(sb_no="5550001", sb_date=date(2026, 3, 1), port="INMAA1", invsn=1, invoice_no="EXP-9",
                     itemsn=1, qty=2)
    (g,) = dt.apply_export_rows(session, rows)
    assert g.status == "posted" and g.lines == 1, g.message
    ws = load_workbook(io.BytesIO(blob))["SB Items"]
    head = [c.value for c in ws[1]]
    assert "Stock available (on download)" in head and "Stock check" in head
