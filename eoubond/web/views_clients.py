"""Client selection and administration."""
import secrets
from flask import abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from ..models import AdminAuditLog, Client, ClientAccess, User
from ..services.errors import BusinessError
from . import login_required, role_required
from .views import bp, commit_or_flash

ROLES = ("viewer", "operator", "admin")


@bp.route("/admin/audit")
@login_required
def admin_audit():
    if not g.user.is_superadmin:
        abort(403)
    entries = list(g.db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(500)))
    return render_template("admin_audit.html", entries=entries)


@bp.route("/select-client", methods=["POST"])
@login_required
def select_client():
    try:
        client_id = int(request.form.get("client_id", ""))
    except ValueError:
        abort(400)
    if not any(c.id == client_id for c in g.clients):
        abort(403)
    session["client_id"] = client_id
    session.pop("bond_id", None)
    session["_csrf"] = secrets.token_urlsafe(32)
    return redirect(url_for("main.dashboard"))


@bp.route("/clients", methods=["GET", "POST"])
@login_required
def clients():
    if request.method == "POST":
        if not g.user.is_superadmin:
            abort(403)

        def save():
            name = request.form.get("name", "").strip()
            if not name or len(name) > 128:
                raise BusinessError("Client name is required and must be at most 128 characters")
            client_id = request.form.get("client_id")
            client = g.db.get(Client, int(client_id)) if client_id else Client(name=name)
            if not client:
                raise BusinessError("Client not found")
            client.name = name
            client.iec_code = request.form.get("iec_code", "").strip() or None
            client.gstin = request.form.get("gstin", "").strip() or None
            client.address = request.form.get("address", "").strip() or None
            if len(client.iec_code or "") > 32 or len(client.gstin or "") > 32:
                raise BusinessError("IEC and GSTIN must be at most 32 characters")
            g.db.add(client)
            g.db.flush()

        commit_or_flash(save, "Client saved")
        return redirect(url_for("main.clients"))
    return render_template("clients.html", clients=g.clients)


@bp.route("/users", methods=["GET", "POST"])
@role_required("admin")
def users():
    if request.method == "POST":
        def change():
            action = request.form.get("action", "")
            role = request.form.get("role", "viewer")
            if action in ("create", "assign", "role") and role not in ROLES:
                raise BusinessError("Invalid client role")
            if action == "create":
                name = request.form.get("username", "").strip()
                password = request.form.get("password", "")
                if not name or len(name) > 64 or len(password) < 8:
                    raise BusinessError("Username and a password of at least 8 characters are required")
                if g.db.scalar(select(User.id).where(User.username == name)):
                    raise BusinessError("Username already exists; assign the existing user instead")
                user = User(username=name, full_name=request.form.get("full_name") or None,
                            password_hash=generate_password_hash(password), role=role)
                g.db.add(user)
                g.db.flush()
                g.db.add(ClientAccess(user_id=user.id, client_id=g.client.id, role=role))
                return
            if action == "assign":
                name = request.form.get("username", "").strip()
                user = g.db.scalar(select(User).where(User.username == name))
                if not user:
                    raise BusinessError("User not found")
                if g.db.get(ClientAccess, (user.id, g.client.id)):
                    raise BusinessError("User already has access to this client")
                g.db.add(ClientAccess(user_id=user.id, client_id=g.client.id, role=role))
                return
            try:
                user = g.db.get(User, int(request.form.get("user_id", "")))
            except ValueError:
                raise BusinessError("User not found")
            if not user or user.id == g.user.id:
                raise BusinessError("User not found or cannot change your own account here")
            access = g.db.get(ClientAccess, (user.id, g.client.id))
            if not g.user.is_superadmin and (not access or user.is_superadmin):
                abort(403)
            if action in ("role", "remove"):
                if not access:
                    raise BusinessError("User does not have access to this client")
                if action == "role":
                    access.role = role
                else:
                    g.db.delete(access)
            elif action in ("toggle", "password"):
                count = g.db.scalar(select(func.count()).select_from(ClientAccess).where(ClientAccess.user_id == user.id))
                if not g.user.is_superadmin and count > 1:
                    raise BusinessError("Only a super-admin can change an account used by multiple clients")
                if action == "toggle":
                    user.active = not user.active
                else:
                    password = request.form.get("password", "")
                    if len(password) < 8:
                        raise BusinessError("Password must be at least 8 characters")
                    user.password_hash = generate_password_hash(password)
            elif action == "superadmin" and g.user.is_superadmin:
                user.is_superadmin = not user.is_superadmin
            else:
                raise BusinessError("Invalid action")

        commit_or_flash(change, "User updated")
        return redirect(url_for("main.users"))
    stmt = select(User).order_by(User.username)
    if not g.user.is_superadmin:
        stmt = stmt.join(ClientAccess).where(ClientAccess.client_id == g.client.id)
    user_list = list(g.db.scalars(stmt))
    access = {a.user_id: a for a in g.db.scalars(select(ClientAccess).where(ClientAccess.client_id == g.client.id))}
    return render_template("users.html", users=user_list, access=access, roles=ROLES)
