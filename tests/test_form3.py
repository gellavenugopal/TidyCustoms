"""Form III quarterly return: column arithmetic, row selection, rule switches and
the .xls layout (sheet names, headers, cell types) of the filed template."""
from datetime import date

import pytest
import xlrd

from eoubond import config
from eoubond.services import dta, exports, form3, imports, production
from tests.conftest import D

Q_END = date(2026, 6, 30)  # quarter Apr-Jun 2026
H = form3.DETAILS_HEADERS
col = {h: i for i, h in enumerate(H)}


def _imp(w, s, no, be_no, be_date, lines, received=None):
    inv = imports.create_import(s, invoice_no=no, invoice_date=be_date, supplier_id=w.sup.id, bond_id=w.bond.id,
                                be_no=be_no, be_date=be_date, port_of_import="INHYD4",
                                clearance_date=be_date, receipt_date=be_date, lines=lines)
    imports.post_import(s, inv.id)
    return inv


def _line(item, q, av, bcd, sws, igst, **kw):
    return dict(description=f"{item.description} (RAW MATERIAL)", item_id=item.id, qty=q, assessable_value=av,
                bcd_forgone=bcd, sws_forgone=sws, igst_forgone=igst, **kw)


@pytest.fixture
def scenario(world, session):
    w, s = world, session
    _imp(w, s, "INV-A", "1001", date(2026, 2, 1), [
        _line(w.tft, 10, 200000, 15000, 1500, 38970), _line(w.csi, 10, 100000, 10000, 1000, 19980),
        _line(w.pcb, 20, 40000, 4000, 400, 7992), _line(w.bat, 20, 20000, 2000, 200, 3996),
        _line(w.cab, 10, 5000, 500, 50, 999)])
    _imp(w, s, "INV-C", "1000", date(2026, 1, 10), [_line(w.cab, 5, 2500, 250, 25, 500)])
    # cable lot from INV-C fully exported before the quarter -> must not be listed
    exports.post_export(s, invoice_no="EX-0", export_date=date(2026, 2, 15),
                        lines=[dict(line_type="ITEM", item_id=w.cab.id, qty=5)])
    production.produce(s, batch_no="P1", model_id=w.m4343.id, qty_units=4, production_date=date(2026, 3, 1))
    exports.post_export(s, invoice_no="EX-1", export_date=date(2026, 4, 10), shipping_bill_no="5521907",
                        shipping_bill_date=date(2026, 4, 10), port_of_export="INBOM4", lines=[
                            dict(line_type="FG", model_id=w.m4343.id, qty=3),
                            dict(line_type="ITEM", item_id=w.cab.id, qty=4, export_item_name="Tether cable")])
    clr = dta.post_dta(s, clearance_no="DTA-1", clearance_date=date(2026, 5, 1),
                       lines=[dict(line_type="ITEM", item_id=w.bat.id, qty=2)])
    dta.mark_paid(s, clr.id, challan_no="CH-9", paid_date=date(2026, 5, 3))
    _imp(w, s, "INV-B", "1002", date(2026, 5, 15), [_line(w.tft, 5, 100000, 7500, 750, 19485, qty_received=4)])
    return w


def _rows(f):
    return {(r[0], r[4]): dict(zip(H, r)) for r in f.sheets["Details of Goods"]}


