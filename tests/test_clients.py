import hashlib
import io
import json
import re
import zipfile
from datetime import date

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from eoubond import backup, db
from eoubond.models import BOM, Bond, Client, ClientAccess, Item, User
from eoubond.services import masters
from eoubond.services.errors import ValidationError
from eoubond.web import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("EOU_DATA_DIR", str(tmp_path))
    app = create_app(f"sqlite:///{(tmp_path / 'clients.db').as_posix()}", create_all=True, testing=True)
    with db.SessionLocal() as s:
        s.add(User(username="root", password_hash=generate_password_hash("password1"),
                   role="admin", is_superadmin=True))
        s.commit()
    yield app
    db.engine.dispose()


def login(c, username, password="password1"):
    html = c.get("/login").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    assert c.post("/login", data={"username": username, "password": password, "_csrf": token}).status_code == 302
    return token


def post(c, url, token, **data):
    return c.post(url, data={"_csrf": token, **data}, follow_redirects=True)


def make_client(c, token, name):
    response = post(c, "/clients", token, name=name, iec_code="IEC", gstin="GST", address="Address")
    assert "Client saved" in response.get_data(as_text=True)
    with db.SessionLocal() as s:
        return s.scalar(select(Client.id).where(Client.name == name))


def choose(c, token, client_id):
    return c.post("/select-client", data={"_csrf": token, "client_id": str(client_id)})


def current_token(c):
    with c.session_transaction() as session:
        return session["_csrf"]


def make_bond(c, token, number, amount):
    response = post(c, "/bonds", token, bond_no=number, bond_amount=str(amount),
                    valid_from="2026-01-01", valid_to="2027-01-01")
    assert "Bond created" in response.get_data(as_text=True)


def test_client_selection_roles_and_data_isolation(app):
    root = app.test_client()
    token = login(root, "root")
    second = make_client(root, token, "Client Two")
    make_bond(root, token, "SAME", 100)
    make_bond(root, token, "ONLY-FIRST", 100)
    assert "User updated" in post(root, "/users", token, action="create", username="worker",
                                  password="password1", role="operator").get_data(as_text=True)
    assert choose(root, token, second).status_code == 302
    old_token, token = token, current_token(root)
    assert root.post("/bonds", data={"_csrf": old_token, "bond_no": "STALE"}).status_code == 400
    make_bond(root, token, "SAME", 200)
    make_bond(root, token, "ONLY-SECOND", 200)
    assert "User updated" in post(root, "/users", token, action="assign", username="worker",
                                  role="viewer").get_data(as_text=True)
    html = root.get("/bonds").get_data(as_text=True)
    assert "ONLY-SECOND" in html and "ONLY-FIRST" not in html
    html = root.get("/").get_data(as_text=True)
    assert "ONLY-SECOND" in html and "ONLY-FIRST" not in html
    assert "ONLY-FIRST" not in root.get("/reports/monthly").get_data(as_text=True)
    workbook = load_workbook(io.BytesIO(root.get("/reports/all.xlsx").data), data_only=True)
    values = [str(cell.value) for sheet in workbook for row in sheet for cell in row if cell.value is not None]
    assert any("ONLY-SECOND" in v for v in values)
    assert not any("ONLY-FIRST" in v for v in values)
    with db.SessionLocal() as s:
        first_id = s.scalar(select(Bond.id).where(Bond.bond_no == "ONLY-FIRST"))
        s.info["client_id"] = second
        assert s.scalar(select(func.sum(Bond.bond_amount))) == 400
    assert root.get(f"/bonds/{first_id}/edit").status_code == 404

    worker = app.test_client()
    worker_token = login(worker, "worker")
    assert choose(worker, worker_token, second).status_code == 302
    worker_token = current_token(worker)
    html = worker.get("/bonds").get_data(as_text=True)
    assert "ONLY-SECOND" in html and "ONLY-FIRST" not in html
    response = post(worker, "/bonds", worker_token, bond_no="FORBIDDEN", bond_amount="10",
                    valid_from="2026-01-01", valid_to="2027-01-01")
    assert "Viewers cannot edit" in response.get_data(as_text=True)
    assert choose(worker, worker_token, 999).status_code == 403
    assert choose(worker, worker_token, 1).status_code == 302
    worker_token = current_token(worker)
    make_bond(worker, worker_token, "OPERATOR-WRITE", 100)


