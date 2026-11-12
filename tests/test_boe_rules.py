"""Bill of Entry duty sources and bond reconciliation rules, plus an optional
check over a whole folder of real BEs.

Folder check: set EOU_BOE_SAMPLES to a folder of ICEGATE BE PDFs, e.g.
    set EOU_BOE_SAMPLES=C:\\path\\to\\bill-of-entry-pdfs
Every BE must extract without warnings and reconcile to its bond debit. Client
PDFs stay in their own folder - they are not copied into this project.
"""
import os
from pathlib import Path

import pytest

from eoubond.extract.boe_parser import BoeItem, BoeResult, _bond_check, extract_bill_of_entry


def item(n, **kw):
    return BoeItem(invsno=1, itemsn=n, assessable_value=kw.pop("av", 1000.0), **kw)


def test_duty_sources_in_priority_order():
    eou = item(1, bcd_rate=10, bcd_amt_fg=110.0, oth_amt_fg=198.0, amt_bcd=0, fg_bcd=100.0)
    assert (eou.duty_source, eou.bcd, eou.sws, eou.igst) == (BoeItem.G, 100.0, 10.0, 198.0)
    wh = item(2, amt_bcd=75.0, amt_sws=7.5, amt_igst=194.85, amt_cess=50.0)
    assert (wh.duty_source, wh.bcd, wh.sws, wh.igst, wh.cess, wh.total_duty_fg) == \
        (BoeItem.ASSESSED, 75.0, 7.5, 194.85, 50.0, 327.35)
    ex = item(3, bcd_rate=0, amt_bcd=0, fg_bcd_sws=110.0, fg_bcd=100.0, fg_igst=72.0)
    assert (ex.duty_source, ex.bcd, ex.sws, ex.igst) == (BoeItem.EXEMPT, 100.0, 10.0, 72.0)
    assert item(4).duty_source is None and item(4).total_duty_fg is None


def _res(bonds, items, be_type="W"):
    return BoeResult(source_filename="x", be_type=be_type, items=items,
                     bonds=[dict(bond_no=f"B{i}", bond_code=c, debit_amount=d) for i, (c, d) in enumerate(bonds)])


def test_bond_check_rules():
    a = item(1, amt_bcd=100.0, amt_sws=10.0, amt_igst=200.0)  # assessed duty 310
    e = item(2, fg_bcd_sws=55.0, fg_bcd=50.0, fg_igst=40.0)  # exempt: BCD+SWS 55, IGST 40
    assert _bond_check(_res([("WH", 930)], [a]))["ok"]  # 3 x 310
    assert not _bond_check(_res([("WH", 310)], [a]))["ok"]
    r = _bond_check(_res([("RE", 55), ("WH", 930)], [a, e]))  # RE bonds BCD + SWS only
    assert r["ok"] and "RE bond 55 = BCD + SWS forgone" in r["text"] and r["duty"] == 405.0
    eou = item(3, bcd_amt_fg=110.0, oth_amt_fg=198.0)
    assert _bond_check(_res([("EI", 308)], [eou], be_type="H"))["ok"]


SAMPLES = os.environ.get("EOU_BOE_SAMPLES")


@pytest.mark.skipif(not SAMPLES, reason="set EOU_BOE_SAMPLES to a folder of BE PDFs")
@pytest.mark.parametrize("pdf", sorted(Path(SAMPLES).glob("*.pdf")) if SAMPLES else [], ids=lambda p: p.name)
def test_real_bill_of_entry_reconciles(pdf):
    r = extract_bill_of_entry(pdf.name, pdf.read_bytes())
    assert r.success and not r.warnings, r.errors + r.warnings
    assert r.bond_check and r.bond_check["ok"], r.bond_check
    for it in r.items:
        assert it.qty and it.uqc and it.assessable_value and it.cth and it.description and it.duty_source
    assert {i.sno for i in r.invoices} == {it.invsno for it in r.items}
    if r.total_assessable_value is not None:
        assert abs(sum(it.assessable_value for it in r.items) - r.total_assessable_value) <= 1 + len(r.items)
    if "OOC" in pdf.name.upper():
        assert r.ooc_date is not None
