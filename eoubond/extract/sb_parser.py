"""
Fully offline extraction engine for Indian Customs Shipping Bills.

No internet connection and no API key required. This module reads the
PDF's own embedded text layer directly (every ICEGATE-generated shipping
bill — the "LET EXPORT COPY" printout a CA downloads from the ICEGATE
portal — is a text-layer PDF, not a scanned image), so extraction is
instant, free, and works fully offline.

Why coordinate-based parsing instead of regex-on-flat-text
------------------------------------------------------------
PyMuPDF's flat text extraction for this template reads the Part III
item-details table in a confusing column-major order (all "Item Sl No"
labels, then all Sl No values, then all HSN labels, then all HSN values,
...) and silently *skips* blank cells (e.g. IGST Value/Amount are only
present for items where IGSTSTAT='P'), which makes positional alignment
unreliable with simple line-counting.

Instead, this parser reads each PDF page as a set of positioned text
blocks (x0, y0, text) and reconstructs the table using its actual grid:
- Each of the 9 "row 1" field labels (Item Sl No, HSN, Description, Qty,
  UQC, Rate, Value(F/C), FOB(INR), PMV) appears once per item, at a
  known x-position (column) and a y-position (row) that repeats for
  every item box on the page.
- The item's data sits in a y-band just below its own label row, in the
  same x-column as the label above it.
- A second label row per item (Duty Amt, Cess Rate, Cess Amt, DBK Claimed,
  IGST Status, IGST Value, IGST Amount, Scheme Code) is parsed the same
  way, and blank cells (no block present) are correctly treated as
  "not applicable" rather than mis-aligned to the wrong item.

This has been validated against a real, multi-page (26 page, 61 line
item) shipping bill: extracted FOB and IGST Amount column sums match the
bill's own Part I summary totals exactly.

Known constraint: this parser is built against the standard ICEGATE
"Shipping Bill Summary / Invoice Details / Item Details" EDI print
template (the format used at JNCH Nhava Sheva and, as far as is publicly
documented, uniformly across Indian customs houses, since it's generated
by the same central ICEGATE system). A shipping bill from a fundamentally
different, non-ICEGATE source may not parse correctly — such documents
will surface as "no line items found" rather than silently wrong data,
since the label-matching requires the exact field names this template
uses.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from .pdf_utils import get_clean_lines, get_clean_words

from .sb_schema import ExtractionResult, LineItem  # EOU Tool: was .schema

logger = logging.getLogger("shipping_bill_extractor")

# --- Known field labels & their column order in the ICEGATE Part III table ---

FIELD_LABELS_ROW1 = [
    "2ITEMSN", "3.HS CD", "4.DESCRIPTION", "5.QUANTITY", "6UQC",
    "7.RATE", "8VALUE(F/C)", "9.FOB (INR)", "10.PMV",
]
FIELD_KEYS_ROW1 = [
    "item_serial_no", "hsn_sac", "description", "quantity", "uqc",
    "rate", "value_fc", "fob_value_inr", "pmv",
]

FIELD_LABELS_ROW2 = [
    "11.DUTYAMT", "12.CESS RT", "13CESAMT", "14.DBKCLMD", "15.IGSTSTAT",
    "16. IGST VALUE", "17. IGST AMOUNT", "18SCHCOD",
]
FIELD_KEYS_ROW2 = [
    "duty_amt", "cess_rt", "cess_amt", "dbk_clmd", "igst_stat",
    "igst_value", "igst_amount", "sch_cod",
]

# Cosmetic elements that land inside the table's coordinate space and would
# otherwise be mistaken for data (the diagonal "LET EXPORT COPY" watermark
# is the main offender — it can land at almost any y-position on a page).
NOISE_TEXTS = {"LET EXPORT COPY", "PART - III - ITEM DETAILS", "INVOICE (1/1)"}

# --- Row-1 item data: pattern-based extraction (replaces label-position
# bucketing for this specific table, since PyMuPDF's block/line grouping
# merges adjacent column labels and even adjacent data cells (HS code +
# description) unpredictably from page to page -- verified against real
# ICEGATE shipping bills where this caused entire pages to silently
# contribute zero rows. HS codes are always 6-8 digits and the trailing
# quantity/UQC/rate/value/FOB/PMV run is a fixed 6-token pattern, so both
# are reliably regex-matchable regardless of which text block they land in.
#
# RATE and VALUE(F/C) are printed WITHOUT a leading zero whenever they're
# under 1 (".25" rather than "0.25") -- a plain [\d,]+\.?\d* requires at
# least one digit before the decimal point and silently fails to match
# those tokens, which drops the whole numeric tail into the description
# instead (verified: this is exactly what happened to items 63/64 of a
# 64-item invoice, where RATE=.25 VALUE(F/C)=.75 broke the match and let
# their wrapped second description line get swallowed into the first).
_NUM_RE = r"(?:\d[\d,]*\.?\d*|\.\d+)"
_NUMERIC_TAIL_RE = re.compile(
    rf"({_NUM_RE})\s+([A-Z]{{2,4}})\s+({_NUM_RE})\s+({_NUM_RE})\s+({_NUM_RE})\s+({_NUM_RE})"
)
_ROW_START_TOKEN_RE = re.compile(r"^\d{1,3}\s+\d{6,8}\s")
_ROW1_START_WITH_INVSN_RE = re.compile(r"(?:^|\s)(\d{1,2})\s+(\d{1,3})\s+(\d{6,8})\s+(.*)$", re.S)
_INVOICE_REF_RE = re.compile(r"^(\d{1,2})\s+([A-Z]{4}/\d{2}-\d{2}/\d{5})\s+(\d{2}/\d{2}/\d{4})")
_ROW1_SKIP_LINE_RE = re.compile(
    r"^(11\.DUTYAMT|12\.CESS|13CESAMT|20\.\s*SQC|19\.\s*SCHEME|24\.\s*PT|"
    r"25\.COMP|26\.END|Page|Scan QR|PART\s*-|Glossary|INVOICE\s*\()", re.I
)
_ROW2_TAIL_RE = re.compile(rf"({_NUM_RE})\s+({_NUM_RE})\s+(\d{{1,3}})\s*$")
_IGST_STAT_RE = re.compile(r"\b(P|NA)\b")


SB_HEADER_RE = re.compile(
    r"Port Code\s*SB No\s*SB Date\s*(\S+)\s*(\d+)\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})"
)

# "2.INVOICE No. & Dt." (A.REF / Part II section) prints the invoice number
# and its date on one line, separated by a space, e.g.
# "ABCD/25-26/00001 01/01/2026" -- captured as two groups and split into two
# output columns (INV NO / INV DATE) rather than kept as one combined field.
_INVOICE_NO_DATE_RE = re.compile(r"([A-Z]{4}/\d{2}-\d{2}/\d{5})\s+(\d{2}/\d{2}/\d{4})")


class OfflineParseError(RuntimeError):
    pass


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _bucket_columns(label_blocks: List[Tuple[float, str]]) -> List[Tuple[float, str]]:
    return sorted(label_blocks, key=lambda t: t[0])


def _assign_column(x0: float, sorted_labels: List[Tuple[float, str]]) -> str:
    """Bucket a data block's x0 into the nearest column using label-midpoint boundaries."""
    n = len(sorted_labels)
    for i in range(n):
        lo = sorted_labels[i][0] if i == 0 else (sorted_labels[i - 1][0] + sorted_labels[i][0]) / 2
        hi = (sorted_labels[i][0] + sorted_labels[i + 1][0]) / 2 if i < n - 1 else float("inf")
        if (lo <= x0 < hi) or (i == 0 and x0 < hi):
            return sorted_labels[i][1]
    return sorted_labels[-1][1]


