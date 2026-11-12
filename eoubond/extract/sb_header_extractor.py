"""
sb_header_extractor.py (PyMuPDF / fitz version)

Extracts Port Code, Invoice No. & Date, and INVSN/ITEMSN/HS-code row
anchors from ICEGATE shipping bill PDFs using pdf_utils_fitz's
watermark-safe line extraction. Uses the PyMuPDF dependency already
in requirements.txt -- no new library added.

The row extraction handles items that land alone on a page immediately
after a reprinted table header.
"""

import re
import fitz  # PyMuPDF

from .pdf_utils import get_clean_lines, get_clean_words, group_lines_by_row  # EOU Tool: relative import


def get_header_field(words: list[dict], label_tokens: list[str], value_pattern: str | None = None) -> str | None:
    label_words: list[dict] = []
    for i in range(len(words) - len(label_tokens) + 1):
        if all(words[i + j]["text"] == label_tokens[j] for j in range(len(label_tokens))):
            label_words = words[i:i + len(label_tokens)]
            break
    if not label_words:
        return None
    label_x0 = label_words[0]["x0"]
    label_top = label_words[0]["top"]
    # anchor on the label's top (not bottom) -- PyMuPDF's line bbox height
    # for these embedded fonts can overstate descent, making bottom
    # unreliable as a boundary; searching a fixed window below the label's
    # top avoids both re-matching the label's own row and skipping past
    # the value row into the next label down.
    candidates = sorted(
        [w for w in words if label_top + 5 < w["top"] < label_top + 15 and abs(w["x0"] - label_x0) < 40],
        key=lambda w: w["top"],
    )
    if not candidates:
        return None
    value = candidates[0]["text"]
    if value_pattern and not re.match(value_pattern, value):
        return None
    return value


def extract_port_code(page: "fitz.Page") -> str | None:
    words = get_clean_words(page)
    return get_header_field(words, ["Port Code"], value_pattern=r"^[A-Z]{4,6}\d?$")


def extract_invoice_no_and_date(page: "fitz.Page") -> list[dict]:
    lines = get_clean_lines(page)
    full_text = "\n".join(l["text"] for l in sorted(lines, key=lambda l: l["top"]))
    return [
        {"invoice_no": inv, "invoice_date": dt}
        for inv, dt in re.findall(r"([A-Z]{4}/\d{2}-\d{2}/\d{5})\s+(\d{2}/\d{2}/\d{4})", full_text)
    ]


def extract_invsn_rows(page: "fitz.Page") -> list[dict]:
    """
    PyMuPDF emits each table cell as its own 'line' object even when
    several cells sit on the same visual table row (common in
    government report-generator PDFs, where each column is its own
    text run). So row reconstruction groups clean lines by geometric
    top coordinate first, then reads tokens left-to-right within that
    group -- the same principle as the pdfplumber version, adapted to
    fitz's per-cell line granularity.
    """
    lines = get_clean_lines(page)
    rows = group_lines_by_row(lines)
    results = []
    for row_top in sorted(rows):
        row_lines = sorted(rows[row_top], key=lambda l: l["x0"])
        tokens = []
        for l in row_lines:
            tokens.extend(l["text"].split())
        if (
            len(tokens) >= 3
            and tokens[0].isdigit()
            and tokens[1].isdigit()
            and re.match(r"^\d{6,8}$", tokens[2])
        ):
            results.append({"invsn": tokens[0], "itemsn": tokens[1], "hs_code": tokens[2]})
    return results


def extract_sb_header_summary(doc: "fitz.Document") -> dict:
    port_code = extract_port_code(doc[0])
    invoices: list[dict] = []
    invsn_rows: list[dict] = []

    for page in doc:
        text = page.get_text()
        if "PART - II" in text or "PART -II" in text:
            for inv in extract_invoice_no_and_date(page):
                if inv not in invoices:
                    invoices.append(inv)
        if "PART - III" in text:
            invsn_rows.extend(extract_invsn_rows(page))

    return {
        "port_code": port_code,
        "invoices": invoices,
        "invsn_rows": invsn_rows,
        "invsn_row_count": len(invsn_rows),
    }
