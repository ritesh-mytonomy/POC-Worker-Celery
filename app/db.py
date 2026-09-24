"""Database engine and sessions. Only the API touches PostgreSQL (design.md §1)."""
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide engine, created on first use from DATABASE_URL."""
    return create_engine(get_settings().DATABASE_URL, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    """Return the session factory bound to the engine."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db_session() -> Iterator[Session]:
    """Yield a session for one request and close it afterwards; callers commit explicitly."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
