"""Audit trail: every ORM create / update / delete writes a row to audit_log with
who, what, when, and the old and new column values (as JSON).

It hooks SQLAlchemy's after_flush event, at which point new rows have primary
keys but attribute history (old values) is still available. The audit row is
inserted on the same connection, so it commits or rolls back with the change.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import event, inspect, insert
from sqlalchemy.orm import Session

from .audit_context import current_username
from .models import AuditLog, _now

MASKED_COLUMNS = {"password_hash"}
SKIP_TABLES = {AuditLog.__tablename__}


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _column_values(obj, which: str) -> dict:
    """which = 'current' | 'old'."""
    state = inspect(obj)
    out = {}
    for attr in state.mapper.column_attrs:
        key = attr.key
        hist = state.attrs[key].history
        if which == "old":
            if hist.deleted:
                val = hist.deleted[0]
            elif hist.unchanged:
                val = hist.unchanged[0]
            else:
                val = None
        else:
            val = getattr(obj, key, None)
        out[key] = "***" if key in MASKED_COLUMNS else _jsonable(val)
    return out


def _changes(obj) -> tuple[dict, dict]:
    state = inspect(obj)
    old, new = {}, {}
    for attr in state.mapper.column_attrs:
        hist = state.attrs[attr.key].history
        if not hist.has_changes():
            continue
        o = hist.deleted[0] if hist.deleted else None
        n = hist.added[0] if hist.added else None
        if _jsonable(o) == _jsonable(n):
            continue
        masked = attr.key in MASKED_COLUMNS
        old[attr.key] = "***" if masked else _jsonable(o)
        new[attr.key] = "***" if masked else _jsonable(n)
    return old, new


def _pk(obj) -> str:
    ident = inspect(obj).identity
    if ident:
        return ",".join(str(i) for i in ident)
    return str(getattr(obj, "id", ""))


@event.listens_for(Session, "after_flush")
def _audit_after_flush(session: Session, _ctx):
    rows = []
    user = current_username()
    ts = _now()
    for obj in session.new:
        table = getattr(obj, "__tablename__", None)
        if not table or table in SKIP_TABLES:
            continue
        rows.append(dict(ts=ts, username=user, action="CREATE", table_name=table,
                         record_id=_pk(obj), old_values=None,
                         new_values=json.dumps(_column_values(obj, "current"))))
    for obj in session.dirty:
        table = getattr(obj, "__tablename__", None)
        if not table or table in SKIP_TABLES or not session.is_modified(obj, include_collections=False):
            continue
        old, new = _changes(obj)
        if not new and not old:
            continue
        rows.append(dict(ts=ts, username=user, action="UPDATE", table_name=table,
                         record_id=_pk(obj), old_values=json.dumps(old), new_values=json.dumps(new)))
    for obj in session.deleted:
        table = getattr(obj, "__tablename__", None)
        if not table or table in SKIP_TABLES:
            continue
        rows.append(dict(ts=ts, username=user, action="DELETE", table_name=table,
                         record_id=_pk(obj), old_values=json.dumps(_column_values(obj, "old")),
                         new_values=None))
    if rows:
        session.connection().execute(insert(AuditLog), rows)