def test_details_columns_follow_the_filed_arithmetic(scenario, session):
    f = form3.build(session, Q_END)
    rows = _rows(f)
    assert (1000, 1) not in rows  # exhausted before the quarter
    tft = rows[(1001, 1)]
    assert tft["BE Date(DD-MMM-YY)"] == "01-FEB-26" and tft["Port of Import"] == "INHYD4"
    assert tft["BCD Forgone"] == 16500 and tft["IGST Forgone"] == 38970  # SWS inside BCD, as filed
    assert tft["Other Custom Duties Forgone"] == 0 and tft["Total Duty Forgone"] == 55470
    assert tft["Quantity of goods use for manufacturing goods that are exported"] == 3  # 3 of 4 units exported
    assert tft["Quantity of Goods Pending in stock in the importer’s premises (A)"] == 7  # incl. 1 in FG
    bat = rows[(1001, 4)]
    assert bat["Quantity of imported goods cleared on payment of duty"] == 2
    assert bat["Quantity of goods use for manufacturing goods that are exported"] == 3
    assert bat["Quantity of goods pending in stock (A+B+C)"] == 15
    cab = rows[(1001, 5)]
    assert cab["Quantity of goods use for manufacturing goods that are exported"] == 4  # accessory export -> V
    short = rows[(1002, 1)]
    assert (short["Quantity of Import"], short["Quantity Received at importer's permises"],
            short["Quantity Not Received at importer's permises"]) == (5, 4, 1)
    for r in rows.values():  # the identities that hold on every row of the filed return
        g = lambda h: r[h]  # noqa: E731
        assert g(H[13]) == g(H[16]) + g(H[17])
        assert g(H[24]) == g(H[20]) + g(H[21]) + g(H[22]) + g(H[23])
        assert g(H[25]) == g(H[16]) - g(H[18]) - g(H[19]) - g(H[24])
        assert g(H[28]) == g(H[25]) + g(H[26]) + g(H[27])
    cleared = f.sheets["Goods cleared on payment"]
    assert len(cleared) == 1 and cleared[0][6] == 2 and cleared[0][7] == "CH-9" and cleared[0][8] == "03-MAY-26"
    assert f.sheets["Re-Export"] == [] and not f.warnings


def test_rule_switches(scenario, session):
    config.set_rules(form3_item_export_as="re_export", form3_fg_stock_as="export", form3_sws_in_bcd_column=False)
    f = form3.build(session, Q_END)
    rows = _rows(f)
    tft = rows[(1001, 1)]
    assert tft["Quantity of goods use for manufacturing goods that are exported"] == 4  # all consumed
    assert tft["BCD Forgone"] == 15000 and tft["Other Custom Duties Forgone"] == 1500
    cab = rows[(1001, 5)]
    assert cab["Quantity of imported goods which are Re-Exported"] == 4
    (rx,) = f.sheets["Re-Export"]
    assert rx[7:] == ["5521907", "10-APR-26", "INBOM4", 1, 2, 4, 4]


def test_missing_be_data_and_duty_split_are_flagged(world, session):
    w = world
    w.import_("INV-X", date(2026, 4, 2), [w.line(w.tft, 1, 100, 50)])
    f = form3.build(session, Q_END)
    assert any("BE number" in m for m in f.warnings)
    assert any("not split" in m for m in f.warnings)
    (row,) = f.sheets["Details of Goods"]
    assert row[col["Other Custom Duties Forgone"]] == 50 and row[col["Total Duty Forgone"]] == 50


def test_xls_matches_template_layout(scenario, session):
    data = form3.to_xls(form3.build(session, Q_END))
    wb = xlrd.open_workbook(file_contents=data)
    assert wb.sheet_names() == list(form3.SHEETS)
    for name in form3.SHEETS:
        assert wb.sheet_by_name(name).row_values(0) == form3.HEADERS[name]
    sh = wb.sheet_by_name("Details of Goods")
    assert sh.cell(1, 0).ctype == xlrd.XL_CELL_NUMBER  # BE number numeric
    assert sh.cell(1, 1).ctype == xlrd.XL_CELL_TEXT  # BE date as DD-MMM-YY text
    assert sh.cell(1, 15).ctype == xlrd.XL_CELL_DATE  # receipt date as a real date


def test_quarter_helpers():
    assert form3.quarter_start(date(2025, 12, 31)) == date(2025, 10, 1)
    assert form3.quarter_end_for(date(2025, 11, 9)) == date(2025, 12, 31)
    assert "calendar Q4 2025 / FY 2025-26 Q3" in form3.quarter_label(date(2025, 12, 31))


def test_partial_receipt_limits_stock_but_debits_full_duty(world, session):
    w = world
    inv = imports.create_import(session, invoice_no="I", invoice_date=date(2026, 2, 1), supplier_id=w.sup.id,
                                bond_id=w.bond.id, lines=[dict(description="x", item_id=w.cab.id, qty=10,
                                                               qty_received=8, assessable_value=1000,
                                                               duty_saved_value=500)])
    imports.post_import(session, inv.id)
    lot = inv.lines[0]
    from eoubond.models import ImportLot
    l = session.query(ImportLot).one()
    assert (l.qty_received, l.dsv_total) == (D(8), D("400.00"))
    assert w.bond.balance == D(1_000_000) - D(500)
    assert lot.received == D(8)
