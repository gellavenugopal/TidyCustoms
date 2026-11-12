"""Engine / session setup.

SQLite: foreign keys are switched on and every transaction starts with
BEGIN IMMEDIATE, so two writers can never interleave a FIFO allocation.
PostgreSQL: services lock bond and lot rows with SELECT ... FOR UPDATE.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from . import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None, *, foreign_keys: bool = True):
    """foreign_keys=False is for migrations only: SQLite rebuilds a table to alter a
    column, which it can't do while other tables' foreign keys point at it. The
    migration then runs PRAGMA foreign_key_check before committing."""
    url = url or config.database_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None  # let SQLAlchemy emit BEGIN itself
            cur = dbapi_conn.cursor()
            cur.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            # Writers take the write lock up front (no lock-upgrade deadlocks during FIFO
            # allocation). Read-only work opts out with execution_options(sqlite_begin="DEFERRED").
            mode = conn.get_execution_options().get("sqlite_begin", "IMMEDIATE")
            conn.exec_driver_sql(f"BEGIN {mode}")

    return engine


engine = None
SessionLocal: sessionmaker[Session] = sessionmaker(expire_on_commit=False)


def read_only(eng=None):
    """Engine view whose transactions don't take SQLite's write lock (no-op elsewhere)."""
    return (eng or engine).execution_options(sqlite_begin="DEFERRED")


def init_engine(url: str | None = None, create_all: bool = False):
    """Bind the global session factory to an engine. Returns the engine."""
    global engine
    from . import audit  # noqa: F401  (register the audit-trail listener)

    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
    if create_all:
        from . import models  # noqa: F401  (register tables)

        Base.metadata.create_all(engine)
    return engine