@dataclass
class _RawRow:
    invsn: Optional[str] = None  # EOU Tool: invoice serial passed through
    item_serial_no: Optional[str] = None
    hsn_sac: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[str] = None
    uqc: Optional[str] = None
    fob_value_inr: Optional[str] = None
    igst_stat: Optional[str] = None
    igst_value: Optional[str] = None
    igst_amount: Optional[str] = None


def _extract_row1_items(page: "fitz.Page") -> List[Tuple[float, Dict[str, List[str]]]]:
    """
    Pattern-based extraction of row-1 item data (item_serial_no, hsn_sac,
    description, quantity, uqc, fob_value_inr).

    Replaces label-position bucketing for this specific table: PyMuPDF's
    block/line grouping merges adjacent column labels -- and even adjacent
    data cells (HS code + description start) -- unpredictably from page to
    page, which was causing entire pages to silently contribute zero rows
    (verified against real ICEGATE shipping bills, including a case where
    this dropped the final item of a 127-item invoice because that item
    landed alone on a page where the header row happened to merge).

    HS codes are always 6-8 digits and the trailing quantity/UQC/rate/
    value/FOB/PMV run is a fixed 6-token pattern, so both are reliably
    regex-matchable regardless of which text block they land in, and
    wrapped multi-line descriptions are reassembled by scanning forward
    until the next item or the row-2 (duty/IGST) section begins.
    """
    lines = get_clean_lines(page)
    rows: Dict[float, List[dict]] = defaultdict(list)
    for l in lines:
        rows[round(l["top"], 0)].append(l)
    row_tops = sorted(rows.keys())

    def row_text(top: float) -> str:
        return " ".join(l["text"] for l in sorted(rows[top], key=lambda l: l["x0"]))

    results: List[Tuple[float, Dict[str, List[str]]]] = []
    i = 0
    while i < len(row_tops):
        top_i = row_tops[i]
        text_i = row_text(top_i)
        # Try to capture INVSN (invoice serial no.) immediately preceding
        # ITEMSN on the same row first; fall back to the 2-token pattern
        # (ITEMSN + HS code only) if INVSN isn't present as its own token,
        # so a row still extracts even without it.
        m = _ROW1_START_WITH_INVSN_RE.search(text_i)
        if m:
            invsn, itemsn, hscode, rest0 = m.group(1), m.group(2), m.group(3), m.group(4)
        else:
            m2 = re.search(r"(?:^|\s)(\d{1,3})\s+(\d{6,8})\s+(.*)$", text_i, re.S)
            if not m2:
                i += 1
                continue
            invsn, itemsn, hscode, rest0 = None, m2.group(1), m2.group(2), m2.group(3)

        combined = [rest0]
        j = i + 1
        while j < len(row_tops):
            text_j = row_text(row_tops[j])
            stripped = text_j.strip()
            if _ROW1_SKIP_LINE_RE.match(stripped) or _ROW_START_TOKEN_RE.match(stripped):
                break
            combined.append(text_j)
            j += 1

        full_rest = " ".join(combined)
        tail_match = _NUMERIC_TAIL_RE.search(full_rest)
        if tail_match:
            qty, uqc, rate, value_fc, fob_inr, pmv = tail_match.groups()
            description = full_rest[:tail_match.start()] + " " + full_rest[tail_match.end():]
        else:
            qty = uqc = fob_inr = None
            description = full_rest
        description = _clean(description)

        results.append((
            top_i,
            {
                "invsn": [invsn] if invsn else [],  # EOU Tool
                "item_serial_no": [itemsn],
                "hsn_sac": [hscode],
                "description": [description] if description else [],
                "quantity": [qty] if qty else [],
                "uqc": [uqc] if uqc else [],
                "fob_value_inr": [fob_inr] if fob_inr else [],
            },
        ))
        i = j

    return results


