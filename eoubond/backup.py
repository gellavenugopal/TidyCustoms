"""Full backup and restore.

A backup is one .zip file holding:
  manifest.json   app version, schema (Alembic) revision, time, row counts, SHA-256 of data.json
  data.json       every row of every table (users, masters, bonds, ledger, stock,
                  transactions, audit trail), database-independent JSON
  rules.toml      the business rules / rates file in use

It is database-neutral: a backup taken from SQLite on one PC restores into
PostgreSQL in the cloud and vice versa. Restore replaces ALL data in one
transaction - if anything fails, nothing changes. Before restoring, the current
data is saved as a safety backup.

Backups from an older version of the app restore into a newer one (columns
added later take their defaults). A backup from a newer version is refused.
"""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Date, DateTime, Numeric, delete, func, insert, inspect, select, text

from . import __version__, config, db
from .db import Base

FORMAT = "eoubond-backup/1"


class BackupError(Exception):
    pass


def _tables():
    from . import models  # noqa: F401  (register tables)

    return list(Base.metadata.sorted_tables)  # parents before children


def _enc(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _dec(col, v):
    if v is None:
        return None
    if isinstance(col.type, DateTime):
        return datetime.fromisoformat(v)
    if isinstance(col.type, Date):
        return date.fromisoformat(v)
    if isinstance(col.type, Numeric):
        return Decimal(v)
    return v


def schema_revision(conn) -> str | None:
    if not inspect(conn).has_table("alembic_version"):
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


# ------------------------------------------------------------------ create
def create_backup(engine=None, *, client_id: int | None = None) -> bytes:
    engine = engine or db.engine
    data, counts = {}, {}
    # one read transaction = a consistent snapshot, without blocking other users
    with db.read_only(engine).connect() as conn, conn.begin():
        rev = schema_revision(conn)
        if client_id is not None:
            from .models import Client
            client_key = conn.execute(select(Client.backup_id).where(Client.id == client_id)).scalar_one_or_none()
            if not client_key:
                raise BackupError("Client not found")
        for t in _tables():
            if client_id is not None and "client_id" not in t.c:
                continue
            if client_id is not None and t.name == "client_access":
                continue
            query = select(t).order_by(*t.primary_key.columns)
            if client_id is not None:
                query = query.where(t.c.client_id == client_id)
            rows = [{k: _enc(v) for k, v in r._mapping.items()}
                    for r in conn.execute(query)]
            data[t.name] = rows
            counts[t.name] = len(rows)
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    manifest = dict(format=FORMAT, app_version=__version__, schema_revision=rev,
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    database=engine.dialect.name, row_counts=counts,
                    sha256=hashlib.sha256(payload).hexdigest())
    if client_id is not None:
        manifest.update(scope="client", client_id=client_id, client_key=client_key)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("data.json", payload)
        rules = Path(config.rules_file())
        if client_id is None and rules.exists():
            z.write(rules, "rules.toml")
    return buf.getvalue()


def write_backup(directory: Path | str | None = None, kind: str = "manual", engine=None) -> Path:
    directory = Path(directory or config.backup_dir())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"eoubond-{kind}-{datetime.now():%Y%m%d-%H%M%S}.zip"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(create_backup(engine))
    tmp.replace(path)  # never leave a half-written backup behind
    return path


def list_backups(directory: Path | str | None = None) -> list[Path]:
    d = Path(directory or config.backup_dir())
    return sorted(d.glob("eoubond-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []


def prune(directory: Path | str | None = None, keep: int | None = None, kind: str = "auto") -> None:
    keep = keep or config.backup_keep()
    autos = [p for p in list_backups(directory) if p.name.startswith(f"eoubond-{kind}-")]
    for p in autos[keep:]:
        p.unlink(missing_ok=True)


# ------------------------------------------------------------------ read / check
def read_backup(blob: bytes) -> tuple[dict, dict, bytes | None]:
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        manifest = json.loads(z.read("manifest.json"))
        payload = z.read("data.json")
        rules = z.read("rules.toml") if "rules.toml" in z.namelist() else None
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError("This is not an EOU Bond Tracker backup file") from exc
    if manifest.get("format") != FORMAT:
        raise BackupError("Unknown backup format")
    if hashlib.sha256(payload).hexdigest() != manifest.get("sha256"):
        raise BackupError("Backup file is damaged (checksum mismatch) - use another backup")
    return manifest, json.loads(payload), rules


def describe(blob: bytes) -> dict:
    manifest, _data, _rules = read_backup(blob)
    return manifest


# ------------------------------------------------------------------ restore
def restore_backup(blob: bytes, *, restore_rules: bool = True, engine=None, safety_copy: bool = True,
                   restored_by: str = "system", client_id: int | None = None) -> dict:
    """Replace all data with the backup's. Atomic. Returns a summary."""
    engine = engine or db.engine
    manifest, data, rules = read_backup(blob)
    tables = _tables()
    known = {t.name: t for t in tables}
    if client_id is None and manifest.get("scope") == "client":
        raise BackupError("A client backup cannot replace the entire system")
    if client_id is not None:
        from .models import Client
        with db.read_only(engine).connect() as conn:
            client_key = conn.execute(select(Client.backup_id).where(Client.id == client_id)).scalar_one_or_none()
        if (not client_key or manifest.get("scope") != "client" or manifest.get("client_id") != client_id
                or manifest.get("client_key") != client_key):
            raise BackupError("This backup is not for the selected client")
        tables = [t for t in tables if "client_id" in t.c and t.name != "client_access"]
        expected = {t.name for t in tables}
        if set(data) != expected or any(row.get("client_id") != client_id
                                        for rows in data.values() for row in rows):
            raise BackupError("Client backup contains invalid or incomplete data")
        restore_rules = False
    elif "clients" not in data:
        data["clients"] = [dict(id=1, name="Existing data", active=True, backup_id=secrets.token_urlsafe(32),
                                created_at=datetime.now(timezone.utc).isoformat())]
        for row in data.get("users", []):
            row["is_superadmin"] = row.get("role") == "admin"
        data["client_access"] = [dict(user_id=row["id"], client_id=1, role=row["role"])
                                 for row in data.get("users", [])]
        for table in tables:
            if "client_id" in table.c and table.name != "client_access":
                for row in data.get(table.name, []):
                    row["client_id"] = 1
    unknown = set(data) - set(known)
    if unknown:
        raise BackupError(f"Backup is from a newer version of the app (tables {', '.join(sorted(unknown))}); "
                          "update the app first")
    for name, rows in data.items():
        if rows:
            extra = set(rows[0]) - set(known[name].columns.keys())
            if extra:
                raise BackupError(f"Backup is from a newer version of the app (columns {name}."
                                  f"{', '.join(sorted(extra))}); update the app first")

    safety = None
    if safety_copy:
        try:
            if client_id is None:
                safety = write_backup(kind="before-restore", engine=engine)
            else:
                directory = config.backup_dir()
                directory.mkdir(parents=True, exist_ok=True)
                safety = directory / f"eoubond-client-{client_id}-before-restore-{datetime.now():%Y%m%d-%H%M%S}.zip"
                safety.write_bytes(create_backup(engine, client_id=client_id))
        except OSError as exc:
            raise BackupError("Could not save the safety backup; restore was not started") from exc

    with engine.begin() as conn:
        for t in reversed(tables):
            statement = delete(t)
            if client_id is not None:
                statement = statement.where(t.c.client_id == client_id)
            conn.execute(statement)
        for t in tables:
            rows = data.get(t.name) or []
            if not rows:
                continue
            cols = {c.name: c for c in t.columns}
            conv = [{k: _dec(cols[k], v) for k, v in r.items()} for r in rows]
            for i in range(0, len(conv), 500):
                conn.execute(insert(t), conv[i:i + 500])
        if client_id is None:
            from .models import SystemState
            state = SystemState.__table__
            if conn.execute(select(state.c.id).where(state.c.id == 1)).first():
                conn.execute(state.update().where(state.c.id == 1).values(session_epoch=secrets.token_urlsafe(32)))
            else:
                conn.execute(insert(state).values(id=1, session_epoch=secrets.token_urlsafe(32)))
        for t in tables:
            if "client_id" not in t.c or t.name == "client_access":
                continue
            for fk in t.foreign_keys:
                target = fk.column.table
                if target.name == "clients" or "client_id" not in target.c:
                    continue
                invalid = select(func.count()).select_from(t.join(target, fk.parent == fk.column)).where(
                    t.c.client_id != target.c.client_id)
                if conn.execute(invalid).scalar():
                    raise BackupError("Backup contains references between different clients")
        if conn.dialect.name == "postgresql":
            for t in tables:
                if "id" in t.columns:
                    conn.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {t.name}), 1), "
                        f"(SELECT MAX(id) FROM {t.name}) IS NOT NULL)"))
        from .models import AuditLog
        conn.execute(insert(AuditLog), [dict(
            ts=datetime.now(timezone.utc).replace(tzinfo=None), username=restored_by, action="RESTORE",
            table_name="*", record_id=None, old_values=None,
            client_id=client_id or 1,
            new_values=json.dumps(dict(backup_created_at=manifest.get("created_at"),
                                       app_version=manifest.get("app_version"),
                                       row_counts=manifest.get("row_counts"))))])

    rules_restored = False
    if restore_rules and rules is not None:
        target = Path(config.rules_file())
        if not target.exists() or target.read_bytes() != rules:
            if target.exists():
                target.with_suffix(".toml.bak").write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rules)
            rules_restored = True
        config.set_rules(config.load_rules())
    return dict(manifest=manifest, safety_backup=str(safety) if safety else None, rules_restored=rules_restored,
                rows=sum((manifest.get("row_counts") or {}).values()))


