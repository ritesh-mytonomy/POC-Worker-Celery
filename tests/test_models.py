"""Integration tests for app.models and app.db against the compose PostgreSQL."""
import uuid

from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.models import Base, StagedDocument, UploadBatch, UploadFile


def test_insert_and_read_batch_and_file(db_session: Session) -> None:
    """A batch and a file round-trip, with server defaults filled in by PostgreSQL."""
    org = uuid.uuid4()
    batch = UploadBatch(organization_id=org)
    db_session.add(batch)
    db_session.flush()
    upload = UploadFile(batch_id=batch.batch_id, organization_id=org, file_name="valid.docx",
                        file_ext="docx", is_archive=False, s3_key="ClinSync/incoming/x/valid.docx")
    db_session.add(upload)
    db_session.flush()
    batch_id, file_id = batch.batch_id, upload.file_id
    db_session.expunge_all()

    read_batch = db_session.get(UploadBatch, batch_id)
    read_file = db_session.scalars(select(UploadFile).where(UploadFile.batch_id == batch_id)).one()
    assert read_batch is not None and read_batch.organization_id == org
    assert read_batch.status == "in_progress" and read_batch.created_at is not None
    assert read_file.file_id == file_id and read_file.file_name == "valid.docx"
    assert read_file.status == "uploading"
    assert read_file.entries_done == 0 and read_file.attempt_count == 0
    assert read_file.claim_token is None and read_file.updated_at is not None


def test_models_match_schema(db_session: Session) -> None:
    """Every model's columns and nullability match the tables created by init.sql."""
    inspector = inspect(db_session.connection())
    for table in Base.metadata.sorted_tables:
        db_columns = {c["name"]: c["nullable"] for c in inspector.get_columns(table.name)}
        model_columns = {c.name: c.nullable for c in table.columns}
        assert model_columns == db_columns, table.name
    assert StagedDocument.__table__.name in inspector.get_table_names()


def test_get_db_session_yields_working_session(pg_connection: Connection) -> None:
    """get_db_session yields a usable session and closes it when the generator finishes."""
    generator = get_db_session()
    session = next(generator)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    generator.close()
