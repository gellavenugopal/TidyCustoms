"""Offline extraction of ICEGATE Bill of Entry PDFs (EOU / Section 65 BEs).

Same approach as the Shipping Bill Extractor: read the PDF's own text layer
with PyMuPDF, drop the diagonal "ASSESSED COPY" watermark and the vertical
section labels by line angle (pdf_utils.get_clean_lines), rebuild geometric
rows, and anchor every value on the template's fixed field labels.

Where each field comes from (ICEGATE BE print template):
  every page header   Port Code / BE No / BE Date / BE Type
  Part I  F. PAYMENT  1.BOND NO. 2.PORT 3.BOND CD 4.DEBT AMT   (bond debit = total duty forgone)
  Part I  I. events   OOC date (when the BE has been given out of charge)
  Part II A. INVOICE  1.S.NO 2.INVOICE NO. & DT.  ;  3.SUPPLIER NAME ; 14.Cur
  Part III per item   1.INVSNO 2.ITEMSN 3.CTH 4.CETH 5.ITEM DESCRIPTION
                      13.C.QTY 14.C.UQC ; 29.ASSESS VALUE ; DUTY "Rate" row (BCD, SWS ...)
  Part IV  G. SCHEME NOTIFICATION AND DUTY FOREGONE: 8.BCD AMT FG (BCD + SWS), 9.OTH AMT FG (IGST etc.)

Checks: the per-item duty forgone must add up to the bond debit printed in
Part I, and the per-item assessable values to "18.TOT.ASS VAL". A mismatch is
reported as a warning so the extraction is never silently wrong.

"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import fitz  # PyMuPDF

from .pdf_utils import get_clean_lines

NUM = r"(?:\d[\d,]*\.?\d*|\.\d+)"
DATE_DMY = re.compile(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b")  # 09/11/2026, 27-07-2024 (OOC copies)
DATE_DMONY = re.compile(r"\b(\d{2}-[A-Z]{3}-\d{2,4})\b", re.I)


@dataclass
class BoeInvoice:
    sno: int
    invoice_no: str | None = None
    invoice_date: date | None = None
    supplier: str | None = None
    currency: str | None = None
    amount: float | None = None


@dataclass
class BoeItem:
    invsno: int
    itemsn: int
    cth: str | None = None
    description: str = ""
    qty: float | None = None
    uqc: str | None = None
    unit_price: float | None = None
    assessable_value: float | None = None
    bcd_rate: float | None = None
    sws_rate: float | None = None
    igst_rate: float | None = None
    bcd_amt_fg: float | None = None  # Part IV-G: BCD + SWS forgone (EOU / exemption BEs)
    oth_amt_fg: float | None = None  # Part IV-G: IGST (+ other) forgone
    # Part III DUTY "Amount" row: assessed duty - on a warehousing BE (type W, MOOWR /
    # Section 59-65) this is the duty deferred under bond
    amt_bcd: float | None = None
    amt_sws: float | None = None
    amt_igst: float | None = None
    amt_cess: float | None = None
    amt_other: float | None = None
    # Part III "Duty Fg" rows: duty forgone under an exemption notification
    # (first DUTY block: BCD column = BCD + SWS forgone, IGST column = IGST forgone;
    #  second block: BCD forgone alone)
    fg_bcd_sws: float | None = None
    fg_bcd: float | None = None
    fg_igst: float | None = None
    fg_cess: float | None = None

    G, ASSESSED, EXEMPT = "Part IV-G duty forgone", "Part III assessed duty", "Part III duty forgone (exemption)"

    @property
    def duty_source(self) -> str | None:
        if self.bcd_amt_fg is not None or self.oth_amt_fg is not None:
            return self.G
        if any((self.amt_bcd, self.amt_sws, self.amt_igst, self.amt_cess, self.amt_other)):
            return self.ASSESSED
        if any((self.fg_bcd_sws, self.fg_bcd, self.fg_igst, self.fg_cess)):
            return self.EXEMPT
        return None

    def _split(self, bcd_sws: float | None, bcd_only: float | None) -> tuple[float | None, float | None]:
        """BCD and SWS from a combined BCD+SWS figure."""
        if bcd_sws is None:
            return bcd_only, None
        if bcd_only:
            bcd = min(bcd_only, bcd_sws)
        elif self.bcd_rate and self.assessable_value is not None:
            bcd = min(round(self.assessable_value * self.bcd_rate / 100, 2), bcd_sws)
        else:
            return bcd_sws, 0.0
        return bcd, round(bcd_sws - bcd, 2)

    @property
    def bcd(self) -> float | None:
        src = self.duty_source
        if src == self.ASSESSED:
            return self.amt_bcd or 0.0
        if src == self.G:
            return None if self.bcd_amt_fg is None else self._split(self.bcd_amt_fg, self.fg_bcd)[0]
        if src == self.EXEMPT:
            return self._split(self.fg_bcd_sws, self.fg_bcd)[0]
        return None

    @property
    def sws(self) -> float | None:
        src = self.duty_source
        if src == self.ASSESSED:
            return self.amt_sws or 0.0
        if src == self.G:
            return None if self.bcd_amt_fg is None else self._split(self.bcd_amt_fg, self.fg_bcd)[1]
        if src == self.EXEMPT:
            return self._split(self.fg_bcd_sws, self.fg_bcd)[1]
        return None

    @property
    def igst(self) -> float | None:
        src = self.duty_source
        if src == self.ASSESSED:
            return self.amt_igst or 0.0
        if src == self.EXEMPT:
            return self.fg_igst
        return self.oth_amt_fg

    @property
    def cess(self) -> float | None:
        src = self.duty_source
        if src == self.ASSESSED:
            return self.amt_cess or None
        return (self.fg_cess or None) if src == self.EXEMPT else None

    @property
    def other(self) -> float | None:
        return (self.amt_other or None) if self.duty_source == self.ASSESSED else None

    @property
    def total_duty_fg(self) -> float | None:
        if self.duty_source is None:
            return None
        return round(sum(v or 0 for v in (self.bcd, self.sws, self.igst, self.cess, self.other)), 2)


@dataclass
class BoeResult:
    source_filename: str
    success: bool = False
    port_code: str | None = None
    be_no: str | None = None
    be_date: date | None = None
    be_type: str | None = None
    bonds: list[dict] = field(default_factory=list)
    ooc_date: date | None = None
    exchange_rates: dict[str, float] = field(default_factory=dict)
    total_assessable_value: float | None = None
    invoices: list[BoeInvoice] = field(default_factory=list)
    items: list[BoeItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bond_check: dict | None = None  # see _bond_check

    @property
    def bond_debit(self) -> float | None:
        vals = [b["debit_amount"] for b in self.bonds if b.get("debit_amount") is not None]
        return sum(vals) if vals else None

    def invoice(self, sno: int) -> BoeInvoice | None:
        return next((i for i in self.invoices if i.sno == sno), None)


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.title() if "-" in s and s[3:6].isalpha() else s, fmt).date()
        except ValueError:
            continue
    return None


class _Page:
    """Geometric rows of one page: [(top, [line, ...] sorted by x)]."""

    def __init__(self, page: "fitz.Page"):
        rows: dict[float, list[dict]] = defaultdict(list)
        for l in get_clean_lines(page):
            rows[round(l["top"], 0)].append(l)
        self.rows = [(t, sorted(rows[t], key=lambda l: l["x0"])) for t in sorted(rows)]
        self.text = page.get_text("text")

    @staticmethod
    def join(lines) -> str:
        return re.sub(r"\s+", " ", " ".join(l["text"] for l in lines)).strip()

    def row_text(self, i: int) -> str:
        return self.join(self.rows[i][1])

    def find(self, needle: str, start: int = 0) -> int | None:
        for i in range(start, len(self.rows)):
            if needle in self.row_text(i):
                return i
        return None

    def label_x(self, i: int, label: str) -> float | None:
        for l in self.rows[i][1]:
            if label in l["text"]:
                return l["x0"]
        return None


# ------------------------------------------------------------------ sections
def _header(p: _Page, r: BoeResult):
    i = p.find("BE No")
    if i is None or i + 1 >= len(p.rows):
        return
    m = re.search(r"\b([A-Z]{5}\d)\s+(\d{5,8})\s+(\d{2}/\d{2}/\d{4})(?:\s+([A-Z]{1,2}))?", p.row_text(i + 1))
    if m:
        r.port_code, r.be_no, r.be_date, r.be_type = m.group(1), m.group(2), parse_date(m.group(3)), m.group(4)


def _part1(p: _Page, r: BoeResult):
    i = p.find("1.BOND NO.")
    if i is not None:
        split_x = p.label_x(i, "1.SR NO") or 340
        for j in range(i + 1, min(i + 6, len(p.rows))):
            left = [l for l in p.rows[j][1] if l["x0"] < split_x - 5]
            toks = p.join(left).split()
            if len(toks) >= 2 and re.match(r"^[A-Z0-9/]{5,}$", toks[0]) and "WBE" not in toks[0]:
                nums = [t for t in toks[1:] if re.fullmatch(NUM, t)]
                r.bonds.append(dict(bond_no=toks[0], port=toks[1] if len(toks) > 1 else None,
                                    bond_code=toks[2] if len(toks) > 2 and not re.fullmatch(NUM, toks[2]) else None,
                                    debit_amount=_num(nums[0]) if nums else None))
            elif r.bonds or "WBE" in p.row_text(j):
                break
    for j in range(len(p.rows)):
        t = p.row_text(j)
        if t.startswith("OOC ") and not r.ooc_date:
            m = DATE_DMONY.search(t) or DATE_DMY.search(t)
            if m:
                r.ooc_date = parse_date(m.group(1).upper())
        if t.startswith("OOC DATE") and not r.ooc_date:
            m = DATE_DMY.search(t) or DATE_DMONY.search(t)
            if m:
                r.ooc_date = parse_date(m.group(1))
        for cur, rate in re.findall(r"1\s*([A-Z]{3})\s*=\s*(" + NUM + r")\s*INR", t):
            r.exchange_rates[cur] = float(rate)
        if "TOT.ASS VAL" in t and j + 1 < len(p.rows):
            nums = re.findall(NUM, p.row_text(j + 1))
            if nums:
                r.total_assessable_value = _num(nums[-1])


def _part2(p: _Page, r: BoeResult):
    i = p.find("2.INVOICE NO. & DT.")
    if i is None or i + 1 >= len(p.rows):
        return
    po_x = p.label_x(i, "3.PURCHASE ORDER") or 185
    left = lambda j: p.join([l for l in p.rows[j][1] if l["x0"] < po_x - 5])  # noqa: E731
    m = re.match(r"^(\d{1,3})\s+(\S+)(?:\s+(\S+))?", left(i + 1))
    if not m:
        return
    inv = BoeInvoice(sno=int(m.group(1)), invoice_no=m.group(2))
    for j in range(i + 1, min(i + 4, len(p.rows))):
        d = DATE_DMONY.search(left(j)) or DATE_DMY.search(left(j))
        if d:
            inv.invoice_date = parse_date(d.group(1).upper() if "-" in d.group(1) else d.group(1))
            break
    k = p.find("3.SUPPLIER NAME", i)
    if k is not None and k + 1 < len(p.rows):
        third_x = p.label_x(k, "4.THIRD PARTY") or 300
        inv.supplier = p.join([l for l in p.rows[k + 1][1] if l["x0"] < third_x - 10]) or None
    k = p.find("1.INV VALUE", i)
    if k is not None and k + 1 < len(p.rows):
        nums = re.findall(NUM, p.row_text(k + 1))
        inv.amount = _num(nums[0]) if nums else None
    k = p.find("14.Cur", i)
    if k is not None:
        m2 = re.search(r"14\.Cur\s+([A-Z]{3})", p.row_text(k))
        inv.currency = m2.group(1) if m2 else None
    if not r.invoice(inv.sno):
        r.invoices.append(inv)


_ITEM_START = re.compile(r"^(\d{1,3})\s+(\d{1,4})\s+(\d{8})\s+(\S+)\s*(.*)$")
_FLAGS_TAIL = re.compile(r"(?:\s+[YN]){1,5}\s*$")


def _part3(p: _Page, r: BoeResult):
    i = 0
    while i < len(p.rows):
        if "1.INVSNO" not in p.row_text(i) or "2.ITEMSN" not in p.row_text(i) or i + 1 >= len(p.rows):
            i += 1
            continue
        m = _ITEM_START.match(p.row_text(i + 1))
        if not m:
            i += 1
            continue
        it = BoeItem(invsno=int(m.group(1)), itemsn=int(m.group(2)), cth=m.group(3))
        desc = [_FLAGS_TAIL.sub("", m.group(5))]
        j = i + 2
        while j < len(p.rows) and "11.UPI" not in p.row_text(j) and "1.INVSNO" not in p.row_text(j):
            desc.append(p.row_text(j))
            j += 1
        it.description = re.sub(r"\s+", " ", " ".join(desc)).strip()
        end = p.find("1.INVSNO", j) or len(p.rows)
        if j + 1 < len(p.rows) and "11.UPI" in p.row_text(j):
            q = re.match(rf"^({NUM})\s+([A-Z]{{2}})?\s*({NUM})\s+([A-Z]{{2,4}})\b", p.row_text(j + 1))
            if q:
                it.unit_price, it.qty, it.uqc = _num(q.group(1)), _num(q.group(3)), q.group(4)
        k = p.find("29.ASSESS VALUE", j)
        if k is not None and k < end and k + 1 < len(p.rows):
            nums = re.findall(NUM, p.row_text(k + 1))
            if len(nums) >= 2:
                it.assessable_value = _num(nums[-2])
        k = p.find("1. BCD", j)
        if k is not None and k < end:
            labels = {key: p.label_x(k, lab) for key, lab in
                      (("bcd", "1. BCD"), ("sws", "3.SWS"), ("igst", "5.IGST"), ("cvd", "2.CVD"),
                       ("sad", "4.SAD"), ("cess", "6. G. CESS"), ("add", "7.ADD"))}
            labels = {a: x for a, x in labels.items() if x is not None}
            def values(prefix):
                n = next((n for n in range(k + 1, min(k + 7, len(p.rows))) if p.row_text(n).startswith(prefix)), None)
                out = {}
                if n is not None and labels:
                    for l in p.rows[n][1]:
                        if re.fullmatch(NUM, l["text"].strip()):
                            col = min(labels, key=lambda a: abs(labels[a] - l["x0"]))
                            out[col] = out.get(col, 0) + _num(l["text"].strip())
                return out
            rates = values("Rate")
            it.bcd_rate, it.sws_rate, it.igst_rate = rates.get("bcd"), rates.get("sws"), rates.get("igst")
            amts = values("Amount")
            it.amt_bcd, it.amt_sws, it.amt_igst = amts.get("bcd"), amts.get("sws"), amts.get("igst")
            it.amt_cess = amts.get("cess")
            other = sum(amts.get(c, 0) for c in ("cvd", "sad", "add"))
            it.amt_other = other or None
            fg = values("Duty Fg")
            it.fg_bcd_sws, it.fg_igst, it.fg_cess = fg.get("bcd"), fg.get("igst"), fg.get("cess")
            k2 = p.find("1.SP EXD", k + 1)  # second DUTY block: its "Duty Fg" row carries BCD forgone alone
            if k2 is not None and k2 < end:
                # its "Amount" row holds the other cesses (Health cess, AIDC, ...): add them to cess
                na = next((n for n in range(k2 + 1, min(k2 + 7, len(p.rows))) if p.row_text(n).startswith("Amount")), None)
                if na is not None:
                    extra = sum(_num(l["text"].strip()) for l in p.rows[na][1] if re.fullmatch(NUM, l["text"].strip()))
                    if extra:
                        it.amt_cess = (it.amt_cess or 0) + extra
                n2 = next((n for n in range(k2 + 1, min(k2 + 7, len(p.rows))) if p.row_text(n).startswith("Duty Fg")), None)
                if n2 is not None:
                    # printed under the block's 5th column (label "5.CAIDC"), left of 6.EAIDC
                    lo = (p.label_x(k2, "4.CESS") or 254) + 20
                    hi = p.label_x(k2, "6.EAIDC") or 358
                    vals = [_num(l["text"].strip()) for l in p.rows[n2][1]
                            if re.fullmatch(NUM, l["text"].strip()) and lo <= l["x0"] < hi]
                    it.fg_bcd = vals[0] if vals else None
        r.items.append(it)
        i = end if end > i else i + 1


def _part4_duty_fg(pages: list[_Page]) -> dict[tuple[int, int], tuple[float, float]]:
    out, active = {}, False
    for p in pages:
        for n in range(len(p.rows)):
            t = p.row_text(n)
            if "G. SCHEME NOTIFICATION" in t:
                active = True
                continue
            if active and ("H. CERTIFICATE" in t or t.startswith("PART -")):
                active = False
            if not active:
                continue
            m = re.match(rf"^(\d{{1,3}})\s+(\d{{1,4}})\s+.*?({NUM})\s+({NUM})$", t)
            if m:
                out[(int(m.group(1)), int(m.group(2)))] = (_num(m.group(3)), _num(m.group(4)))
    return out


WAREHOUSE_BOND_MULTIPLE = 3  # a warehousing (Section 59) bond is for three times the duty


def _bond_check(r: "BoeResult") -> dict:
    """Reconcile the items' duty with the bond debit(s) printed in Part I.

    * WH (warehouse) bond: debit = 3 x the assessed duty of the items
      (MOOWR / Section 59 bond for thrice the duty).
    * any other bond (EI EOU, RE end-use, ...): debit = the duty forgone of the
      remaining items - all items when there is no WH bond. On an end-use (RE)
      bond only BCD + SWS are bonded, IGST is not.
    """
    wh = [b for b in r.bonds if (b.get("bond_code") or "").upper() == "WH"]
    rest = [b for b in r.bonds if b not in wh]
    assessed = [it for it in r.items if it.duty_source == BoeItem.ASSESSED]
    other = [it for it in r.items if it not in assessed] if wh else list(r.items)
    parts, ok = [], True
    n = len(r.items)
    if wh:
        duty = sum(it.total_duty_fg or 0 for it in assessed)
        debit = sum(b["debit_amount"] or 0 for b in wh)
        good = abs(WAREHOUSE_BOND_MULTIPLE * duty - debit) <= 2 + 1.5 * n
        ok &= good
        parts.append(f"WH bond {debit:,.0f} = 3 x assessed duty {duty:,.2f}" + ("" if good else " ✗"))
    if rest:
        debit = sum(b["debit_amount"] or 0 for b in rest)
        full = sum(it.total_duty_fg or 0 for it in other)
        bcd_sws = sum((it.bcd or 0) + (it.sws or 0) for it in other)
        codes = "/".join(sorted({b.get("bond_code") or "?" for b in rest}))
        if abs(full - debit) <= 1 + 0.5 * n:
            parts.append(f"{codes} bond {debit:,.0f} = duty forgone {full:,.2f}")
        elif abs(bcd_sws - debit) <= 1 + 0.5 * n:
            parts.append(f"{codes} bond {debit:,.0f} = BCD + SWS forgone {bcd_sws:,.2f} (IGST not bonded)")
        else:
            ok = False
            parts.append(f"{codes} bond {debit:,.0f} does not match the duty of the items ({full:,.2f}) ✗")
    return dict(ok=ok, text="; ".join(parts),
                duty=round(sum(it.total_duty_fg or 0 for it in r.items), 2))


# ------------------------------------------------------------------ main
def extract_bill_of_entry(filename: str, pdf_bytes: bytes) -> BoeResult:
    r = BoeResult(source_filename=filename)
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"Could not open PDF: {exc}")
        return r
    with doc:
        if not "".join(doc[i].get_text("text") for i in range(min(doc.page_count, 3))).strip():
            r.errors.append("This PDF has no text layer (a scan?). Download the original BE PDF from ICEGATE.")
            return r
        pages = [_Page(doc[i]) for i in range(doc.page_count)]
    if "BILL OF ENTRY" not in pages[0].text.upper():
        r.errors.append("This does not look like an ICEGATE Bill of Entry")
        return r
    _header(pages[0], r)
    for p in pages:
        if "PART - I " in p.text or "BILL OF ENTRY SUMMARY" in p.text:
            _part1(p, r)
        if "PART - II" in p.text and "INVOICE & VALUATION" in p.text:
            _part2(p, r)
        if "PART - III" in p.text and "DUTIES" in p.text:
            _part3(p, r)
    fg = _part4_duty_fg(pages)
    for it in r.items:
        if (it.invsno, it.itemsn) in fg:
            it.bcd_amt_fg, it.oth_amt_fg = fg[(it.invsno, it.itemsn)]
        elif it.duty_source is None:
            r.warnings.append(f"Item {it.invsno}/{it.itemsn}: duty not found (neither Part IV-G duty forgone nor a "
                              "Part III duty amount) - enter BCD/SWS/IGST")
        if it.qty is None:
            r.warnings.append(f"Item {it.invsno}/{it.itemsn}: quantity not found")
        if it.assessable_value is None:
            r.warnings.append(f"Item {it.invsno}/{it.itemsn}: assessable value not found")

    if not r.be_no:
        r.errors.append("BE number/date header not found")
    if not r.items:
        r.errors.append("No items found in Part III (DUTIES)")
    for inv_no in {it.invsno for it in r.items} - {i.sno for i in r.invoices}:
        r.warnings.append(f"Invoice {inv_no}: invoice number/supplier not found in Part II")
    if r.bonds and r.items:
        r.bond_check = _bond_check(r)
        if not r.bond_check["ok"]:
            r.warnings.append(r.bond_check["text"] + " - check the items")
    if r.total_assessable_value is not None and r.items:
        av = sum(it.assessable_value or 0 for it in r.items)
        if abs(av - r.total_assessable_value) > 1 + len(r.items):
            r.warnings.append(f"Assessable value of the items ({av:,.2f}) does not match the BE total "
                              f"({r.total_assessable_value:,.2f})")
    r.success = not r.errors
    return r
