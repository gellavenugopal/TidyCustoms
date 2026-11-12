"""Engine / session setup.

SQLite: foreign keys are switched on and every transaction starts with
BEGIN IMMEDIATE, so two writers can never interleave a FIFO allocation.
PostgreSQL: services lock bond and lot rows with SELECT ... FOR UPDATE.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria
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
        with engine.begin() as conn:
            if not conn.execute(select(models.Client.id).where(models.Client.id == 1)).first():
                conn.execute(models.Client.__table__.insert().values(id=1, name="Existing data"))
                if conn.dialect.name == "postgresql":
                    conn.execute(text("SELECT setval(pg_get_serial_sequence('clients', 'id'), 1, true)"))
            if not conn.execute(select(models.SystemState.id).where(models.SystemState.id == 1)).first():
                conn.execute(models.SystemState.__table__.insert().values(id=1))
    return engine


@event.listens_for(Session, "do_orm_execute")
def _scope_client_queries(state):
    client_id = state.session.info.get("client_id")
    if client_id is None:
        return
    if not state.is_select:
        raise ValueError("Bulk database changes are not allowed in a client session")
    from .models import ClientScoped

    state.statement = state.statement.options(
        with_loader_criteria(ClientScoped, lambda cls: cls.client_id == client_id, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _scope_client_writes(session, _ctx, _instances):
    client_id = session.info.get("client_id")
    if client_id is None:
        return
    from .models import ClientScoped
    from .services.errors import ValidationError

    pending = []
    for obj in session.new.union(session.dirty).union(session.deleted):
        if not isinstance(obj, ClientScoped):
            continue
        if obj in session.new and obj.client_id is None:
            obj.client_id = client_id
        if obj.client_id != client_id:
            raise ValidationError("The record does not belong to the selected client")
        if obj not in session.deleted:
            pending.append(obj)
    session.info["_client_pending"] = pending


@event.listens_for(Session, "after_flush_postexec")
def _check_client_references(session, _ctx):
    client_id = session.info.get("client_id")
    if client_id is None:
        return
    from .services.errors import ValidationError

    conn = session.connection()
    for obj in session.info.pop("_client_pending", []):
        for column in inspect(obj).mapper.local_table.columns:
            value = getattr(obj, column.key)
            if value is None:
                continue
            for fk in column.foreign_keys:
                target = fk.column.table
                if target.name == "clients" or "client_id" not in target.c:
                    continue
                owner = conn.execute(select(target.c.client_id).where(fk.column == value)).scalar_one_or_none()
                if owner != client_id:
                    raise ValidationError("A referenced record does not belong to the selected client")