def _extract_row2_items(page: "fitz.Page") -> List[Tuple[float, Dict[str, Optional[str]]]]:
    """
    Pattern-based extraction of row-2 duty/IGST data (igst_stat, igst_value,
    igst_amount). Replaces label-position bucketing for the same reason as
    _extract_row1_items: PyMuPDF's block/line grouping merges adjacent
    labels (13CESAMT/14.DBKCLMD/15.IGSTSTAT commonly merge into one line),
    and this previously caused igst_value/igst_amount to come back blank on
    any page where the merge happened -- even though the row itself
    extracted fine otherwise (verified: this affected item 127 of a
    127-item invoice, since that item's page had this merge).

    IGST VALUE, IGST AMOUNT, and SCHCOD are always the last three tokens on
    the row-2 data line, in that order, with nothing after SCHCOD -- a
    reliable anchor regardless of which earlier columns (DUTYAMT/CESS RT/
    CESAMT/DBKCLMD) are blank for a given item. IGST STAT is then found by
    searching the remaining text for its known value set (P/NA).

    Verified against a real 127-item invoice: summed igst_value and
    igst_amount across every extracted item matched the shipping bill's
    own Part I summary totals (4.IGST VALUE / 2.IGST AMT) to the cent.
    """
    lines = get_clean_lines(page)
    rows: Dict[float, List[dict]] = defaultdict(list)
    for l in lines:
        rows[round(l["top"], 0)].append(l)
    row_tops = sorted(rows.keys())

    def row_text(top: float) -> str:
        return " ".join(l["text"] for l in sorted(rows[top], key=lambda l: l["x0"]))

    results: List[Tuple[float, Dict[str, Optional[str]]]] = []
    for idx, top in enumerate(row_tops):
        text = row_text(top)
        if "11.DUTYAMT" not in text[:20]:
            continue
        if idx + 1 >= len(row_tops):
            continue
        data_top = row_tops[idx + 1]
        data_text = row_text(data_top)
        tail = _ROW2_TAIL_RE.search(data_text)
        if not tail:
            results.append((data_top, {"igst_stat": None, "igst_value": None, "igst_amount": None}))
            continue
        igst_value, igst_amount, _sch_cod = tail.groups()
        stat_match = _IGST_STAT_RE.search(data_text[: tail.start()])
        results.append((
            data_top,
            {
                "igst_stat": stat_match.group(1) if stat_match else None,
                "igst_value": igst_value,
                "igst_amount": igst_amount,
            },
        ))
    return results


