"""Shared fixtures for compose PostgreSQL: rolled-back sessions and committed rows.

Database tests skip when PostgreSQL is unreachable, unless REQUIRE_DB=1, in which case they fail.
"""
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import get_engine


def connect_or_skip() -> Connection:
    """Open a connection to PostgreSQL; skip the test if unreachable, or fail when REQUIRE_DB=1."""
    try:
        connection = get_engine().connect()
        connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        message = f"PostgreSQL not reachable: {exc.orig}"
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"{message} (REQUIRE_DB=1)")
        pytest.skip(message)
    connection.rollback()                # end the probe's implicit transaction
    return connection


@pytest.fixture
def pg_connection() -> Iterator[Connection]:
    """Yield a connection to the compose PostgreSQL."""
    connection = connect_or_skip()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def db_session(pg_connection: Connection) -> Iterator[Session]:
    """Yield a session inside an outer transaction that is rolled back, leaving no rows behind.

    session.commit() inside the test only releases a savepoint; the outer transaction still rolls back.
    """
    transaction = pg_connection.begin()
    session = Session(bind=pg_connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()


@contextmanager
def _committed_file(status: str) -> Iterator[uuid.UUID]:
    """Commit a batch with one file in `status`, visible to every connection; delete it afterwards."""
    connect_or_skip().close()
    engine = get_engine()
    with engine.begin() as conn:
        batch_id = conn.execute(
            text("INSERT INTO upload_batch (organization_id) VALUES (:org) RETURNING batch_id"),
            {"org": uuid.uuid4()},
        ).scalar_one()
        file_id = conn.execute(
            text("""INSERT INTO upload_file (batch_id, organization_id, file_name, file_ext, is_archive,
                                             s3_key, status, uploaded_at)
                    SELECT batch_id, organization_id, 'race.docx', 'docx', false,
                           'ClinSync/incoming/race.docx', :status,
                           CASE WHEN :confirmed THEN now() END
                    FROM upload_batch WHERE batch_id = :b RETURNING file_id"""),
            {"b": batch_id, "status": status, "confirmed": status != "uploading"},
        ).scalar_one()
    try:
        yield file_id
    finally:
        with engine.begin() as conn:     # ON DELETE CASCADE removes the file and any candidates
            conn.execute(text("DELETE FROM upload_batch WHERE batch_id = :b"), {"b": batch_id})


@pytest.fixture
def committed_file() -> Iterator[uuid.UUID]:
    """A committed `uploaded` file (confirmed, not yet claimed)."""
    with _committed_file("uploaded") as file_id:
        yield file_id


@pytest.fixture
def committed_uploading_file() -> Iterator[uuid.UUID]:
    """A committed `uploading` file (seeded, not yet confirmed)."""
    with _committed_file("uploading") as file_id:
        yield file_id


@pytest.fixture
def race_engine() -> Iterator[Engine]:
    """Engine without pooling, so every Session opens its own physical connection."""
    engine = create_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    yield engine
    engine.dispose()
