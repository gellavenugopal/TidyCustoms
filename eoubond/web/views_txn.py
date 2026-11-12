"""Transaction screens: imports, production, exports, DTA clearances."""
from __future__ import annotations

from datetime import date

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import select

from ..excel import reports_to_xlsx
from ..models import (BOM, Bond, DisposalAllocation, DTAClearance, ExportLine, ExportShipment, FGModel,
                      ImportInvoice, Item, ProductionBatch, Supplier)
from ..services import dta as dta_svc
from ..services import exports as export_svc
from ..services import imports as import_svc
from ..services import production as prod_svc
from ..services.reports import Column, Report
from ..services.stock import available_fg_qty, available_import_qty
from . import int_or_none, login_required, parse_date, role_required
from .views import bp, commit_or_flash
import io


def _items():
    return list(g.db.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.code)))


def _models():
    return list(g.db.scalars(select(FGModel).where(FGModel.active.is_(True)).order_by(FGModel.code)))


def _bonds(active_only=True):
    stmt = select(Bond).order_by(Bond.bond_no)
    if active_only:
        stmt = stmt.where(Bond.status == "ACTIVE")
    return list(g.db.scalars(stmt))


def _get(model, id_):
    obj = g.db.get(model, id_)
    if obj is None:
        abort(404)
    return obj


# ------------------------------------------------------------------ imports
@bp.route("/imports")
@login_required
def imports():
    stmt = select(ImportInvoice).order_by(ImportInvoice.invoice_date.desc(), ImportInvoice.id.desc())
    if session.get("bond_id"):
        stmt = stmt.where(ImportInvoice.bond_id == session["bond_id"])
    return render_template("imports.html", invoices=list(g.db.scalars(stmt)))


@bp.route("/imports/new", methods=["GET", "POST"])
@role_required("operator")
def import_new():
    f = request.form
    cols = ["description", "item_id", "be_item_serial", "qty", "qty_received", "av", "bcd", "sws", "igst",
            "cess", "other", "dsv"]
    n = len(f.getlist("description"))
    get = lambda key, i: (f.getlist(key) + [""] * n)[i]  # noqa: E731
    rows = [{k: get(k, i) for k in cols} for i in range(n)] or [{k: "" for k in cols}]
    if request.method == "POST":
        lines = []
        for r in rows:
            if r["description"].strip() or r["qty"].strip():
                lines.append(dict(description=r["description"], item_id=int_or_none(r["item_id"]), qty=r["qty"],
                                  be_item_serial=r["be_item_serial"], qty_received=r["qty_received"],
                                  assessable_value=r["av"], duty_saved_value=r["dsv"], bcd_forgone=r["bcd"],
                                  sws_forgone=r["sws"], igst_forgone=r["igst"], cess_forgone=r["cess"],
                                  other_forgone=r["other"]))

        def go():
            return import_svc.create_import(
                g.db, invoice_no=f.get("invoice_no"), invoice_date=parse_date(f.get("invoice_date")),
                supplier_id=int(f.get("supplier_id") or 0), bond_id=int(f.get("bond_id") or 0) or None, lines=lines,
                import_type=f.get("import_type", "BOND"),
                be_no=f.get("be_no"), be_date=parse_date(f.get("be_date")), currency=f.get("currency"),
                port_of_import=f.get("port_of_import"), be_invoice_serial=f.get("be_invoice_serial"),
                clearance_date=parse_date(f.get("clearance_date")), receipt_date=parse_date(f.get("receipt_date")))
        ok, inv = commit_or_flash(go, "Import saved as draft")
        if ok:
            return redirect(url_for("main.import_view", invoice_id=inv.id))
    return render_template("import_new.html", suppliers=list(g.db.scalars(select(Supplier).order_by(Supplier.name))),
                           bonds=_bonds(), items=_items(), form=f, rows=rows)


