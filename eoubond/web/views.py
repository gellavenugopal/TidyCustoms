"""Blueprint, login/logout, dashboard, bond selection, users and the shared
'run a service and commit' helper."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from werkzeug.security import check_password_hash, generate_password_hash

from ..models import Bond, DTAClearance, ImportInvoice, ImportLine, User
from ..services.errors import BusinessError
from . import login_required, role_required

bp = Blueprint("main", __name__)


def commit_or_flash(fn, success: str | None = None):
    """Run fn() inside the request transaction. Commit on success; on a business
    rule violation roll everything back and show the message. Returns (ok, result)."""
    try:
        result = fn()
        g.db.commit()
        if success:
            flash(success, "ok")
        return True, result
    except (BusinessError, ValueError) as exc:
        g.db.rollback()
        flash(str(exc), "error")
        return False, None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.db.scalar(select(func.count(User.id))) == 0:
        return redirect(url_for("main.setup"))  # fresh install
    if request.method == "POST":
        u = g.db.scalar(select(User).where(User.username == request.form.get("username", "").strip()))
        if u and u.active and check_password_hash(u.password_hash, request.form.get("password", "")):
            token = session.get("_csrf")
            session.clear()
            session["uid"] = u.id
            session["_csrf"] = token
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.dashboard"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/")
@login_required
def dashboard():
    from datetime import datetime

    from ..services import dashboard as dash
    from . import charts

    d = dash.build(g.db)
    month_lbl = [datetime.strptime(m, "%Y-%m").strftime("%b") for m in d["months"]]
    month_full = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in d["months"]]
    monthly_chart = charts.columns(
        month_lbl, [("Debits (imports)", "--series-1", [d["monthly"][m]["debit"] for m in d["months"]]),
                    ("Credits (exports / DTA)", "--series-2", [d["monthly"][m]["credit"] for m in d["months"]])],
        title="Bond debits and credits by month", tip_cats=month_full)
    items_chart = charts.hbars([(i["item"], i["dsv"], f'{i["item"]} - {i["description"]}: {charts.inr(i["dsv"])}')
                                for i in d["top_items"]], title="Duty saved in stock by item")
    share_chart = charts.share_bar([("Raw material in stock", "--series-1", d["rm_dsv"]),
                                    ("Inside unsold finished goods", "--series-2", d["fg_dsv"])],
                                   title="Where the duty-saved stock sits")
    return render_template("dashboard.html", d=d, inr=charts.inr, monthly_chart=monthly_chart,
                           items_chart=items_chart, share_chart=share_chart, month_full=month_full)


@bp.route("/select-bond", methods=["POST"])
@login_required
def select_bond():
    bid = request.form.get("bond_id")
    session["bond_id"] = int(bid) if bid else None
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.route("/users", methods=["GET", "POST"])
@role_required("admin")
def users():
    if request.method == "POST":
        def go():
            action = request.form.get("action")
            if action == "create":
                name = request.form.get("username", "").strip()
                pw = request.form.get("password", "")
                if not name or len(pw) < 8:
                    raise BusinessError("Username and a password of at least 8 characters are required")
                if g.db.scalar(select(User).where(User.username == name)):
                    raise BusinessError("Username already exists")
                g.db.add(User(username=name, full_name=request.form.get("full_name") or None,
                              password_hash=generate_password_hash(pw), role=request.form.get("role", "viewer")))
            else:
                u = g.db.get(User, int(request.form["user_id"]))
                if u.id == g.user.id:
                    raise BusinessError("You cannot change your own account here")
                if action == "toggle":
                    u.active = not u.active
                elif action == "role":
                    u.role = request.form.get("role", u.role)
                elif action == "password":
                    pw = request.form.get("password", "")
                    if len(pw) < 8:
                        raise BusinessError("Password must be at least 8 characters")
                    u.password_hash = generate_password_hash(pw)
            g.db.flush()
        commit_or_flash(go, "User updated")
        return redirect(url_for("main.users"))
    return render_template("users.html", users=list(g.db.scalars(select(User).order_by(User.username))))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        def go():
            if not check_password_hash(g.user.password_hash, request.form.get("current", "")):
                raise BusinessError("Current password is wrong")
            new = request.form.get("new", "")
            if len(new) < 8:
                raise BusinessError("New password must be at least 8 characters")
            g.user.password_hash = generate_password_hash(new)
            g.db.flush()
        ok, _ = commit_or_flash(go, "Password changed")
        if ok:
            return redirect(url_for("main.dashboard"))
    return render_template("password.html")
