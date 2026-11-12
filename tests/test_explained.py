"""The formula workbooks: recalculate them with the `formulas` engine and check
every computed figure against the tracker's own numbers."""
from datetime import date

import pytest

formulas = pytest.importorskip("formulas")

from eoubond.explained_xlsx import bond_workbook, form3_workbook  # noqa: E402
from eoubond.services import dta, exports, form3, imports, production, reports  # noqa: E402
from tests.test_form3 import _imp, _line  # noqa: E402


def calc(tmp_path, blob, name="w.xlsx"):
    p = tmp_path / name
    p.write_bytes(blob)
    sol = formulas.ExcelModel().loads(str(p)).finish().calculate()
    vals = {}
    for k, v in sol.items():
        if "!" not in k or ":" in k.split("!")[1]:
            continue
        sheet, cell = k.split("]", 1)[1].split("!")
        vals[(sheet.strip("'").upper(), cell.upper())] = v.value[0][0] if hasattr(v, "value") else v
    return vals


def num(x):
    return float(x) if x not in (None, "") else 0.0


@pytest.fixture
def busy(world, session):
    w, s = world, session
    _imp(w, s, "INV-A", "1001", date(2026, 2, 1), [
        _line(w.tft, 10, 200000, 15000, 1500, 38970), _line(w.csi, 10, 100000, 10000, 1000, 19980),
        _line(w.pcb, 20, 40000, 4000, 400, 7992), _line(w.bat, 20, 20000, 2000, 200, 3996),
        _line(w.cab, 10, 5000, 500, 50, 999, qty_received=8)])
    production.produce(s, batch_no="P1", model_id=w.m4343.id, qty_units=4, production_date=date(2026, 3, 1))
    exports.post_export(s, invoice_no="EX-1", export_date=date(2026, 4, 10), lines=[
        dict(line_type="FG", model_id=w.m4343.id, qty=3), dict(line_type="ITEM", item_id=w.cab.id, qty=4)])
    paid = dta.post_dta(s, clearance_no="DTA-1", clearance_date=date(2026, 5, 1),
                        lines=[dict(line_type="ITEM", item_id=w.bat.id, qty=2)])
    dta.mark_paid(s, paid.id, challan_no="CH-1", paid_date=date(2026, 5, 3))
    dta.post_dta(s, clearance_no="DTA-2", clearance_date=date(2026, 5, 20),  # stays pending
                 lines=[dict(line_type="FG", model_id=w.m4343.id, qty=1)])
    _imp(w, s, "INV-B", "1002", date(2026, 5, 15), [_line(w.tft, 5, 100000, 7500, 750, 19485)], )
    s.flush()
    return w


def test_bond_workbook_formulas_reproduce_tracker(busy, session, tmp_path):
    w = busy
    v = calc(tmp_path, bond_workbook(session))
    # Bonds: balance by formula agrees with the tracker, and utilisation is fully explained
    assert v[("BONDS", "G2")] == pytest.approx(float(w.bond.balance))
    assert v[("BONDS", "I2")] == "✓ agrees"
    assert v[("BONDS", "N2")] == pytest.approx(0, abs=0.01)  # unexplained = 0
    assert num(v[("BONDS", "L2")]) > 0 and num(v[("BONDS", "M2")]) > 0  # pending DTA and short receipt both present
    # Monthly summary closing == report
    rep = reports.monthly_summary(session, w.bond.id)
    for i, row in enumerate(rep.rows, start=2):
        assert v[("MONTHLY SUMMARY", f"B{i}")] == row["month"]
        assert v[("MONTHLY SUMMARY", f"H{i}")] == pytest.approx(float(row["closing"]))
    # Stock by bond RM duty saved == report
    st = {(r["bond"], r["item"]): r for r in reports.stock_by_bond(session).rows}
    i = 2
    while ("STOCK BY BOND", f"A{i}") in v and v[("STOCK BY BOND", f"A{i}")] != "Total":
        key = (v[("STOCK BY BOND", f"A{i}")], v[("STOCK BY BOND", f"B{i}")])
        assert v[("STOCK BY BOND", f"G{i}")] == pytest.approx(float(st[key]["rm_dsv"]))
        assert v[("STOCK BY BOND", f"I{i}")] == pytest.approx(float(st[key]["fg_dsv"]))
        i += 1
    # DTA lines: formula duty == tracker duty on every line
    i = 2
    while ("DTA LINES", f"L{i}") in v and v[("DTA LINES", f"L{i}")] not in (None, ""):
        assert v[("DTA LINES", f"L{i}")] == "✓", i
        i += 1


def test_form3_working_formulas_match_filing_values(busy, session, tmp_path):
    f = form3.build(session, date(2026, 6, 30))
    v = calc(tmp_path, form3_workbook(f), "f3.xlsx")
    from openpyxl.utils import get_column_letter as col
    for n, row in enumerate(f.sheets["Details of Goods"], start=3):
        for idx in (12, 17, 18, 19, 20, 21, 24, 25, 28):  # the formula columns M, R, S, T, U, V, Y, Z, AC
            assert num(v[("DETAILS OF GOODS", f"{col(idx + 1)}{n}")]) == pytest.approx(float(row[idx])), \
                (n, form3.DETAILS_HEADERS[idx])
