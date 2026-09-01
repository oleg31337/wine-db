"""Database engine/session management."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _create_engine(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _create_engine(get_settings().database_url)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    # Add columns introduced after first deploy without a full migration tool.
    # create_all() won't alter existing tables, so backfill any missing columns
    # here. Idempotent and safe across SQLite + Postgres.
    _ensure_columns(
        engine,
        "users",
        {"is_admin": "BOOLEAN NOT NULL DEFAULT FALSE"},
    )
    _ensure_columns(
        engine,
        "wines",
        {"back_label_text": "TEXT"},
    )
    # Drop columns removed after first deploy without a full migration tool.
    # create_all() won't touch existing tables, so prune here. Idempotent.
    _drop_columns(engine, "wines", {"barcode", "notes_source"})


def _ensure_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns(table)}
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _drop_columns(engine: Engine, table: str, columns: set[str]) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns(table)}
    with engine.begin() as conn:
        for name in columns:
            if name in existing:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))


def reset_engine() -> None:
    """Used by tests to rebind after changing settings."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
