"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from tg import models  # noqa: F401  — import registers tables on SQLModel.metadata

_engine: Engine | None = None


def _prepare_sqlite_path(database_url: str) -> None:
    """SQLite will not create missing parent directories on its own."""
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        path = Path(database_url[len(prefix) :])
        if path.name and str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)


def init_engine(database_url: str, echo: bool = False) -> Engine:
    """Create (once) and return the process-wide engine."""
    global _engine
    if _engine is not None:
        return _engine

    _prepare_sqlite_path(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, echo=echo, connect_args=connect_args)

    if database_url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            # WAL lets the web UI read while the poller writes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("engine not initialised — call init_engine() first")
    return _engine


def create_all(database_url: str | None = None) -> None:
    engine = init_engine(database_url) if database_url else get_engine()
    SQLModel.metadata.create_all(engine)


def reset_engine() -> None:
    """Drop the cached engine. Used by tests."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error.

    ``expire_on_commit=False`` matters here: callers routinely keep working with
    objects (alerts, screenings) after the scope closes, and the default would turn
    every such access into a query against a closed session.
    """
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