@bp.route("/imports/<int:invoice_id>", methods=["GET", "POST"])
@login_required
def import_view(invoice_id):
    inv = _get(ImportInvoice, invoice_id)
    if request.method == "POST":
        if g.role == "viewer":
            flash("Viewers cannot edit", "error")
            return redirect(url_for("main.import_view", invoice_id=invoice_id))
        action = request.form.get("action")
        if action == "resolve":
            def go():
                n = 0
                for line in inv.lines:
                    chosen = int_or_none(request.form.get(f"map_{line.id}"))
                    if chosen and chosen != line.item_id:
                        import_svc.resolve_line(g.db, line.id, chosen,
                                                scope=request.form.get(f"scope_{line.id}", "supplier"))
                        n += 1
                return n
            commit_or_flash(go, "Mappings saved")
        elif action == "post":
            commit_or_flash(lambda: import_svc.post_import(g.db, invoice_id),
                            "Import posted and bond debited")
        elif action == "cancel":
            commit_or_flash(lambda: import_svc.cancel_import(g.db, invoice_id, date.today()),
                            "Import cancelled and bond recredited")
        elif action == "delete":
            if commit_or_flash(lambda: import_svc.delete_draft(g.db, invoice_id), "Draft deleted")[0]:
                return redirect(url_for("main.imports"))
        return redirect(url_for("main.import_view", invoice_id=invoice_id))
    return render_template("import_view.html", inv=inv, items=_items(),
                           unmapped=import_svc.unmapped_lines(inv))


# ------------------------------------------------------------------ production
@bp.route("/production")
@login_required
def production():
    rows = list(g.db.scalars(select(ProductionBatch).order_by(ProductionBatch.production_date.desc(),
                                                              ProductionBatch.id.desc())))
    return render_template("production.html", batches=rows)


@bp.route("/production/new", methods=["GET", "POST"])
@role_required("operator")
def production_new():
    f = request.form
    preview = bom = None
    if request.method == "POST":
        args = dict(model_id=int(f.get("model_id") or 0), qty_units=f.get("qty"),
                    production_date=parse_date(f.get("production_date"), date.today()),
                    bom_id=int_or_none(f.get("bom_id")), bond_id=int_or_none(f.get("bond_id")))
        if f.get("action") == "post":
            ok, batch = commit_or_flash(lambda: prod_svc.produce(g.db, batch_no=f.get("batch_no"),
                                                                 remarks=f.get("remarks"), **args),
                                        "Production posted; components consumed FIFO")
            if ok:
                return redirect(url_for("main.production_view", batch_id=batch.id))
        try:
            bom, preview = prod_svc.availability(g.db, **args)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            flash(str(exc), "error")
    models = _models()
    boms = list(g.db.scalars(select(BOM).order_by(BOM.model_id, BOM.version)))
    return render_template("production_new.html", models=models, boms=boms, bonds=_bonds(False), form=f,
                           preview=preview, bom=bom)


@bp.route("/production/<int:batch_id>")
@login_required
def production_view(batch_id):
    return render_template("production_view.html", b=_get(ProductionBatch, batch_id))


# ------------------------------------------------------------------ export / DTA line parsing
def _parse_targets(f, extra_fields):
    lines = []
    for idx, target in enumerate(f.getlist("target")):
        q = f.getlist("qty")[idx]
        if not target or not q.strip():
            continue
        kind, _, tid = target.partition(":")
        ln = dict(line_type=kind, qty=q, model_id=int(tid) if kind == "FG" else None,
                  item_id=int(tid) if kind == "ITEM" else None)
        for key in extra_fields:
            ln[key] = f.getlist(key)[idx]
        lines.append(ln)
    return lines


def _stock_options(on: date):
    fg = [(m, available_fg_qty(g.db, m.id, on)) for m in _models()]
    it = [(i, available_import_qty(g.db, i.id, on)) for i in _items()]
    return fg, it


# ------------------------------------------------------------------ exports
@bp.route("/exports")
@login_required
def exports():
    rows = list(g.db.scalars(select(ExportShipment).order_by(ExportShipment.export_date.desc(),
                                                             ExportShipment.id.desc())))
    return render_template("exports.html", shipments=rows)


@bp.route("/exports/new", methods=["GET", "POST"])
@role_required("operator")
def export_new():
    f = request.form
    if request.method == "POST":
        lines = _parse_targets(f, ["export_item_name", "fob_value"])
        ok, sh = commit_or_flash(lambda: export_svc.post_export(
            g.db, invoice_no=f.get("invoice_no"), export_date=parse_date(f.get("export_date"), date.today()),
            shipping_bill_no=f.get("shipping_bill_no"), shipping_bill_date=parse_date(f.get("shipping_bill_date")),
            consignee=f.get("consignee"), country=f.get("country"), port_of_export=f.get("port_of_export"),
            lines=lines),
            "Export posted; bond recredited")
        if ok:
            return redirect(url_for("main.export_view", shipment_id=sh.id))
    fg, it = _stock_options(date.today())
    return render_template("export_new.html", fg=fg, items=it, form=f)