def _parse_part3_page(page: "fitz.Page") -> List[_RawRow]:
    """Parse every item row out of one Part III ("Item Details") page."""
    row1_items = _extract_row1_items(page)
    row2_items = _extract_row2_items(page)

    results: List[_RawRow] = []
    for label_y, row1 in row1_items:
        if not row1:
            continue
        # Row 2 (duty/IGST) sits ~20-30pt below its matching row 1 box; match by
        # nearest label_y rather than a fixed offset, since spacing varies slightly.
        best_row2: Dict[str, Optional[str]] = {}
        if row2_items:
            _, best_row2 = min(row2_items, key=lambda item: abs(item[0] - (label_y + 25)))

        def first(d, key):
            v = d.get(key)
            return v[0] if v else None

        results.append(
            _RawRow(
                invsn=first(row1, "invsn"),  # EOU Tool
                item_serial_no=first(row1, "item_serial_no"),
                hsn_sac=first(row1, "hsn_sac"),
                description=first(row1, "description"),
                quantity=first(row1, "quantity"),
                uqc=first(row1, "uqc"),
                fob_value_inr=first(row1, "fob_value_inr"),
                igst_stat=best_row2.get("igst_stat"),
                igst_value=best_row2.get("igst_value"),
                igst_amount=best_row2.get("igst_amount"),
            )
        )
    return results


