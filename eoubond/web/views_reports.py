"""Report screens with Excel export (?format=xlsx on any report)."""
from __future__ import annotations

import io
from datetime import date, timedelta

from flask import abort, g, render_template, request, send_file, session
from sqlalchemy import func, select

from ..excel import reports_to_xlsx
from ..models import AuditLog, Bond
from ..services import reports as rep
from . import int_or_none, login_required, parse_date
from .views import bp

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _bond_id():
    return int_or_none(request.args.get("bond_id")) or session.get("bond_id")


def _ledger():
    bid = _bond_id()
    if bid:
        return [rep.bond_ledger(g.db, bid, parse_date(request.args.get("from")), parse_date(request.args.get("to")))]
    # no bond chosen: one ledger per bond
    return [rep.bond_ledger(g.db, b.id, parse_date(request.args.get("from")), parse_date(request.args.get("to")))
            for b in g.db.scalars(select(Bond).order_by(Bond.bond_no))]


REPORTS = {
    "stock": ("Stock by bond", lambda: [rep.stock_by_bond(g.db, _bond_id()), rep.fg_stock(g.db)]),
    "ledger": ("Bond ledger", _ledger),
    "monthly": ("Monthly debit / credit summary", lambda: [rep.monthly_summary(g.db, _bond_id())]),
    "pending-dta": ("Pending DTA duty", lambda: [rep.pending_dta_duty(g.db)]),
    "imports": ("Import register", lambda: [rep.import_register(g.db)]),
    "production": ("Production register", lambda: [rep.production_register(g.db)]),
    "exports": ("Export register", lambda: [rep.export_register(g.db)]),
    "audit": ("Audit trail", lambda: [rep.audit_trail(g.db, request.args.get("table") or None)]),
}


WORKING = {"stock", "ledger", "monthly", "pending-dta"}  # Excel = the formula workbook


def _working():
    from ..explained_xlsx import bond_workbook

    return send_file(io.BytesIO(bond_workbook(g.db)), as_attachment=True, mimetype=XLSX,
                     download_name=f"EOU_working_{date.today():%Y%m%d}.xlsx")


@bp.route("/reports/working.xlsx")
@login_required
def report_working():
    return _working()


@bp.route("/reports")
@login_required
def reports_index():
    return render_template("reports.html", reports=REPORTS)


@bp.route("/reports/<name>")
@login_required
def report(name):
    if name not in REPORTS:
        abort(404)
    title, build = REPORTS[name]
    if request.args.get("format") == "xlsx" and name in WORKING:
        return _working()
    results = build()
    if request.args.get("format") == "xlsx":
        return send_file(io.BytesIO(reports_to_xlsx(*results)), as_attachment=True, mimetype=XLSX,
                         download_name=f"{name}-{date.today():%Y%m%d}.xlsx")
    tables = sorted({t for (t,) in g.db.execute(select(AuditLog.table_name).distinct())}) if name == "audit" else []
    return render_template("report.html", name=name, title=title, results=results, tables=tables,
                           filters=dict(bond=name in ("stock", "ledger", "monthly"), dates=name == "ledger"))


@bp.route("/returns/form3")
@login_required
def form3_return():
    from ..models import ImportInvoice
    from ..services import form3 as f3

    first = g.db.scalar(select(func.min(func.coalesce(ImportInvoice.be_date, ImportInvoice.invoice_date)))
                        .where(ImportInvoice.status == "POSTED"))
    ends, e = [], f3.quarter_end_for(date.today())
    stop = f3.quarter_end_for(first) if first else e
    while e >= stop:
        ends.append(e)
        e = f3.quarter_start(e) - timedelta(days=1)
    chosen = parse_date(request.args.get("quarter_end")) or (ends[1] if len(ends) > 1 else ends[0])
    form = f3.build(g.db, chosen)
    if request.args.get("format") == "working":
        from ..explained_xlsx import form3_workbook

        return send_file(io.BytesIO(form3_workbook(form)), as_attachment=True, mimetype=XLSX,
                         download_name=f"Form3_working_{chosen:%Y%m%d}.xlsx")
    if request.args.get("format") == "xls":
        return send_file(io.BytesIO(f3.to_xls(form)), as_attachment=True, mimetype="application/vnd.ms-excel",
                         download_name=f"Form3_{chosen:%Y%m%d}.xls")
    return render_template("form3.html", form=form, ends=ends, chosen=chosen, f3=f3)


@bp.route("/reports/all.xlsx")
@login_required
def report_all():
    sheets = [rep.stock_by_bond(g.db), rep.fg_stock(g.db), rep.monthly_summary(g.db), rep.pending_dta_duty(g.db),
              rep.import_register(g.db), rep.production_register(g.db), rep.export_register(g.db)]
    sheets += [rep.bond_ledger(g.db, b.id) for b in g.db.scalars(select(Bond).order_by(Bond.bond_no))]
    return send_file(io.BytesIO(reports_to_xlsx(*sheets)), as_attachment=True, mimetype=XLSX,
                     download_name=f"eou-bond-reports-{date.today():%Y%m%d}.xlsx")