@bp.route("/exports/<int:shipment_id>")
@login_required
def export_view(shipment_id):
    sh = _get(ExportShipment, shipment_id)
    trace = {l.id: list(g.db.scalars(select(DisposalAllocation).where(DisposalAllocation.export_line_id == l.id)
                                     .order_by(DisposalAllocation.id))) for l in sh.lines}
    return render_template("export_view.html", sh=sh, trace=trace)


# ------------------------------------------------------------------ DTA
@bp.route("/dta")
@login_required
def dta():
    rows = list(g.db.scalars(select(DTAClearance).order_by(DTAClearance.clearance_date.desc(),
                                                           DTAClearance.id.desc())))
    return render_template("dta.html", clearances=rows)


@bp.route("/dta/new", methods=["GET", "POST"])
@role_required("operator")
def dta_new():
    f = request.form
    if request.method == "POST":
        lines = _parse_targets(f, ["description", "sale_value"])
        ok, clr = commit_or_flash(lambda: dta_svc.post_dta(
            g.db, clearance_no=f.get("clearance_no"), clearance_date=parse_date(f.get("clearance_date"), date.today()),
            customer=f.get("customer"), be_no=f.get("be_no"), lines=lines), "DTA clearance recorded")
        if ok:
            return redirect(url_for("main.dta_view", clearance_id=clr.id))
    fg, it = _stock_options(date.today())
    return render_template("dta_new.html", fg=fg, items=it, form=f)


def _dta_computation_report(clr) -> list[Report]:
    lines = Report(f"DTA duty computation - {clr.clearance_no}", [
        Column("line_no", "Line"), Column("description", "Goods"), Column("qty", "Qty", "qty"),
        Column("av", "Assessable value (inputs)", "money"), Column("bcd", "BCD", "money"),
        Column("sws", "SWS", "money"), Column("duty", "Duty payable", "money")],
        [dict(line_no=l.line_no, description=l.description, qty=l.qty, av=l.assessable_value, bcd=l.bcd,
              sws=l.sws, duty=l.duty) for l in clr.lines],
        subtitle=(f"Date {clr.clearance_date:%d-%m-%Y} | Customer {clr.customer or '-'} | BCD + SWS only; "
                  "GST is payable separately on the domestic supply"),
        totals=dict(av=clr.total_av, bcd=clr.total_bcd, sws=clr.total_sws, duty=clr.total_duty),
        sheet="Duty per line")
    detail = Report("Input-wise working", [
        Column("line_no", "Line"), Column("item", "Input"), Column("import_ref", "Import inv/line"),
        Column("bond", "Bond"), Column("qty", "Qty", "qty"), Column("av", "AV", "money"),
        Column("bcd_rate", "BCD %", "rate"), Column("bcd", "BCD", "money"), Column("sws_rate", "SWS % of BCD", "rate"),
        Column("sws", "SWS", "money"), Column("dsv", "Duty saved (bond)", "money")],
        dta_svc.computation_rows(g.db, clr), sheet="Input working",
        subtitle="Paise-level working; line totals are rounded to the rupee when the rule is on")
    return [lines, detail]


@bp.route("/dta/<int:clearance_id>", methods=["GET", "POST"])
@login_required
def dta_view(clearance_id):
    clr = _get(DTAClearance, clearance_id)
    if request.method == "POST":
        if g.role == "viewer":
            flash("Viewers cannot edit", "error")
        else:
            commit_or_flash(lambda: dta_svc.mark_paid(
                g.db, clearance_id, challan_no=request.form.get("challan_no", ""),
                paid_date=parse_date(request.form.get("paid_date"), date.today())), "Duty marked paid")
        return redirect(url_for("main.dta_view", clearance_id=clearance_id))
    if request.args.get("format") == "xlsx":
        return send_file(io.BytesIO(reports_to_xlsx(*_dta_computation_report(clr))),
                         as_attachment=True, download_name=f"dta-{clr.clearance_no}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    summary, detail = _dta_computation_report(clr)
    return render_template("dta_view.html", clr=clr, summary=summary, detail=detail)
