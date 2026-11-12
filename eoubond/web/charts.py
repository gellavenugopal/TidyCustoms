"""Small server-side SVG charts for the dashboard (no JS library, works offline).

Follows the dataviz method: thin marks, 4 px rounded data-ends anchored to the
baseline, 2 px gaps between adjacent bars, recessive grid, series colours from
CSS roles (--series-1/2/3, with selected dark steps), text in ink tokens, a
legend for two or more series, selective direct labels, and a hover tooltip
(data-tip) on every mark. Each chart also gets a table view in the template.
"""
from __future__ import annotations

import math
from decimal import Decimal
from html import escape

from markupsafe import Markup


def inr(v) -> str:
    """Compact Indian notation: ₹1.25 Cr, ₹45.3 L, ₹8,500."""
    v = float(v or 0)
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:,.1f} L"
    return f"{sign}₹{a:,.0f}"


def _nice_max(m: float) -> tuple[float, float]:
    if m <= 0:
        return 1.0, 0.25
    raw = m / 4
    p = 10 ** math.floor(math.log10(raw))
    step = next(s * p for s in (1, 2, 2.5, 5, 10) if s * p >= raw)
    return step * math.ceil(m / step), step


def _bar_v(x, y_top, w, y_base, r=4) -> str:
    """Vertical bar with only the data-end (top) rounded."""
    h = y_base - y_top
    if h <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y_base:.1f} V{y_top + r:.1f} Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top + r:.1f} V{y_base:.1f} Z")


def _bar_h(x0, y, x1, h, r=4) -> str:
    """Horizontal bar with only the data-end (right) rounded."""
    w = x1 - x0
    if w <= 0:
        return ""
    r = min(r, h / 2, w)
    return (f"M{x0:.1f},{y:.1f} H{x1 - r:.1f} Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f} V{y + h - r:.1f} "
            f"Q{x1:.1f},{y + h:.1f} {x1 - r:.1f},{y + h:.1f} H{x0:.1f} Z")


def hbars(rows: list[tuple[str, float, str]], *, label_w: int = 170, width: int = 620, title: str = "") -> Markup:
    """One series of horizontal bars. rows = [(label, value, tooltip)]."""
    if not rows:
        return Markup('<p class="muted">Nothing to show yet.</p>')
    bar_h, gap = 16, 14
    h = len(rows) * (bar_h + gap) + 6
    top, _step = _nice_max(max(float(v) for _l, v, _t in rows))
    x0, x1 = label_w, width - 86
    parts = [f'<svg viewBox="0 0 {width} {h}" role="img" aria-label="{escape(title)}">']
    for i, (label, v, tip) in enumerate(rows):
        y = i * (bar_h + gap) + 4
        xe = x0 + (x1 - x0) * (float(v) / top)
        short = label if len(label) <= 24 else label[:23] + "…"
        parts.append(f'<text class="viz-cat" x="{x0 - 10}" y="{y + bar_h - 3}" text-anchor="end">{escape(short)}</text>')
        parts.append(f'<rect x="{x0}" y="{y}" width="{x1 - x0}" height="{bar_h}" rx="4" fill="var(--chart-track)"/>')
        parts.append(f'<path class="viz-mark" d="{_bar_h(x0, y, max(xe, x0 + 2), bar_h)}" fill="var(--series-1)" '
                     f'tabindex="0" data-tip="{escape(tip)}"/>')
        parts.append(f'<text class="viz-val" x="{max(xe, x0) + 8}" y="{y + bar_h - 3}">{escape(inr(v))}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def columns(cats: list[str], series: list[tuple[str, str, list[float]]], *, width: int = 680, height: int = 250,
            title: str = "", tip_cats: list[str] | None = None) -> Markup:
    """Grouped columns. series = [(name, css colour var, values per category)]."""
    left, right, top_pad, bottom = 58, 8, 10, 26
    plot_w, plot_h = width - left - right, height - top_pad - bottom
    vmax = max([float(v) for _n, _c, vals in series for v in vals] + [0])
    top, step = _nice_max(vmax)
    y = lambda v: top_pad + plot_h * (1 - float(v) / top)  # noqa: E731
    band = plot_w / max(len(cats), 1)
    n = len(series)
    bw = min(18, (band * 0.62 - 2 * (n - 1)) / max(n, 1))
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">']
    k = 0
    while k * step <= top + 1e-9:
        yy = y(k * step)
        parts.append(f'<line class="{"viz-base" if k == 0 else "viz-grid"}" x1="{left}" x2="{width - right}" '
                     f'y1="{yy:.1f}" y2="{yy:.1f}"/>')
        parts.append(f'<text class="viz-axis" x="{left - 8}" y="{yy + 4:.1f}" text-anchor="end">{escape(inr(k * step))}</text>')
        k += 1
    base = y(0)
    for i, cat in enumerate(cats):
        group_w = n * bw + (n - 1) * 2
        gx = left + band * i + (band - group_w) / 2
        for j, (name, colour, vals) in enumerate(series):
            v = float(vals[i] or 0)
            x = gx + j * (bw + 2)
            label = (tip_cats or cats)[i]
            if v > 0:
                parts.append(f'<path class="viz-mark" d="{_bar_v(x, y(v), bw, base)}" fill="var({colour})" tabindex="0" '
                             f'data-tip="{escape(f"{label} · {name}: {inr(v)}")}"/>')
        parts.append(f'<text class="viz-axis" x="{left + band * i + band / 2:.1f}" y="{height - 8}" '
                     f'text-anchor="middle">{escape(cat)}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def share_bar(parts_: list[tuple[str, str, float]], *, width: int = 620, title: str = "") -> Markup:
    """One 100% bar split into parts = [(name, css colour var, value)], 2 px gaps, direct labels below."""
    total = sum(float(v) for _n, _c, v in parts_)
    if total <= 0:
        return Markup('<p class="muted">Nothing to show yet.</p>')
    h, x = 22, 0.0
    live = [(n, c, float(v)) for n, c, v in parts_ if float(v) > 0]
    gaps = 2 * (len(live) - 1)
    out = [f'<svg viewBox="0 0 {width} {h + 28}" role="img" aria-label="{escape(title)}">']
    for idx, (name, colour, v) in enumerate(live):
        w = (width - gaps) * v / total
        rx = 4 if idx in (0, len(live) - 1) else 0
        out.append(f'<rect class="viz-mark" x="{x:.1f}" y="0" width="{max(w, 1):.1f}" height="{h}" rx="{rx}" '
                   f'fill="var({colour})" tabindex="0" data-tip="{escape(f"{name}: {inr(v)} ({v / total:.0%})")}"/>')
        if w > 70:
            out.append(f'<text class="viz-val" x="{x + 2:.1f}" y="{h + 18}">{v / total:.0%} · {escape(inr(v))}</text>')
        x += w + 2
    out.append("</svg>")
    return Markup("".join(out))


def as_float(v) -> float:
    return float(v) if isinstance(v, Decimal) else float(v or 0)