def test_cross_client_foreign_keys_are_rejected(app):
    root = app.test_client()
    token = login(root, "root")
    second = make_client(root, token, "Other")
    with db.SessionLocal() as s:
        s.info["client_id"] = 1
        item = masters.create_item(s, code="PART", description="First client part")
        first_item_id = item.id
        s.commit()
    with db.SessionLocal() as s:
        s.info["client_id"] = second
        assert s.get(Item, first_item_id) is None
        model = masters.create_model(s, code="MODEL", name="Second client model")
        with pytest.raises(ValidationError, match="referenced record"):
            masters.create_bom_version(s, model_id=model.id, effective_from=date(2026, 1, 1),
                                       lines=[dict(item_id=first_item_id, qty_per_unit=1)])
        s.rollback()
    with db.SessionLocal() as s:
        assert s.scalar(select(BOM.id)) is None


def test_client_admin_cannot_access_system_backup_or_change_shared_account(app):
    root = app.test_client()
    token = login(root, "root")
    second = make_client(root, token, "Other")
    post(root, "/users", token, action="create", username="manager", password="password1", role="admin")
    post(root, "/users", token, action="create", username="shared", password="password1", role="viewer")
    choose(root, token, second)
    token = current_token(root)
    post(root, "/users", token, action="assign", username="shared", role="operator")
    with db.SessionLocal() as s:
        shared_id = s.scalar(select(User.id).where(User.username == "shared"))
    manager = app.test_client()
    manager_token = login(manager, "manager")
    assert manager.get("/admin/backup/download?scope=system").status_code == 403
    assert manager.get("/admin/backup/file/eoubond-auto.zip").status_code == 403
    assert manager.get("/admin/audit").status_code == 403
    audit = root.get("/admin/audit").get_data(as_text=True)
    assert "shared" in audit and "password1" not in audit
    response = manager.get("/admin/backup/download?scope=client")
    assert response.status_code == 200 and backup.describe(response.data)["client_id"] == 1
    result = post(manager, "/users", manager_token, action="password", user_id=str(shared_id), password="newpass123")
    assert "Only a super-admin" in result.get_data(as_text=True)
    assert post(manager, "/clients", manager_token, name="Forbidden").status_code == 403


def test_revoking_access_rejects_stale_submissions(app):
    root = app.test_client()
    root_token = login(root, "root")
    second = make_client(root, root_token, "Other")
    post(root, "/users", root_token, action="create", username="worker", password="password1", role="operator")
    choose(root, root_token, second)
    root_token = current_token(root)
    post(root, "/users", root_token, action="assign", username="worker", role="operator")
    worker = app.test_client()
    worker_token = login(worker, "worker")
    choose(worker, worker_token, second)
    worker_token = current_token(worker)
    with db.SessionLocal() as s:
        user = s.scalar(select(User).where(User.username == "worker"))
        user_id = user.id
    post(root, "/users", root_token, action="remove", user_id=str(user_id))
    response = worker.post("/bonds", data={"_csrf": worker_token, "bond_no": "STALE"})
    assert response.status_code == 400
    with db.SessionLocal() as s:
        assert s.scalar(select(Bond.id).where(Bond.bond_no == "STALE")) is None


def test_system_restore_invalidates_other_sessions(app):
    root = app.test_client()
    token = login(root, "root")
    post(root, "/users", token, action="create", username="viewer", password="password1", role="viewer")
    viewer = app.test_client()
    login(viewer, "viewer")
    assert viewer.get("/bonds").status_code == 200
    blob = backup.create_backup()
    response = root.post("/admin/backup/restore", data={"_csrf": token, "scope": "system", "confirm": "RESTORE",
                                                       "backup_file": (io.BytesIO(blob), "system.zip")},
                         content_type="multipart/form-data")
    assert response.status_code == 302
    response = viewer.get("/bonds")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_client_backup_restores_only_that_client(app):
    root = app.test_client()
    token = login(root, "root")
    second = make_client(root, token, "Other")
    make_bond(root, token, "FIRST", 100)
    client_backup = backup.create_backup(client_id=1)
    manifest, data, rules = backup.read_backup(client_backup)
    assert manifest["scope"] == "client" and rules is None
    assert "users" not in data and "clients" not in data and "client_access" not in data
    make_bond(root, token, "AFTER", 100)
    choose(root, token, second)
    token = current_token(root)
    make_bond(root, token, "SECOND", 100)
    with pytest.raises(backup.BackupError, match="selected client"):
        backup.restore_backup(client_backup, client_id=second)
    backup.restore_backup(client_backup, client_id=1)
    with db.SessionLocal() as s:
        assert set(s.scalars(select(Bond.bond_no))) == {"FIRST", "SECOND"}
        assert s.scalar(select(User).where(User.username == "root")).is_superadmin
    with pytest.raises(backup.BackupError, match="entire system"):
        backup.restore_backup(client_backup)
    with db.SessionLocal() as s:
        s.get(Client, 1).backup_id = "different-installation"
        s.commit()
    with pytest.raises(backup.BackupError, match="selected client"):
        backup.restore_backup(client_backup, client_id=1)