def _parse_number(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _extract_sb_header(doc: "fitz.Document") -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Port Code / SB No / SB Date are repeated in the header block on every page; check the first few."""
    for page_index in range(min(3, doc.page_count)):
        text = doc[page_index].get_text("text")
        m = SB_HEADER_RE.search(text)
        if m:
            port_code, sb_no, sb_date = m.groups()
            return port_code, sb_no, sb_date
    return None, None, None


_RECONCILIATION_TOLERANCE = 0.05  # rupees; allows for rounding on the bill's own printed totals


def _extract_part1_inv_no(page0: "fitz.Page") -> Optional[str]:
    """
    Reads "2.INV NO." from the Part I "F. INVOICE SUMMARY" box on the
    bill's first page. This is distinct from Part II's "2.INVOICE No. &
    Dt." (which pairs the invoice number with its date, in the A.REF
    section) -- this one sits alongside invoice amount and currency
    instead, and is the field printed at the top-of-bill summary level
    rather than per invoice-detail block.
    """
    lines = get_clean_lines(page0)
    rows: Dict[float, List[dict]] = defaultdict(list)
    for l in lines:
        rows[round(l["top"], 0)].append(l)
    row_tops = sorted(rows.keys())

    label_anchor = None
    for top in row_tops:
        for w in sorted(rows[top], key=lambda l: l["x0"]):
            if w["text"].strip() == "2.INV NO.":
                label_anchor = (w["x0"], top)
                break
        if label_anchor:
            break
    if not label_anchor:
        return None

    label_x0, label_top = label_anchor
    candidates = [
        (top, w) for top in row_tops if label_top < top < label_top + 15
        for w in sorted(rows[top], key=lambda l: l["x0"])
        if abs(w["x0"] - label_x0) < 20
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]["text"].strip() or None


def _extract_invoice_no_and_date(doc: "fitz.Document") -> Tuple[Optional[str], Optional[str]]:
    """
    Reads "2.INVOICE No. & Dt." from the Part II "A.REF" section. Unlike
    "2.INV NO." in the Part I summary (see _extract_part1_inv_no), this
    field prints the invoice number and its date together on one line,
    e.g. "ABCD/25-26/00001 01/01/2026" -- split here into two values so
    they land in separate INV NO / INV DATE output columns.
    """
    for page_index in range(min(5, doc.page_count)):
        text = doc[page_index].get_text("text")
        if "INVOICE No. & Dt." not in text:
            continue
        m = _INVOICE_NO_DATE_RE.search(text)
        if m:
            return m.group(1), m.group(2)
    return None, None


def _get_label_value(
    words: List[dict], label_text: str, value_pattern: Optional[str] = None
) -> Optional[str]:
    """
    Generic word-position label lookup: finds a single-token label (e.g.
    "1.DBK CLAIM") and returns the nearest word in a fixed band just below
    it, in roughly the same x-column -- the same anchoring approach used
    throughout this module for header-box fields (search a y-band below
    the label's top rather than its bottom, since embedded-font line boxes
    can overstate descent and make "bottom" an unreliable boundary).
    """
    label_words = [w for w in words if w["text"].strip() == label_text]
    if not label_words:
        return None
    label_x0, label_top = label_words[0]["x0"], label_words[0]["top"]
    candidates = sorted(
        [w for w in words if label_top + 5 < w["top"] < label_top + 20 and abs(w["x0"] - label_x0) < 40],
        key=lambda w: w["top"],
    )
    if not candidates:
        return None
    value = candidates[0]["text"].strip()
    if value_pattern and not re.match(value_pattern, value):
        return None
    return value or None


def _extract_part1_dbk_rodtep(page0: "fitz.Page") -> Tuple[Optional[float], Optional[float]]:
    """
    Reads "1.DBK CLAIM" and "5.RODTEP AMT" from the Part I "D. EX.PR."
    summary box on the bill's first page (same box as FOB VALUE / IGST
    AMT / IGST VALUE etc., each its own single-token label with its value
    printed directly below).
    """
    words = get_clean_words(page0)
    amount_pattern = r"^[\d,]+\.?\d*$"
    dbk_claim = _parse_number(_get_label_value(words, "1.DBK CLAIM", amount_pattern))
    rodtep_amt = _parse_number(_get_label_value(words, "5.RODTEP AMT", amount_pattern))
    return dbk_claim, rodtep_amt


def _extract_part1_igst_totals(page0: "fitz.Page") -> Tuple[Optional[float], Optional[float]]:
    """
    Reads the "4.IGST VALUE" and "2. IGST AMT" totals from the shipping
    bill's own Part I summary (first page). Used to cross-check the sum of
    every extracted line item's igst_value/igst_amount -- a document-
    verified reconciliation, not just an internal consistency check.
    """
    lines = get_clean_lines(page0)
    rows: Dict[float, List[dict]] = defaultdict(list)
    for l in lines:
        rows[round(l["top"], 0)].append(l)
    row_tops = sorted(rows.keys())

    def row_words(top):
        return sorted(rows[top], key=lambda l: l["x0"])

    igst_amt_anchor = igst_value_anchor = None  # (label_x0, label_top)
    for top in row_tops:
        for w in row_words(top):
            if "IGST AMT" in w["text"]:
                igst_amt_anchor = (w["x0"], top)
            if "IGST VALUE" in w["text"]:
                igst_value_anchor = (w["x0"], top)

    def value_below(anchor):
        if not anchor:
            return None
        label_x0, label_top = anchor
        candidates = [
            (top, w) for top in row_tops if label_top < top < label_top + 15
            for w in row_words(top) if abs(w["x0"] - label_x0) < 15
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        try:
            return float(candidates[0][1]["text"].replace(",", ""))
        except ValueError:
            return None

    return value_below(igst_value_anchor), value_below(igst_amt_anchor)


def extract_shipping_bill_offline(filename: str, pdf_bytes: bytes) -> ExtractionResult:
    """
    Extract all line items from a shipping bill PDF entirely offline.

    Only works on text-layer PDFs (the normal case for an ICEGATE "LET
    EXPORT COPY" download). A scanned/photographed shipping bill with no
    embedded text layer will come back with a clear error asking for a
    text-based PDF instead, rather than silently returning nothing.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        return ExtractionResult(
            source_filename=filename, success=False, errors=[f"Could not open PDF: {exc}"]
        )

    with doc:
        full_text = "".join(doc[i].get_text("text") for i in range(min(doc.page_count, 5)))
        if not full_text.strip():
            return ExtractionResult(
                source_filename=filename,
                success=False,
                errors=[
                    "This PDF has no extractable text layer (it looks like a scanned image). "
                    "The offline parser needs a text-based PDF — the original 'LET EXPORT COPY' "
                    "download from ICEGATE, not a photograph or scan of a printout."
                ],
            )

        port_code, sb_no, sb_date = _extract_sb_header(doc)
        if not sb_no:
            return ExtractionResult(
                source_filename=filename,
                success=False,
                errors=[
                    "Could not find the 'SB No / SB Date' header. This doesn't look like a "
                    "standard ICEGATE shipping bill printout."
                ],
            )

        raw_rows: List[_RawRow] = []
        part3_pages = 0
        for i in range(doc.page_count):
            page = doc[i]
            if "PART - III - ITEM DETAILS" not in page.get_text("text"):
                continue
            part3_pages += 1
            raw_rows.extend(_parse_part3_page(page))

        header_igst_value, header_igst_amount = _extract_part1_igst_totals(doc[0])
        inv_no, inv_date = _extract_invoice_no_and_date(doc)
        dbk_claim, rodtep_amt = _extract_part1_dbk_rodtep(doc[0])

        if part3_pages == 0:
            return ExtractionResult(
                source_filename=filename,
                success=False,
                errors=[
                    "No 'PART - III - ITEM DETAILS' section found. This tool expects the full "
                    "multi-page ICEGATE shipping bill printout, not an extract or summary page."
                ],
            )

    line_items: List[LineItem] = []
    errors: List[str] = []
    warnings: List[str] = []
    for i, row in enumerate(raw_rows):
        missing = []
        if not row.item_serial_no:
            missing.append("item serial number not found")
        if not row.hsn_sac:
            missing.append("HSN/SAC not found")
        if not row.description:
            missing.append("description not found")
        if missing:
            warnings.append(f"Row {i + 1}: {'; '.join(missing)} — row skipped.")
            continue

        igst_value = _parse_number(row.igst_value) if row.igst_stat == "P" else None
        igst_amount = _parse_number(row.igst_amount) if row.igst_stat == "P" else None
        row_missing = []
        if row.igst_stat not in ("P", "NA", None):
            row_missing.append(f"unrecognized IGST status '{row.igst_stat}'")

        try:
            line_items.append(
                LineItem(
                    port_code=port_code,
                    sb_no=sb_no,
                    sb_date=sb_date,
                    inv_no=inv_no,
                    inv_date=inv_date,
                    invsn=row.invsn,  # EOU Tool
                    item_serial_no=row.item_serial_no,
                    hsn_sac=row.hsn_sac,
                    description=row.description,
                    quantity=_parse_number(row.quantity),
                    uqc=row.uqc,
                    fob_value_inr=_parse_number(row.fob_value_inr),
                    igst_value=igst_value,
                    igst_amount=igst_amount,
                    dbk_claim=dbk_claim,
                    rodtep_amt=rodtep_amt,
                    missing_fields=row_missing,
                )
            )
        except Exception as exc:  # pydantic validation error on a genuinely malformed row
            errors.append(f"Row {i + 1} (item {row.item_serial_no}) failed validation: {exc}")

    if not line_items:
        errors.append("No valid line items could be extracted from the Item Details table.")

    extracted_igst_value_sum = sum(li.igst_value for li in line_items if li.igst_value is not None) or None
    extracted_igst_amount_sum = sum(li.igst_amount for li in line_items if li.igst_amount is not None) or None

    igst_reconciled: Optional[bool] = None
    if header_igst_value is not None and header_igst_amount is not None and line_items:
        value_ok = abs((extracted_igst_value_sum or 0.0) - header_igst_value) <= _RECONCILIATION_TOLERANCE
        amount_ok = abs((extracted_igst_amount_sum or 0.0) - header_igst_amount) <= _RECONCILIATION_TOLERANCE
        igst_reconciled = value_ok and amount_ok
        if not igst_reconciled:
            warnings.append(
                f"IGST reconciliation mismatch: extracted line items sum to "
                f"IGST VALUE={extracted_igst_value_sum}, IGST AMOUNT={extracted_igst_amount_sum}, "
                f"but the bill's Part I summary states IGST VALUE={header_igst_value}, "
                f"IGST AMOUNT={header_igst_amount}. Extraction may be incomplete — review this file."
            )

    return ExtractionResult(
        source_filename=filename,
        success=len(line_items) > 0,
        line_items=line_items,
        warnings=warnings,
        errors=errors,
        header_igst_value=header_igst_value,
        header_igst_amount=header_igst_amount,
        extracted_igst_value_sum=extracted_igst_value_sum,
        extracted_igst_amount_sum=extracted_igst_amount_sum,
        igst_reconciled=igst_reconciled,
    )
