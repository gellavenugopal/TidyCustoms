"""Backup / restore: full round trip after total data loss, integrity checks,
older-backup compatibility, safety copy, automatic backups, and the web flows
(admin restore, first-run restore on a fresh install)."""
import io
import json
import re
import zipfile
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from eoubond import backup as bk
from eoubond import db
from eoubond.models import AuditLog, Bond, BondLedgerEntry, ImportLot, User
from eoubond.services import dta, exports, production
from tests.conftest import D, World


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    rules = tmp_path / "rules.toml"
    rules.write_text("[duty]\ndefault_bcd_rate = 10.0\n")
    monkeypatch.setenv("EOU_RULES_FILE", str(rules))
    engine = db.init_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}", create_all=True)
    yield engine, tmp_path
    engine.dispose()


def _populate(engine):
    with Session(bind=engine) as s:
        w = World(s)
        s.add(User(username="boss", password_hash=generate_password_hash("bosspass1"), role="admin",
                   is_superadmin=True))
        w.import_("INV-1", date(2026, 2, 1), [w.line(w.tft, 10, 200000, 50000), w.line(w.csi, 10, 1, 1),
                                              w.line(w.pcb, 20, 1, 1), w.line(w.bat, 10, 1, 1)])
        production.produce(s, batch_no="P1", model_id=w.m4343.id, qty_units=4, production_date=date(2026, 3, 1))
        exports.post_export(s, invoice_no="EX-1", export_date=date(2026, 3, 2),
                            lines=[dict(line_type="FG", model_id=w.m4343.id, qty=2)])
        dta.post_dta(s, clearance_no="D-1", clearance_date=date(2026, 3, 3),
                     lines=[dict(line_type="FG", model_id=w.m4343.id, qty=1)])
        s.commit()


def _snapshot(engine):
    with Session(bind=engine) as s:
        return dict(
            balances={b.bond_no: b.balance for b in s.scalars(select(Bond))},
            ledger=[(e.entry_type, e.debit, e.credit, e.balance_after) for e in s.scalars(
                select(BondLedgerEntry).order_by(BondLedgerEntry.id))],
            lots=[(l.id, l.qty_available, l.dsv_available, l.lot_date) for l in s.scalars(
                select(ImportLot).order_by(ImportLot.id))],
            users=[(u.username, u.password_hash, u.role) for u in s.scalars(select(User))],
            audit=s.scalar(select(func.count(AuditLog.id))))


def test_round_trip_after_total_loss(env):
    engine, tmp = env
    _populate(engine)
    before = _snapshot(engine)
    blob = bk.create_backup(engine)

    # simulate a formatted PC: a brand-new empty database
    engine.dispose()
    fresh = db.init_engine(f"sqlite:///{(tmp / 'new.db').as_posix()}", create_all=True)
    res = bk.restore_backup(blob, engine=fresh, restored_by="t")
    after = _snapshot(fresh)
    for k in ("balances", "ledger", "lots", "users"):
        assert after[k] == before[k], k
    assert after["audit"] == before["audit"] + 1  # + the RESTORE entry
    assert res["rows"] > 50
    # sequences continue after the restored ids: new rows still insert fine
    with Session(bind=fresh) as s:
        s.add(User(username="new", password_hash="x", role="viewer"))
        s.commit()
    fresh.dispose()


def test_restore_replaces_and_keeps_safety_copy(env):
    engine, tmp = env
    _populate(engine)
    blob = bk.create_backup(engine)
    with Session(bind=engine) as s:  # changes made after the backup
        s.get(Bond, 1).remarks = "changed later"
        s.commit()
    res = bk.restore_backup(blob, engine=engine)
    with Session(bind=engine) as s:
        assert s.get(Bond, 1).remarks is None
    safety = res["safety_backup"]
    assert safety and "before-restore" in safety
    bk.restore_backup(open(safety, "rb").read(), engine=engine)  # undo the restore
    with Session(bind=engine) as s:
        assert s.get(Bond, 1).remarks == "changed later"


def test_damaged_or_foreign_files_are_refused_without_touching_data(env):
    engine, _ = env
    _populate(engine)
    before = _snapshot(engine)
    blob = bk.create_backup(engine)
    z = zipfile.ZipFile(io.BytesIO(blob))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("manifest.json", z.read("manifest.json"))
        out.writestr("data.json", z.read("data.json").replace(b'"B-2026-01"', b'"HACKED"'))
    with pytest.raises(bk.BackupError, match="damaged"):
        bk.restore_backup(buf.getvalue(), engine=engine)
    with pytest.raises(bk.BackupError, match="not an EOU"):
        bk.restore_backup(b"hello", engine=engine)
    assert _snapshot(engine) == before


def _rewrite(blob, fn):
    z = zipfile.ZipFile(io.BytesIO(blob))
    data = json.loads(z.read("data.json"))
    fn(data)
    payload = json.dumps(data).encode()
    m = json.loads(z.read("manifest.json"))
    import hashlib
    m["sha256"] = hashlib.sha256(payload).hexdigest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("manifest.json", json.dumps(m))
        out.writestr("data.json", payload)
    return buf.getvalue()


