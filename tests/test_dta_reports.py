import io
from datetime import date

import pytest
from openpyxl import load_workbook

from eoubond import config
from eoubond.excel import reports_to_xlsx
from eoubond.models import BondLedgerEntry
from eoubond.services import dta, exports, production, reports
from eoubond.services.errors import BusinessError
from tests.conftest import D


def test_compute_duty_bcd_plus_sws_only():
    bcd, sws = dta.compute_duty(D("100000"), D("10"), D("10"))
    assert (bcd, sws) == (D("10000.00"), D("1000.00"))  # SWS is 10% of BCD, no GST


def test_dta_item_clearance_duty_and_recredit_on_payment(world, session):
    w = world
    # TFT has an item-specific BCD of 7.5%; battery uses the 10% default
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 10, 123457, 30000), w.line(w.bat, 10, 50000, 9000)])
    clr = dta.post_dta(session, clearance_no="DTA-1", clearance_date=date(2026, 3, 1), customer="AIIMS", lines=[
        dict(line_type="ITEM", item_id=w.tft.id, qty=1, sale_value=40000),
        dict(line_type="ITEM", item_id=w.bat.id, qty=2)])
    tft_line, bat_line = clr.lines
    # TFT: AV 12345.70 -> BCD 925.93 -> SWS 92.59 -> rounded 926 + 93
    assert (tft_line.assessable_value, tft_line.bcd, tft_line.sws, tft_line.duty) == \
        (D("12345.70"), D("926.00"), D("93.00"), D("1019.00"))
    # Battery: AV 10000 -> 1000 + 100
    assert (bat_line.bcd, bat_line.sws, bat_line.duty) == (D("1000.00"), D("100.00"), D("1100.00"))
    assert clr.total_duty == D("2119.00") and clr.status == "PENDING"
    assert session.query(BondLedgerEntry).filter_by(entry_type="DTA_CREDIT").count() == 0

    bal = w.bond.balance
    dta.mark_paid(session, clr.id, challan_no="CH-55", paid_date=date(2026, 3, 5))
    assert w.bond.balance == bal + D("3000.00") + D("1800.00")
    with pytest.raises(BusinessError):
        dta.mark_paid(session, clr.id, challan_no="CH-56", paid_date=date(2026, 3, 6))


def test_dta_rounding_can_be_switched_off(world, session):
    config.set_rules(round_duty_to_rupee=False)
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 10, 123457, 30000)])
    clr = dta.post_dta(session, clearance_no="DTA-1", clearance_date=date(2026, 3, 1),
                       lines=[dict(line_type="ITEM", item_id=w.tft.id, qty=1)])
    assert (clr.total_bcd, clr.total_sws) == (D("925.93"), D("92.59"))


def test_dta_of_finished_goods_traces_inputs(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 2, 200000, 1), w.line(w.csi, 2, 100000, 1),
                                          w.line(w.pcb, 4, 40000, 1), w.line(w.bat, 2, 20000, 1)])
    production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=2, production_date=date(2026, 3, 1))
    clr = dta.post_dta(session, clearance_no="DTA-1", clearance_date=date(2026, 3, 2),
                       lines=[dict(line_type="FG", model_id=w.m4343.id, qty=1)])
    line = clr.lines[0]
    # inputs AV for one unit: 100000 (7.5%) + 50000 + 20000 + 10000 (10%)
    assert line.assessable_value == D("180000.00")
    assert line.bcd == D("7500.00") + D("8000.00")
    assert line.sws == D("1550.00")
    rows = dta.computation_rows(session, clr)
    assert {r["item"] for r in rows} == {"TFT-4343", "CSI-4343", "PCB-RO", "BAT-LI"}


def test_dta_recredit_on_clearance_rule(world, session):
    config.set_rules(dta_recredit_on="clearance")
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.bat, 2, 2000, 400)])
    dta.post_dta(session, clearance_no="DTA-1", clearance_date=date(2026, 3, 1),
                 lines=[dict(line_type="ITEM", item_id=w.bat.id, qty=1)])
    assert session.query(BondLedgerEntry).filter_by(entry_type="DTA_CREDIT").one().credit == D("200.00")


def _activity(w, session):
    w.import_("INV-1", date(2026, 1, 15), [w.line(w.cab, 10, 1000, 1000)])
    w.import_("INV-2", date(2026, 2, 10), [w.line(w.cab, 10, 1000, 2000)])
    exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 2, 20),
                        lines=[dict(line_type="ITEM", item_id=w.cab.id, qty=15)])


def test_monthly_summary(world, session):
    w = world
    _activity(w, session)
    rep = reports.monthly_summary(session, w.bond.id)
    jan, feb = rep.rows
    assert (jan["month"], jan["opening"], jan["debits"], jan["credits"], jan["closing"]) == \
        ("2026-01", D(1_000_000), D(1000), D(0), D(999_000))
    assert (feb["opening"], feb["debits"], feb["credits"], feb["closing"]) == \
        (D(999_000), D(2000), D(2000), D(999_000))


def test_ledger_running_balance_in_date_order(world, session):
    w = world
    _activity(w, session)
    rep = reports.bond_ledger(session, w.bond.id)
    assert [r["balance"] for r in rep.rows] == [D(999000), D(997000), D(999000)]
    rep2 = reports.bond_ledger(session, w.bond.id, date_from=date(2026, 2, 1))
    assert len(rep2.rows) == 2 and "opening 999,000.00" in rep2.subtitle


def test_stock_by_bond_and_excel_export(world, session):
    w = world
    _activity(w, session)
    rep = reports.stock_by_bond(session)
    (row,) = rep.rows
    assert (row["rm_qty"], row["rm_dsv"]) == (D(5), D("1000.00"))
    data = reports_to_xlsx(rep, reports.bond_ledger(session, w.bond.id), reports.monthly_summary(session),
                           reports.pending_dta_duty(session), reports.audit_trail(session))
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames[0] == "Stock by bond"
    ws = wb["Stock by bond"]
    assert ws.cell(row=4, column=1).value == "Bond" and ws.cell(row=5, column=2).value == "CAB-TETHER"


def test_pending_dta_report(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.bat, 2, 2000, 400)])
    dta.post_dta(session, clearance_no="DTA-1", clearance_date=date(2026, 3, 1),
                 lines=[dict(line_type="ITEM", item_id=w.bat.id, qty=1)])
    rep = reports.pending_dta_duty(session)
    assert rep.rows[0]["duty"] == D("110.00") and rep.totals["duty"] == D("110.00")
