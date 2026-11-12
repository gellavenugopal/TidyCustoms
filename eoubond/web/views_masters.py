"""Master data screens: items, suppliers, models + BOM versions, description
mappings, bonds."""
from __future__ import annotations

from flask import flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from ..models import BOM, Bond, DescriptionMapping, FGModel, Item, Supplier
from ..services import bonds as bond_svc
from ..services import masters
from . import int_or_none, login_required, parse_date, role_required
from .views import bp, commit_or_flash


def _items():
    return list(g.db.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.code)))


# ------------------------------------------------------------------ items
@bp.route("/items", methods=["GET"])
@login_required
def items():
    rows = list(g.db.scalars(select(Item).order_by(Item.code)))
    return render_template("items.html", items=rows)


@bp.route("/items", methods=["POST"])
@role_required("operator")
def items_create():
    f = request.form
    commit_or_flash(lambda: masters.create_item(
        g.db, code=f.get("code"), description=f.get("description"), uom=f.get("uom"),
        item_type=f.get("item_type", "COMPONENT"), hs_code=f.get("hs_code"),
        bcd_rate=f.get("bcd_rate"), sws_rate=f.get("sws_rate")), "Item created")
    return redirect(url_for("main.items"))


@bp.route("/items/<int:item_id>", methods=["GET", "POST"])
@login_required
def item_edit(item_id):
    item = g.db.get(Item, item_id) or _404()
    if request.method == "POST":
        if g.user.role == "viewer":
            flash("Viewers cannot edit", "error")
        elif request.form.get("action") == "delete":
            if commit_or_flash(lambda: masters.delete_row(g.db, item), "Item deleted")[0]:
                return redirect(url_for("main.items"))
        else:
            f = request.form
            commit_or_flash(lambda: masters.update_item(
                g.db, item_id, description=f.get("description"), uom=f.get("uom"), item_type=f.get("item_type"),
                hs_code=f.get("hs_code") or None, bcd_rate=f.get("bcd_rate"), sws_rate=f.get("sws_rate"),
                active=f.get("active") == "on"), "Item saved")
        return redirect(url_for("main.item_edit", item_id=item_id))
    return render_template("item_edit.html", item=item)


def _404():
    from flask import abort
    abort(404)


# ------------------------------------------------------------------ suppliers
@bp.route("/suppliers", methods=["GET", "POST"])
@login_required
def suppliers():
    if request.method == "POST":
        if g.user.role == "viewer":
            flash("Viewers cannot edit", "error")
        elif request.form.get("action") == "delete":
            s = g.db.get(Supplier, int(request.form["supplier_id"]))
            commit_or_flash(lambda: masters.delete_row(g.db, s), "Supplier deleted")
        else:
            commit_or_flash(lambda: masters.create_supplier(g.db, name=request.form.get("name"),
                                                            country=request.form.get("country")), "Supplier added")
        return redirect(url_for("main.suppliers"))
    return render_template("suppliers.html", suppliers=list(g.db.scalars(select(Supplier).order_by(Supplier.name))))


# ------------------------------------------------------------------ models & BOMs
@bp.route("/models", methods=["GET", "POST"])
@login_required
def models():
    if request.method == "POST":
        if g.user.role == "viewer":
            flash("Viewers cannot edit", "error")
        else:
            commit_or_flash(lambda: masters.create_model(g.db, code=request.form.get("code"),
                                                         name=request.form.get("name")), "Model added")
        return redirect(url_for("main.models"))
    return render_template("models.html", models=list(g.db.scalars(select(FGModel).order_by(FGModel.code))))


@bp.route("/models/<int:model_id>/bom/new", methods=["GET", "POST"])
@role_required("operator")
def bom_new(model_id):
    model = g.db.get(FGModel, model_id) or _404()
    if request.method == "POST":
        f = request.form
        lines = [dict(item_id=int(i), qty_per_unit=q) for i, q in zip(f.getlist("item_id"), f.getlist("qty"))
                 if i and q]

        def go():
            return masters.create_bom_version(g.db, model_id=model_id, effective_from=parse_date(f.get("effective_from")),
                                              lines=lines, remarks=f.get("remarks"))
        ok, bom = commit_or_flash(go, "BOM version created")
        if ok:
            return redirect(url_for("main.bom_view", bom_id=bom.id))
    latest = model.boms[-1] if model.boms else None
    return render_template("bom_new.html", model=model, latest=latest, items=_items())


@bp.route("/bom/<int:bom_id>")
@login_required
def bom_view(bom_id):
    bom = g.db.get(BOM, bom_id) or _404()
    return render_template("bom_view.html", bom=bom)


# ------------------------------------------------------------------ mappings
@bp.route("/mappings", methods=["GET", "POST"])
@login_required
def mappings():
    if request.method == "POST":
        f = request.form
        if g.user.role == "viewer":
            flash("Viewers cannot edit", "error")
        elif f.get("action") == "delete":
            m = g.db.get(DescriptionMapping, int(f["mapping_id"]))
            commit_or_flash(lambda: masters.delete_row(g.db, m), "Mapping removed")
        else:
            commit_or_flash(lambda: masters.save_mapping(
                g.db, description=f.get("description", ""), item_id=int(f.get("item_id") or 0),
                supplier_id=int_or_none(f.get("supplier_id"))), "Mapping saved")
        return redirect(url_for("main.mappings"))
    q = (request.args.get("q") or "").strip()
    stmt = select(DescriptionMapping).order_by(DescriptionMapping.normalized_description)
    if q:
        stmt = stmt.where(DescriptionMapping.normalized_description.contains(masters.normalize_description(q)))
    return render_template("mappings.html", mappings=list(g.db.scalars(stmt)), q=q, items=_items(),
                           suppliers=list(g.db.scalars(select(Supplier).order_by(Supplier.name))))


# ------------------------------------------------------------------ bonds
@bp.route("/bonds", methods=["GET", "POST"])
@login_required
def bonds():
    if request.method == "POST":
        f = request.form
        if g.user.role == "viewer":
            flash("Viewers cannot edit", "error")
        else:
            commit_or_flash(lambda: bond_svc.create_bond(
                g.db, bond_no=f.get("bond_no"), bond_amount=f.get("bond_amount"),
                valid_from=parse_date(f.get("valid_from")), valid_to=parse_date(f.get("valid_to")),
                remarks=f.get("remarks")), "Bond created")
        return redirect(url_for("main.bonds"))
    return render_template("bonds.html", bonds=list(g.db.scalars(select(Bond).order_by(Bond.status, Bond.bond_no))))


@bp.route("/bonds/<int:bond_id>", methods=["GET", "POST"])
@login_required
def bond_view(bond_id):
    bond = g.db.get(Bond, bond_id) or _404()
    if request.method == "POST":
        f = request.form
        if g.user.role == "viewer":
            flash("Viewers cannot edit", "error")
        else:
            commit_or_flash(lambda: bond_svc.update_bond(
                g.db, bond_id, bond_amount=f.get("bond_amount") or None,
                valid_from=parse_date(f.get("valid_from")), valid_to=parse_date(f.get("valid_to")),
                remarks=f.get("remarks"), status=f.get("status")), "Bond updated")
        return redirect(url_for("main.bond_view", bond_id=bond_id))
    return redirect(url_for("main.report", name="ledger", bond_id=bond_id))


@bp.route("/bonds/<int:bond_id>/edit")
@role_required("operator")
def bond_edit(bond_id):
    return render_template("bond_edit.html", bond=g.db.get(Bond, bond_id) or _404())
