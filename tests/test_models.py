"""Integration tests for app.models and app.db against the compose PostgreSQL."""
from pathlib import Path

from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.models import Base, UploadBatch, UploadFile
from tests.db_helpers import POC_ORG, POC_USER, SIZE

REPO = Path(__file__).resolve().parents[1]


def test_insert_and_read_batch_and_file(db_session: Session) -> None:
    """A batch and a file round-trip, with the LLD's server defaults filled in by PostgreSQL."""
    batch = UploadBatch(organization_id=POC_ORG, created_by=POC_USER)
    db_session.add(batch)
    db_session.flush()
    upload = UploadFile(batch_id=batch.batch_id, organization_id=POC_ORG, file_name="valid.docx",
                        file_ext="docx", size_bytes=SIZE, is_archive=False, s3_key="ClinSync/incoming/x/valid.docx")
    db_session.add(upload)
    db_session.flush()
    batch_id, file_id = batch.batch_id, upload.file_id
    db_session.expunge_all()

    read_batch = db_session.get(UploadBatch, batch_id)
    read_file = db_session.scalars(select(UploadFile).where(UploadFile.batch_id == batch_id)).one()
    assert read_batch is not None and read_batch.organization_id == POC_ORG
    assert read_batch.status == "in_progress" and read_batch.created_at is not None
    assert read_batch.created_by == POC_USER
    assert read_file.file_id == file_id and read_file.file_name == "valid.docx"
    assert read_file.status == "staged"                              # LLD M2.5: a new file starts staged
    assert read_file.size_bytes == SIZE and read_file.intent == "new" and read_file.client_ref
    assert read_file.entries_done == 0 and read_file.attempt_count == 0
    assert read_file.claim_token is None and read_file.updated_at is not None


def test_models_match_schema(db_session: Session) -> None:
    """The models map exactly the tables init.sql creates, each with the same columns and nullability."""
    inspector = inspect(db_session.connection())
    assert set(Base.metadata.tables) == set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        db_columns = {c["name"]: c["nullable"] for c in inspector.get_columns(table.name)}
        model_columns = {c.name: c.nullable for c in table.columns}
        assert model_columns == db_columns, table.name


def test_init_sql_is_the_spec_schema() -> None:
    """infra/postgres/init.sql is specs/upload-ingest-merge/schema.sql byte for byte (task 1.1)."""
    assert (REPO / "infra/postgres/init.sql").read_bytes() == \
        (REPO / "specs/upload-ingest-merge/schema.sql").read_bytes()


def test_poc_organization_is_seeded(db_session: Session) -> None:
    """init.sql seeds the one POC organization that every organization_id points at."""
    assert db_session.execute(text("SELECT name FROM organizations WHERE id = :id"),
                              {"id": POC_ORG}).scalar_one() == "Mytonomy (POC)"


def test_get_db_session_yields_working_session(pg_connection: Connection) -> None:
    """get_db_session yields a usable session and closes it when the generator finishes."""
    generator = get_db_session()
    session = next(generator)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    generator.close()