def test_older_backup_without_new_columns_restores(env):
    engine, _ = env
    _populate(engine)

    def drop_cols(data):  # pretend the backup predates the Form III columns
        for r in data["import_lines"]:
            for k in ("bcd_forgone", "sws_forgone", "igst_forgone", "cess_forgone", "other_forgone",
                      "be_item_serial", "qty_received"):
                r.pop(k)
    bk.restore_backup(_rewrite(bk.create_backup(engine), drop_cols), engine=engine)
    with engine.connect() as c:
        assert c.exec_driver_sql("select count(*) from import_lines where bcd_forgone = 0").scalar() == 4


def test_newer_backup_is_refused(env):
    engine, _ = env
    _populate(engine)
    blob = _rewrite(bk.create_backup(engine), lambda d: d["bonds"][0].update(future_col=1))
    with pytest.raises(bk.BackupError, match="newer version"):
        bk.restore_backup(blob, engine=engine)


def test_rules_file_restored(env):
    engine, tmp = env
    _populate(engine)
    blob = bk.create_backup(engine)
    (tmp / "rules.toml").write_text("[duty]\ndefault_bcd_rate = 99.0\n")
    res = bk.restore_backup(blob, engine=engine)
    assert res["rules_restored"] and "10.0" in (tmp / "rules.toml").read_text()
    assert (tmp / "rules.toml.bak").exists()


def test_auto_backup_interval_and_pruning(env, monkeypatch):
    engine, tmp = env
    _populate(engine)
    monkeypatch.setenv("BACKUP_KEEP", "2")
    assert bk.auto_backup_once() is not None
    assert bk.auto_backup_once() is None  # newest is younger than 24 h
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "0")
    import time
    for _ in range(3):
        time.sleep(1.1)
        bk.auto_backup_once()
    assert len([p for p in bk.list_backups() if "auto" in p.name]) == 2


# ------------------------------------------------------------------ web
def _tok(c, url="/login"):
    return re.search(r'name="_csrf" value="([^"]+)"', c.get(url, follow_redirects=True).get_data(as_text=True)).group(1)


def test_first_run_restore_and_admin_restore(tmp_path, monkeypatch):
    from eoubond.web import create_app

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("EOU_RULES_FILE", str(tmp_path / "rules.toml"))
    # PC 1 with data
    create_app(f"sqlite:///{(tmp_path / 'pc1.db').as_posix()}", create_all=True)
    _populate(db.engine)
    blob = bk.create_backup()
    db.engine.dispose()

    # PC 2 freshly installed: /login sends to setup, which restores without any login
    app = create_app(f"sqlite:///{(tmp_path / 'pc2.db').as_posix()}", create_all=True)
    c = app.test_client()
    r = c.get("/login")
    assert r.status_code == 302 and "/setup" in r.headers["Location"]
    tok = _tok(c, "/setup")
    r = c.post("/setup", data={"_csrf": tok, "action": "restore", "restore_rules": "on",
                               "backup_file": (io.BytesIO(blob), "b.zip")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "Restored" in r.get_data(as_text=True)
    assert c.get("/setup").status_code == 302  # setup is closed once users exist
    # sign in with the restored user and see restored data
    tok = _tok(c)
    c.post("/login", data=dict(username="boss", password="bosspass1", _csrf=tok))
    assert "B-2026-01" in c.get("/bonds").get_data(as_text=True)

    # admin: backup download, then restore needs the RESTORE confirmation
    d = c.get("/admin/backup/download?scope=system")
    assert d.status_code == 200 and bk.describe(d.data)["format"] == bk.FORMAT
    assert "eoubond-manual-" in c.get("/admin/backup").get_data(as_text=True)
    tok = _tok(c, "/admin/backup")
    r = c.post("/admin/backup/restore", data={"_csrf": tok, "confirm": "no", "backup_file": (io.BytesIO(blob), "b.zip")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "Type RESTORE" in r.get_data(as_text=True)
    r = c.post("/admin/backup/restore", data={"_csrf": tok, "confirm": "RESTORE", "scope": "system",
                                              "backup_file": (io.BytesIO(blob), "b.zip")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "Restored" in r.get_data(as_text=True) and "Sign in" in r.get_data(as_text=True)
    db.engine.dispose()


def test_non_admin_cannot_backup(tmp_path, monkeypatch):
    from eoubond.web import create_app

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    app = create_app(f"sqlite:///{(tmp_path / 'x.db').as_posix()}", create_all=True)
    with db.SessionLocal() as s:
        s.add(User(username="op", password_hash=generate_password_hash("oppass123"), role="operator"))
        s.commit()
    c = app.test_client()
    tok = _tok(c)
    c.post("/login", data=dict(username="op", password="oppass123", _csrf=tok))
    r = c.get("/admin/backup/download")
    assert r.status_code == 302 and r.mimetype != "application/zip"
    db.engine.dispose()
