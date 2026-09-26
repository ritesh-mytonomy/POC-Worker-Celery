"""Shared fixtures for compose PostgreSQL: rolled-back sessions and committed rows.

Database tests skip when PostgreSQL is unreachable, unless REQUIRE_DB=1, in which case they fail.
"""
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import get_engine
from tests.db_helpers import NOT_CONFIRMED, POC_ORG, POC_USER, SIZE


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
            text("INSERT INTO upload_batch (organization_id, created_by) VALUES (:org, :by) RETURNING batch_id"),
            {"org": POC_ORG, "by": POC_USER},
        ).scalar_one()
        file_id = conn.execute(
            text("""INSERT INTO upload_file (batch_id, organization_id, file_name, file_ext, size_bytes, is_archive,
                                             s3_key, status, uploaded_at)
                    SELECT batch_id, organization_id, 'race.docx', 'docx', :size, false,
                           'ClinSync/incoming/race.docx', :status,
                           CASE WHEN :confirmed THEN now() END
                    FROM upload_batch WHERE batch_id = :b RETURNING file_id"""),
            {"b": batch_id, "size": SIZE, "status": status, "confirmed": status not in NOT_CONFIRMED},
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
    """A committed `uploading` file (parts being sent, not yet confirmed)."""
    with _committed_file("uploading") as file_id:
        yield file_id


@pytest.fixture
def committed_staged_file() -> Iterator[uuid.UUID]:
    """A committed `staged` file (registered, no part sent yet — the LLD's starting status)."""
    with _committed_file("staged") as file_id:
        yield file_id


@pytest.fixture
def race_engine() -> Iterator[Engine]:
    """Engine without pooling, so every Session opens its own physical connection."""
    engine = create_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every design.md §10.1 fixture, built once per test session into a temporary directory."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
    import make_fixtures

    out = tmp_path_factory.mktemp("fixtures")
    make_fixtures.build(out)
    return out
