"""Flask application factory, authentication, CSRF and request-scoped DB session."""
from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from flask import Flask, abort, flash, g, redirect, request, session, url_for
from sqlalchemy import select

from .. import config, db
from ..audit_context import set_current_user
from ..models import Bond, Client, ClientAccess, SystemState, User

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


def create_app(database_url: str | None = None, create_all: bool = False, testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY=config.secret_key(), TESTING=testing,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=config.os.environ.get("SESSION_COOKIE_SECURE", "0") == "1")
    db.init_engine(database_url, create_all=create_all)

    @app.before_request
    def _open_session():
        # GET pages only read: don't hold SQLite's write lock for the whole request
        g.db = db.SessionLocal() if request.method == "POST" else db.SessionLocal(bind=db.read_only())
        g.user = None
        g.client = None
        g.role = None
        g.clients = []
        uid = session.get("uid")
        if uid and session.get("epoch") != g.db.get(SystemState, 1).session_epoch:
            session.clear()
            uid = None
        if uid:
            u = g.db.get(User, uid)
            if u and u.active:
                g.user = u
                if u.is_superadmin:
                    g.clients = list(g.db.scalars(select(Client).where(Client.active.is_(True)).order_by(Client.name)))
                else:
                    g.clients = list(g.db.scalars(select(Client).join(ClientAccess).where(
                        ClientAccess.user_id == u.id, Client.active.is_(True)).order_by(Client.name)))
                chosen = session.get("client_id")
                g.client = next((c for c in g.clients if c.id == chosen), None)
                if chosen is not None and g.client is None:
                    session["_csrf"] = secrets.token_urlsafe(32)
                    session.pop("client_id", None)
                    session.pop("bond_id", None)
                    flash("Your previous client is no longer available.", "error")
                if g.client is None and g.clients:
                    g.client = g.clients[0]
                    session["client_id"] = g.client.id
                    session.pop("bond_id", None)
                if g.client:
                    access = g.db.get(ClientAccess, (u.id, g.client.id))
                    g.role = "admin" if u.is_superadmin else access.role
                    g.db.info["client_id"] = g.client.id
        set_current_user(g.user.username if g.user else None)
        if request.method == "POST" and not app.config.get("WTF_CSRF_DISABLED"):
            token = request.form.get("_csrf")
            if not token or token != session.get("_csrf"):
                abort(400, "Invalid or missing CSRF token - reload the page and try again")
        if g.user and not g.client and request.endpoint and request.endpoint.startswith("main."):
            allowed = {"main.login", "main.logout", "main.clients", "main.admin_audit", "main.change_password",
                       "main.setup", "main.select_client"}
            if request.endpoint not in allowed:
                flash("You have no client access. Contact a super-admin.", "error")
                return redirect(url_for("main.clients"))

    @app.teardown_request
    def _close_session(exc):
        s = g.pop("db", None)
        if s is not None:
            if exc is not None:
                s.rollback()
            s.close()
        set_current_user(None)

    def csrf_value() -> str:
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    app.jinja_env.globals["csrf_value"] = csrf_value

    # Pages are read fresh from disk and CSS/JS links carry the file's timestamp,
    # so after an update the browser never mixes an old page with a new stylesheet.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    def asset_url(filename: str) -> str:
        import os

        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = 0
        return url_for("static", filename=filename, v=v)

    app.jinja_env.globals["asset_url"] = asset_url

    @app.context_processor
    def _inject():
        csrf_value()
        bonds = []
        if getattr(g, "client", None):
            bonds = list(g.db.scalars(select(Bond).order_by(Bond.status, Bond.bond_no)))
        return dict(csrf_token=session["_csrf"], current_user=g.get("user"), all_bonds=bonds,
                    selected_bond_id=session.get("bond_id"), today=date.today(),
                    current_client=g.get("client"), available_clients=g.get("clients", []),
                    current_role=g.get("role"),
                    can_edit=bool(g.get("role") and ROLE_RANK[g.role] >= 1),
                    is_admin=bool(g.get("role") == "admin"),
                    is_superadmin=bool(g.get("user") and g.user.is_superadmin), rules=config.get_rules(),
                    brand=config.brand())

    app.jinja_env.filters["money"] = fmt_money
    app.jinja_env.filters["qty"] = fmt_qty
    app.jinja_env.filters["dt"] = fmt_date
    app.jinja_env.filters["cell"] = fmt_cell

    app.config["MAX_CONTENT_LENGTH"] = 520 * 1024 * 1024  # backup uploads
    from . import views_admin, views_clients, views_docs, views_masters, views_reports, views_txn  # noqa: F401
    from .views import bp

    app.register_blueprint(bp)
    return app


# ------------------------------------------------------------------ formatting
def fmt_money(v):
    if v is None or v == "":
        return ""
    return f"{Decimal(v):,.2f}"


def fmt_qty(v):
    if v is None or v == "":
        return ""
    s = f"{Decimal(v):,.3f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_date(v):
    return v.strftime("%d-%m-%Y") if isinstance(v, (date, datetime)) else (v or "")


def fmt_cell(v, kind):
    if kind == "money":
        return fmt_money(v)
    if kind == "qty":
        return fmt_qty(v)
    if kind == "date":
        return fmt_date(v)
    return "" if v is None else v


# ------------------------------------------------------------------ access control
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("main.login", next=request.path))
        return fn(*a, **kw)

    return wrapper


def role_required(role: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("main.login", next=request.path))
            if not g.role or ROLE_RANK[g.role] < ROLE_RANK[role]:
                flash(f"Your role ({g.role or 'none'}) cannot do this; {role} access is needed.", "error")
                return redirect(request.referrer or url_for("main.dashboard"))
            return fn(*a, **kw)

        return wrapper

    return deco


# ------------------------------------------------------------------ form helpers
def parse_date(value: str | None, default: date | None = None) -> date | None:
    value = (value or "").strip()
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value}")


def int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