# ------------------------------------------------------------------ automatic
def newest_auto_age(directory=None) -> timedelta | None:
    autos = [p for p in list_backups(directory) if p.name.startswith("eoubond-auto-")]
    if not autos:
        return None
    return datetime.now() - datetime.fromtimestamp(autos[0].stat().st_mtime)


def auto_backup_once(directory=None, engine=None) -> Path | None:
    """Write an automatic backup if the newest one is older than the interval."""
    age = newest_auto_age(directory)
    if age is not None and age < timedelta(hours=config.backup_interval_hours()):
        return None
    path = write_backup(directory, kind="auto", engine=engine)
    prune(directory)
    return path


def start_auto_backup_thread(log=print) -> threading.Thread:
    def loop():
        while True:
            try:
                p = auto_backup_once()
                if p:
                    log(f"[backup] automatic backup written: {p}")
            except Exception as exc:  # noqa: BLE001 - keep the server running
                log(f"[backup] automatic backup FAILED: {exc}")
            time.sleep(600)

    t = threading.Thread(target=loop, name="auto-backup", daemon=True)
    t.start()
    return t


def has_users(engine=None) -> bool:
    from .models import User

    with db.read_only(engine).connect() as conn:
        return bool(conn.execute(select(func.count()).select_from(User)).scalar())
