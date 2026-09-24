"""Shared fixtures: a PostgreSQL session rolled back after each test (compose Postgres)."""
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import get_engine


@pytest.fixture
def pg_connection() -> Iterator[Connection]:
    """Yield a connection to the compose PostgreSQL, or skip the test if it is unreachable."""
    try:
        connection = get_engine().connect()
        connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable: {exc.orig}")
    connection.rollback()                # end the probe's implicit transaction
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def db_session(pg_connection: Connection) -> Iterator[Session]:
    """Yield a session inside an outer transaction that is rolled back, leaving no rows behind."""
    transaction = pg_connection.begin()
    session = Session(bind=pg_connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
