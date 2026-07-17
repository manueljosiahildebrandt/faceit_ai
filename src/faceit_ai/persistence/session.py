"""Engine and session factory with per-process caching and backend tuning."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from faceit_ai.persistence.models import Base
from faceit_ai.persistence.schema_upgrade import upgrade_schema

# One engine per URL per process. Opening a new engine (and pool) on every UI action
# is wasteful and, for Postgres, needlessly churns connections.
_ENGINE_CACHE: dict[str, tuple[Engine, sessionmaker[Session]]] = {}


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite:")


def _is_memory_sqlite(url: str) -> bool:
    return _is_sqlite(url) and (":memory:" in url or url in ("sqlite://",))


def _build_engine(database_url: str) -> Engine:
    if _is_sqlite(database_url):
        # check_same_thread=False: the web UI touches the engine from worker threads.
        engine = create_engine(
            database_url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            # WAL greatly improves concurrent reader/writer behavior for a local file.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            # Wait instead of instantly failing when another writer holds the lock.
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine

    # Server backends (PostgreSQL, MariaDB/MySQL): pool with liveness checks.
    return create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
    )


def init_db(database_url: str) -> Engine:
    """Create all tables for the configured database (idempotent)."""
    engine = _build_engine(database_url)
    Base.metadata.create_all(engine, checkfirst=True)
    upgrade_schema(engine)
    return engine


def create_engine_and_session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    """Return a cached (engine, session factory) for this URL.

    Schema creation runs once per engine (when first built), not on every call, so it
    stays out of the hot path while still working for local SQLite and first-run setups.
    In-memory SQLite is never cached (each engine is an isolated database).
    """
    cached = _ENGINE_CACHE.get(database_url)
    if cached is not None:
        return cached

    engine = _build_engine(database_url)
    Base.metadata.create_all(engine, checkfirst=True)
    upgrade_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False, future=True)

    if not _is_memory_sqlite(database_url):
        _ENGINE_CACHE[database_url] = (engine, factory)
    return engine, factory


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


T = TypeVar("T")


def run_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.2,
) -> T:
    """Run a DB operation, retrying briefly on transient lock/serialization errors.

    Useful when several PCs write to the same shared database concurrently.
    """
    log = logging.getLogger("faceit_ai")
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except OperationalError as exc:  # locked / serialization / deadlock
            last_exc = exc
            if i == attempts - 1:
                break
            delay = base_delay * (2**i)
            log.debug("DB conflict (attempt %d/%d), retrying in %.2fs", i + 1, attempts, delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
