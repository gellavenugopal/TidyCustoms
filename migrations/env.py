from logging.config import fileConfig

from alembic import context

from eoubond import config as app_config
from eoubond import models  # noqa: F401  (register tables)
from eoubond.db import Base, make_engine

cfg = context.config
if cfg.config_file_name is not None:
    fileConfig(cfg.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=app_config.database_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # SQLite: foreign keys off while tables are rebuilt (batch mode), then a full
    # foreign-key check inside the same transaction - any broken link aborts the upgrade.
    engine = make_engine(app_config.database_url(), foreign_keys=False)
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=sqlite, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                bad = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise RuntimeError(f"Migration would break {len(bad)} foreign-key link(s), e.g. {bad[:3]} "
                                       "- nothing was changed")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
