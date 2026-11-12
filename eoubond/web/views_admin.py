"""Backup & restore screens, and first-run setup (create admin or restore a backup)."""
from __future__ import annotations

import io
from datetime import datetime

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from .. import backup as bk
from .. import config
from ..models import User
from . import role_required
from .views import bp

MAX_UPLOAD = 512 * 1024 * 1024


def _release_db():
    """Finish the request's own transaction so the restore can take the write lock."""
    s = g.get("db")
    if s is not None:
        s.rollback()
        s.close()


def _do_restore(by: str, client_id: int | None = None):
    f = request.files.get("backup_file")
    if not f or not f.filename:
        flash("Choose a backup file (.zip) to restore", "error")
        return False
    blob = f.read(MAX_UPLOAD + 1)
    if len(blob) > MAX_UPLOAD:
        flash("File too large", "error")
        return False
    _release_db()
    try:
        res = bk.restore_backup(blob, restore_rules=request.form.get("restore_rules") == "on",
                                restored_by=by, client_id=client_id)
    except bk.BackupError as exc:
        flash(str(exc), "error")
        return False
    except IntegrityError:
        flash("Restore failed: the backup conflicts with existing data. No changes were saved.", "error")
        return False
    m = res["manifest"]
    msg = f"Restored {res['rows']} records from the backup taken {m['created_at'][:16].replace('T', ' ')} UTC."
    if res["safety_backup"]:
        msg += f" The data that was here before is saved as {res['safety_backup']}."
    if res["rules_restored"]:
        msg += " Business rules restored from the backup too."
    if client_id is None:
        token = session.get("_csrf")
        session.clear()
        session["_csrf"] = token
        msg += " Sign in with a user from the backup."
    flash(msg, "ok")
    return True


@bp.route("/admin/backup")
@role_required("admin")
def backup_page():
    files = ([dict(name=p.name, size=p.stat().st_size, mtime=datetime.fromtimestamp(p.stat().st_mtime))
              for p in bk.list_backups()] if g.user.is_superadmin else [])
    return render_template("backup.html", files=files, backup_dir=config.backup_dir(),
                           safe=config.backup_dir_is_safe(), keep=config.backup_keep(),
                           interval=config.backup_interval_hours())


@bp.route("/admin/backup/download")
@role_required("admin")
def backup_download():
    """Make a backup now: saved in the backup folder AND downloaded to the browser."""
    scope = request.args.get("scope", "client")
    if scope not in ("client", "system"):
        abort(400)
    if scope == "system" and not g.user.is_superadmin:
        abort(403)
    if scope == "client":
        data = bk.create_backup(client_id=g.client.id)
        name = f"eoubond-{g.client.code}-{datetime.now():%Y%m%d-%H%M%S}.zip"
        return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype="application/zip")
    try:
        path = bk.write_backup(kind="manual")
        data = path.read_bytes()
        name = path.name
    except OSError:  # backup folder not writable: still give the user the file
        data, name = bk.create_backup(), f"eoubond-manual-{datetime.now():%Y%m%d-%H%M%S}.zip"
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype="application/zip")


@bp.route("/admin/backup/file/<name>")
@role_required("admin")
def backup_file(name):
    if not g.user.is_superadmin:
        abort(403)
    for p in bk.list_backups():
        if p.name == name:  # only files listed in the backup folder
            return send_file(p, as_attachment=True, download_name=p.name, mimetype="application/zip")
    abort(404)


@bp.route("/admin/backup/restore", methods=["POST"])
@role_required("admin")
def backup_restore():
    scope = request.form.get("scope", "client")
    if scope not in ("client", "system"):
        abort(400)
    if scope == "system" and not g.user.is_superadmin:
        abort(403)
    if request.form.get("confirm", "").strip() != "RESTORE":
        flash("Type RESTORE in the box to confirm - restoring replaces all current data", "error")
        return redirect(url_for("main.backup_page"))
    if _do_restore(g.user.username, g.client.id if scope == "client" else None):
        return redirect(url_for("main.backup_page" if scope == "client" else "main.login"))
    return redirect(url_for("main.backup_page"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only (no users yet): create the first admin, or restore a backup."""
    if bk.has_users():
        return redirect(url_for("main.login"))
    if request.method == "POST":
        if request.form.get("action") == "restore":
            if _do_restore("first-run-restore"):
                return redirect(url_for("main.login"))
        else:
            name = request.form.get("username", "").strip()
            pw = request.form.get("password", "")
            if not name or len(pw) < 8 or pw != request.form.get("password2"):
                flash("Enter a username and the same password (8+ characters) twice", "error")
            else:
                g.db.add(User(username=name, full_name=request.form.get("full_name") or None, role="admin",
                              is_superadmin=True,
                              password_hash=generate_password_hash(pw)))
                g.db.commit()
                flash("Administrator created - sign in", "ok")
                return redirect(url_for("main.login"))
    return render_template("setup.html")
