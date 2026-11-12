from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from eoubond.models import ImportLot
from eoubond.services.errors import InsufficientStock
from eoubond.services.stock import draw_import_lots, fifo_split, share
from tests.conftest import D


def test_fifo_split_pure():
    takes, short = fifo_split([D(5), D(3), D(10)], D(9))
    assert takes == [D(5), D(3), D(1)] and short == 0
    takes, short = fifo_split([D(2), D(1)], D(5))
    assert takes == [D(2), D(1)] and short == D(2)


def test_share_is_exact_when_emptying():
    assert share(D("100.00"), D(3), D(1)) == D("33.33")
    assert share(D("66.67"), D(2), D(2)) == D("66.67")  # remainder taken exactly


def test_fifo_takes_oldest_lot_first_and_splits(world, session):
    w = world
    w.import_("INV-2", date(2026, 3, 1), [w.line(w.tft, 10, 20000, 3000)])
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 4, 8000, 1000)])  # older date, entered later

    takes = draw_import_lots(session, w.tft.id, D(6), date(2026, 4, 1))
    assert [(t.lot.lot_date, t.qty) for t in takes] == [(date(2026, 2, 1), D(4)), (date(2026, 3, 1), D(2))]
    assert takes[0].dsv == D("1000.00")          # first lot emptied: exact
    assert takes[1].dsv == D("600.00")           # 2/10 of 3000
    lots = session.query(ImportLot).order_by(ImportLot.lot_date).all()
    assert [l.qty_available for l in lots] == [D(0), D(8)]
    assert lots[1].dsv_available == D("2400.00")


def test_value_never_drifts_over_many_small_draws(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 3, 1000, 100)])
    vals = [draw_import_lots(session, w.tft.id, D(1), date(2026, 2, 2))[0].dsv for _ in range(3)]
    assert sum(vals) == D("100.00")


def test_lot_dated_after_use_is_not_eligible(world, session):
    w = world
    w.import_("INV-1", date(2026, 5, 1), [w.line(w.tft, 5, 1000, 100)])
    with pytest.raises(InsufficientStock):
        draw_import_lots(session, w.tft.id, D(1), date(2026, 4, 30))


def test_insufficient_stock_raises(world, session):
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 2, 1000, 100)])
    with pytest.raises(InsufficientStock) as exc:
        draw_import_lots(session, w.tft.id, D(3), date(2026, 3, 1))
    assert exc.value.shortages[0]["shortfall"] == D(1)


def test_database_check_blocks_negative_stock(world, session):
    """Even bypassing the service layer, the DB refuses over-consumption."""
    w = world
    w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 2, 1000, 100)])
    lot = session.query(ImportLot).one()
    lot.qty_available = D(-1)
    with pytest.raises(IntegrityError):
        session.flush()
