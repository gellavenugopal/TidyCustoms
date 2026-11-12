"""Upload screens: BoE / SB PDFs -> pre-filled Excel template; filled template
-> preview (dry run, rolled back) -> confirm (same run, committed)."""
from __future__ import annotations

import io
import secrets
from datetime import date

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

from .. import config, doc_templates as dt
from ..extract.boe_parser import extract_bill_of_entry
from ..extract.sb_parser import extract_shipping_bill_offline
from . import role_required
from .views import bp

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_PDF = 25 * 1024 * 1024
KINDS = {
    "imports": dict(title="Bills of Entry", doc="Bill of Entry", back="main.imports"),
    "exports": dict(title="Shipping Bills", doc="Shipping Bill", back="main.exports"),
    "boms": dict(title="Bills of Material", doc="BOM", back="main.models"),
}
BLANK = {"imports": (dt.build_import_template, "BoE_template.xlsx"),
         "exports": (dt.build_export_template, "Export_template_with_stock.xlsx"),
         "boms": (dt.build_bom_template, "BOM_template.xlsx")}


def _kind(kind):
    if kind not in KINDS:
        abort(404)
    return KINDS[kind]


def _upload_dir():
    d = config.data_dir() / "uploads" / str(g.client.id) / str(g.user.id)
    d.mkdir(parents=True, exist_ok=True)
    return d


@bp.route("/<kind>/upload")
@role_required("operator")
def doc_upload(kind):
    return render_template("doc_upload.html", kind=kind, k=_kind(kind))


@bp.route("/<kind>/upload/blank")
@role_required("operator")
def doc_blank(kind):
    _kind(kind)
    build, name = BLANK[kind]
    data = build(g.db, with_stock=True) if kind == "exports" else build(g.db)  # exports: pre-filled with stock
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype=XLSX)


@bp.route("/<kind>/upload/pdf", methods=["POST"])
@role_required("operator")
def doc_from_pdf(kind):
    k = _kind(kind)
    if kind == "boms":
        abort(404)
    files = [f for f in request.files.getlist("pdfs") if f and f.filename]
    if not files:
        flash(f"Choose one or more {k['doc']} PDF files", "error")
        return redirect(url_for("main.doc_upload", kind=kind))
    results = []
    for f in files[:100]:
        blob = f.read(MAX_PDF + 1)
        if len(blob) > MAX_PDF:
            flash(f"{f.filename}: larger than 25 MB, skipped", "error")
            continue
        results.append(extract_bill_of_entry(f.filename, blob) if kind == "imports"
                       else extract_shipping_bill_offline(f.filename, blob))
    if results and not any(r.success for r in results):
        msgs = "; ".join(f"{r.source_filename}: {' '.join(r.errors)}" for r in results)
        flash(f"Nothing could be extracted - {msgs}", "error")
        return redirect(url_for("main.doc_upload", kind=kind))
    # partly failed files are listed on the workbook's "Extraction log" sheet
    if kind == "imports":
        data, name = dt.build_import_template(g.db, results), f"BoE_upload_{date.today():%Y%m%d}.xlsx"
    else:
        data, name = dt.build_export_template(g.db, results), f"SB_upload_{date.today():%Y%m%d}.xlsx"
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype=XLSX)


@bp.route("/<kind>/upload/template", methods=["POST"])
@role_required("operator")
def doc_template_preview(kind):
    k = _kind(kind)
    f = request.files.get("template")
    if not f or not f.filename:
        flash("Choose the filled Excel template", "error")
        return redirect(url_for("main.doc_upload", kind=kind))
    blob = f.read(30 * 1024 * 1024)
    post = request.form.get("post") == "on"
    try:
        outcomes = _run(kind, blob, post)
    except (dt.ValidationError, ValueError) as exc:
        g.db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("main.doc_upload", kind=kind))
    g.db.rollback()  # preview only - nothing is kept
    token = secrets.token_urlsafe(16)
    (_upload_dir() / f"{token}.xlsx").write_bytes(blob)
    return render_template("doc_preview.html", kind=kind, k=k, outcomes=outcomes, token=token, post=post,
                           filename=f.filename, confirmed=False)


@bp.route("/<kind>/upload/confirm", methods=["POST"])
@role_required("operator")
def doc_template_confirm(kind):
    k = _kind(kind)
    token = request.form.get("token", "")
    path = _upload_dir() / f"{token}.xlsx"
    if not token.replace("-", "").replace("_", "").isalnum() or not path.exists():
        flash("This upload has expired - upload the template again", "error")
        return redirect(url_for("main.doc_upload", kind=kind))
    post = request.form.get("post") == "1"
    try:
        outcomes = _run(kind, path.read_bytes(), post)
        g.db.commit()
    except (dt.ValidationError, ValueError) as exc:
        g.db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("main.doc_upload", kind=kind))
    path.unlink(missing_ok=True)
    done = sum(o.status in ("created", "posted") for o in outcomes)
    flash(f"{done} {UNIT[kind]}(s) created"
          + (f", {sum(o.status == 'error' for o in outcomes)} with errors - fix those rows and upload again"
             if any(o.status == "error" for o in outcomes) else ""), "ok")
    return render_template("doc_preview.html", kind=kind, k=k, outcomes=outcomes, token=None, post=post,
                           filename=None, confirmed=True)


UNIT = {"imports": "import", "exports": "shipment", "boms": "BOM version"}


def _run(kind, blob, post):
    if kind == "imports":
        return dt.apply_import_rows(g.db, dt.read_import_template(blob), post=post)
    if kind == "boms":
        return dt.apply_bom_rows(g.db, dt.read_bom_template(blob))
    return dt.apply_export_rows(g.db, dt.read_export_template(blob))
