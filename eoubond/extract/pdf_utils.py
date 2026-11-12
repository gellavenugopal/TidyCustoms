"""
pdf_utils.py (PyMuPDF / fitz version)

Watermark-safe line extraction for ICEGATE shipping bill and
bill-of-entry PDFs, built on the PyMuPDF dependency already in this
project's requirements.txt (no new library needed).

Why this exists: the diagonal "LET EXPORT COPY" watermark on these PDFs
is a text line with direction vector angle ~-64.2 degrees. Vertical
sidebar labels ("A. REF", "D. ITEM DETAILS", etc.) sit at ~-90/-91.7
degrees. PyMuPDF's page.get_text("rawdict") reports a direction vector
per line (line["dir"]), which lets us filter both out by angle before
any row reconstruction happens -- the same principle as the rotation
filter validated in the pdfplumber version, confirmed to produce
identical angle values on the same source files.

Verified against real ICEGATE shipping bill PDFs on 2026-09.
"""

import math
import fitz  # PyMuPDF


def is_body_line(line: dict, tolerance: float = 5) -> bool:
    """
    True for normal horizontal text lines (0 or 180 degrees direction).
    False for the diagonal watermark (~-64 degrees) or vertical sidebar
    labels (~-90 degrees).

    `line` is one entry from page.get_text("rawdict")["blocks"][i]["lines"].
    """
    dx, dy = line["dir"]
    angle = math.degrees(math.atan2(dy, dx))
    return abs(angle) < tolerance or abs(abs(angle) - 180) < tolerance


def get_clean_lines(page: "fitz.Page") -> list[dict]:
    """
    Returns body-text lines only (watermark and sidebar labels removed),
    each as {"text": str, "x0": float, "top": float, "bottom": float}.

    Text is reconstructed from the line's spans/chars directly rather
    than via page.get_text("text"), so filtering happens before any
    linear reading-order text stream is built.
    """
    raw = page.get_text("rawdict")
    lines = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            if not is_body_line(line):
                continue
            text = "".join(c["c"] for span in line["spans"] for c in span["chars"])
            if not text.strip():
                continue
            bbox = line["bbox"]  # (x0, y0, x1, y1)
            lines.append({
                "text": text,
                "x0": bbox[0],
                "top": bbox[1],
                "x1": bbox[2],
                "bottom": bbox[3],
            })
    return lines


def get_clean_words(page: "fitz.Page") -> list[dict]:
    """
    Word-level version of get_clean_lines, for extraction functions that
    need to match individual tokens (e.g. a two-word label like
    "Port Code") rather than whole lines.
    """
    raw = page.get_text("rawdict")
    words = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            if not is_body_line(line):
                continue
            for span in line["spans"]:
                current_text = ""
                current_x0 = None
                last_x1 = None
                for c in span["chars"]:
                    cx0, cy0, cx1, cy1 = c["bbox"]
                    if current_x0 is not None and (cx0 - last_x1) > 2.0:
                        words.append({
                            "text": current_text, "x0": current_x0,
                            "x1": last_x1, "top": cy0, "bottom": cy1,
                        })
                        current_text, current_x0 = "", None
                    if current_x0 is None:
                        current_x0 = cx0
                    current_text += c["c"]
                    last_x1 = cx1
                if current_text.strip():
                    words.append({
                        "text": current_text, "x0": current_x0,
                        "x1": last_x1, "top": c["bbox"][1], "bottom": c["bbox"][3],
                    })
    return [w for w in words if w["text"].strip()]


def group_lines_by_row(lines: list[dict]) -> dict[float, list[dict]]:
    """Groups lines by rounded `top` coordinate -- geometric rows,
    independent of page-break position in reading order."""
    rows: dict[float, list[dict]] = {}
    for l in lines:
        row = round(l["top"], 0)
        rows.setdefault(row, []).append(l)
    return rows