def test_client_restore_rejects_cross_client_links_atomically(app):
    root = app.test_client()
    token = login(root, "root")
    second = make_client(root, token, "Other")
    with db.SessionLocal() as s:
        s.info["client_id"] = 1
        first_item = masters.create_item(s, code="PART", description="First")
        first_id = first_item.id
        s.commit()
    with db.SessionLocal() as s:
        s.info["client_id"] = second
        item = masters.create_item(s, code="PART", description="Second")
        second_id = item.id
        model = masters.create_model(s, code="MODEL", name="Model")
        masters.create_bom_version(s, model_id=model.id, effective_from=date(2026, 1, 1),
                                   lines=[dict(item_id=item.id, qty_per_unit=1)])
        s.commit()
    blob = backup.create_backup(client_id=second)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        data = json.loads(archive.read("data.json"))
        manifest = json.loads(archive.read("manifest.json"))
    data["bom_lines"][0]["item_id"] = first_id
    payload = json.dumps(data).encode()
    manifest["sha256"] = hashlib.sha256(payload).hexdigest()
    forged = io.BytesIO()
    with zipfile.ZipFile(forged, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data.json", payload)
    with pytest.raises(backup.BackupError, match="between different clients"):
        backup.restore_backup(forged.getvalue(), client_id=second)
    with db.SessionLocal() as s:
        s.info["client_id"] = second
        assert s.scalar(select(BOM)).lines[0].item_id == second_id


def test_old_backup_restores_as_default_client_and_promotes_admin(app):
    with db.SessionLocal() as s:
        s.add(Bond(bond_no="LEGACY", bond_amount=100, balance=100, status="ACTIVE",
                   valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1)))
        s.commit()
    old = backup.create_backup()
    archive = zipfile.ZipFile(io.BytesIO(old))
    data = json.loads(archive.read("data.json"))
    data.pop("clients")
    data.pop("client_access")
    for row in data["users"]:
        row.pop("is_superadmin", None)
    for rows in data.values():
        for row in rows:
            row.pop("client_id", None)
    payload = json.dumps(data).encode()
    manifest = json.loads(archive.read("manifest.json"))
    manifest["sha256"] = hashlib.sha256(payload).hexdigest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("manifest.json", json.dumps(manifest))
        out.writestr("data.json", payload)
    backup.restore_backup(buf.getvalue())
    with db.SessionLocal() as s:
        assert s.scalar(select(Bond).where(Bond.bond_no == "LEGACY")).client_id == 1
        user = s.scalar(select(User).where(User.username == "root"))
        assert user.is_superadmin
        assert s.get(ClientAccess, (user.id, 1)).role == "admin"


def test_upgrade_assigns_existing_data_and_users(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from eoubond import config

    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "migrations"))
    command.upgrade(cfg, "0004")
    engine = db.make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, username, password_hash, role, active, created_at) "
                          "VALUES (1, 'old-admin', 'hash', 'admin', true, CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO bonds (id, bond_no, bond_amount, balance, valid_from, valid_to, "
                          "status, created_at) VALUES (1, 'OLD', 100, 100, '2026-01-01', "
                          "'2027-01-01', 'ACTIVE', CURRENT_TIMESTAMP)"))
    command.upgrade(cfg, "head")
    with db.SessionLocal(bind=engine) as s:
        assert s.get(Bond, 1).client_id == 1
        assert s.get(User, 1).is_superadmin
        assert s.get(ClientAccess, (1, 1)).role == "admin"
    engine.dispose()
