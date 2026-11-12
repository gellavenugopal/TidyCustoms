from datetime import date

import pytest

from eoubond.models import BondLedgerEntry, ConsumptionAllocation, ImportLot
from eoubond.services import exports, masters, production
from eoubond.services.errors import InsufficientStock
from tests.conftest import D


def _stock(w, bond=None, n=10, no="INV-1", d=date(2026, 2, 1)):
    w.import_(no, d, [w.line(w.tft, n, n * 20000, n * 5000), w.line(w.csi, n, n * 10000, n * 2500),
                      w.line(w.pcb, 2 * n, n * 4000, n * 1000), w.line(w.bat, 2 * n, n * 2000, n * 500)],
              bond=bond)


def test_production_consumes_bom_fifo_and_creates_fg(world, session):
    w = world
    _stock(w)
    b = production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=4, production_date=date(2026, 3, 1))
    # per unit: 5000 + 2500 + 2*500 (pcb is 1000 per 2 pcs) + 500 (bat 500/2pcs*1) ... compute from lots:
    # tft 4/10*50000=20000, csi 4/10*25000=10000, pcb 8/20*10000=4000, bat 4/20*5000=1000
    assert b.total_dsv == D("35000.00")
    assert b.fg_lot.qty_available == D(4)
    assert {a.item.code: a.qty for a in b.allocations} == {"TFT-4343": D(4), "CSI-4343": D(4),
                                                           "PCB-RO": D(8), "BAT-LI": D(4)}
    memo = session.query(BondLedgerEntry).filter_by(entry_type="CONSUMPTION_MEMO").one()
    assert memo.memo_value == D("35000.00") and memo.debit == 0 and memo.credit == 0


def test_production_spans_lots_across_imports_and_bonds(world, session):
    w = world
    _stock(w, n=2, no="INV-A", d=date(2026, 2, 1))
    _stock(w, bond=w.bond2, n=10, no="INV-B", d=date(2026, 2, 15))
    b = production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=3, production_date=date(2026, 3, 1))
    tft = [a for a in b.allocations if a.item_id == w.tft.id]
    assert [(a.bond.bond_no, a.qty) for a in tft] == [("B-2026-01", D(2)), ("B-2026-02", D(1))]
    memos = session.query(BondLedgerEntry).filter_by(entry_type="CONSUMPTION_MEMO").count()
    assert memos == 2


def test_production_shortage_is_atomic(world, session):
    w = world
    _stock(w, n=2)
    before = [(l.id, l.qty_available) for l in session.query(ImportLot)]
    with pytest.raises(InsufficientStock) as exc:
        production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=3, production_date=date(2026, 3, 1))
    # 2 TFT/CSI and 4 PCB in stock; 3 units need 3/3/6 - battery (4 for 3) is fine
    assert {s["label"] for s in exc.value.shortages} == {"TFT-4343", "CSI-4343", "PCB-RO"}
    assert [(l.id, l.qty_available) for l in session.query(ImportLot)] == before


def test_bom_version_chosen_by_effective_date(world, session):
    w = world
    masters.create_bom_version(session, model_id=w.m4343.id, effective_from=date(2026, 6, 1), lines=[
        dict(item_id=w.tft.id, qty_per_unit=1), dict(item_id=w.csi.id, qty_per_unit=1)])
    _stock(w)
    old = production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=1, production_date=date(2026, 5, 31))
    new = production.produce(session, batch_no="P2", model_id=w.m4343.id, qty_units=1, production_date=date(2026, 6, 1))
    assert (old.bom.version, len(old.allocations)) == (1, 4)
    assert (new.bom.version, len(new.allocations)) == (2, 2)


def test_export_fg_recredits_consumed_value(world, session):
    w = world
    _stock(w)
    production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=4, production_date=date(2026, 3, 1))
    bal_before = w.bond.balance
    sh = exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 10), lines=[
        dict(line_type="FG", model_id=w.m4343.id, qty=3, fob_value=900000)])
    assert sh.total_recredit == D("26250.00")  # 3/4 of 35000
    assert w.bond.balance == bal_before + D("26250.00")
    e = session.query(BondLedgerEntry).filter_by(entry_type="EXPORT_CREDIT").one()
    assert (e.source_type, e.source_id, e.credit) == ("EXPORT", sh.id, D("26250.00"))
    # last unit takes the exact remainder
    sh2 = exports.post_export(session, invoice_no="EX-2", export_date=date(2026, 3, 11), lines=[
        dict(line_type="FG", model_id=w.m4343.id, qty=1)])
    assert sh.total_recredit + sh2.total_recredit == D("35000.00")
    assert all(c.dsv_remaining == 0 for c in session.query(ConsumptionAllocation))


def test_export_fifo_across_fg_lots(world, session):
    w = world
    _stock(w, n=2, no="INV-A")                     # cheap first lot
    w.import_("INV-B", date(2026, 2, 10), [w.line(w.tft, 5, 1, 9000), w.line(w.csi, 5, 1, 1),
                                           w.line(w.pcb, 10, 1, 1), w.line(w.bat, 10, 1, 1)])
    p1 = production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=2, production_date=date(2026, 3, 1))
    p2 = production.produce(session, batch_no="P2", model_id=w.m4343.id, qty_units=2, production_date=date(2026, 3, 2))
    sh = exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 10), lines=[
        dict(line_type="FG", model_id=w.m4343.id, qty=3)])
    assert p1.fg_lot.qty_available == 0 and p2.fg_lot.qty_available == 1
    assert sh.total_recredit == p1.total_dsv + p2.total_dsv / 2


def test_accessory_export_draws_import_lots_and_named(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.cab, 10, 5000, 1000), w.line(w.bat, 4, 4000, 800)])
    sh = exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 1), lines=[
        dict(line_type="ITEM", item_id=w.cab.id, qty=5, export_item_name="Tether cable 10m (spare)"),
        dict(line_type="ITEM", item_id=w.bat.id, qty=1)])
    assert [l.export_item_name for l in sh.lines] == ["Tether cable 10m (spare)", "Li-ion battery pack"]
    assert [l.dsv_recredited for l in sh.lines] == [D("500.00"), D("200.00")]
    assert w.bond.balance == D(1_000_000) - D(1800) + D(700)


def test_export_credit_split_per_bond(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.cab, 1, 100, 100)])
    w.import_("INV-2", date(2026, 2, 2), [w.line(w.cab, 1, 100, 300)], bond=w.bond2)
    exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 1),
                        lines=[dict(line_type="ITEM", item_id=w.cab.id, qty=2)])
    credits = {e.bond.bond_no: e.credit for e in session.query(BondLedgerEntry).filter_by(entry_type="EXPORT_CREDIT")}
    assert credits == {"B-2026-01": D("100.00"), "B-2026-02": D("300.00")}


def test_export_more_than_stock_rejected(world, session):
    w = world
    _stock(w, n=2)
    production.produce(session, batch_no="P1", model_id=w.m4343.id, qty_units=1, production_date=date(2026, 3, 1))
    with pytest.raises(InsufficientStock):
        exports.post_export(session, invoice_no="EX-1", export_date=date(2026, 3, 5),
                            lines=[dict(line_type="FG", model_id=w.m4343.id, qty=2)])
