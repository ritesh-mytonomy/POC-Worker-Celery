"""Tests for the batch status routes (design.md §6.1; R14.1, R14.2)."""
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.main import app
from app.repositories.candidates import upsert
from app.repositories.files import progress
from tests.db_helpers import claimed, make_file

FILE_KEYS = {"file_id", "file_name", "status", "entries_total", "entries_done", "attempt_count",
             "detected_type", "status_message"}
STAGED_KEYS = {"staged_id", "source_file_id", "source_entry_name", "entry_index", "file_name", "status",
               "reject_reason"}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """The real app, with its sessions replaced by the rolled-back test session."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def batch_of(db: Session, file_id: uuid.UUID) -> uuid.UUID:
    """The batch_id of a file."""
    return db.execute(text("SELECT batch_id FROM upload_file WHERE file_id = :id"), {"id": file_id}).scalar_one()


def move_to_batch(db: Session, file_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    """Put a file (and its candidates) into another batch."""
    db.execute(text("UPDATE upload_file SET batch_id = :b WHERE file_id = :id"), {"b": batch_id, "id": file_id})
    db.execute(text("UPDATE staged_document SET batch_id = :b WHERE source_file_id = :id"),
               {"b": batch_id, "id": file_id})
    db.commit()


def test_batch_status_returns_every_file_with_r14_fields(client: TestClient, db_session: Session) -> None:
    """Each file carries exactly the R14.1 fields, in creation order; other batches are excluded."""
    archive, token = claimed(db_session)
    batch_id = batch_of(db_session, archive)
    progress(db_session, archive, token, entries_total=30, entries_done=11, detected_type="zip")
    waiting = make_file(db_session, status="uploading")
    move_to_batch(db_session, waiting, batch_id)
    make_file(db_session)                                   # another batch: must not appear
    # now() is fixed inside the test transaction, so give the archive an earlier created_at explicitly.
    db_session.execute(text("UPDATE upload_file SET created_at = now() - interval '10 seconds' WHERE file_id = :id"),
                       {"id": archive})
    db_session.commit()

    response = client.get(f"/api/v1/uploads/batches/{batch_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == str(batch_id)
    assert [f["file_id"] for f in body["files"]] == [str(archive), str(waiting)]
    assert all(set(f) == FILE_KEYS for f in body["files"])
    first = body["files"][0]
    assert (first["status"], first["entries_total"], first["entries_done"], first["attempt_count"],
            first["detected_type"], first["status_message"]) == ("processing", 30, 11, 1, "zip", None)
    assert body["files"][1]["status"] == "uploading" and body["files"][1]["entries_total"] is None


def test_staged_returns_every_candidate_with_r14_fields(client: TestClient, db_session: Session) -> None:
    """Candidates carry the R14.2 fields plus ids, no s3_key; direct upload first, then entries by index."""
    archive, token = claimed(db_session)
    batch_id = batch_of(db_session, archive)
    upsert(db_session, archive, token, source_entry_name="b.pdf", entry_index=1, file_name="b.pdf",
           file_ext="pdf", status="rejected", reject_reason=".pdf is not supported")
    upsert(db_session, archive, token, source_entry_name="a.docx", entry_index=0, file_name="a.docx",
           file_ext="docx", status="processed", s3_key="ClinSync/staging/k", size_bytes=5)
    direct, direct_token = claimed(db_session)
    upsert(db_session, direct, direct_token, source_entry_name=None, entry_index=None, file_name="valid.docx",
           file_ext="docx", status="processed", s3_key="ClinSync/staging/d", size_bytes=7)
    move_to_batch(db_session, direct, batch_id)
    other, other_token = claimed(db_session)                # another batch: must not appear
    upsert(db_session, other, other_token, source_entry_name=None, entry_index=None, file_name="x.docx",
           file_ext="docx", status="processed", s3_key="ClinSync/staging/x", size_bytes=1)

    response = client.get(f"/api/v1/uploads/batches/{batch_id}/staged")
    assert response.status_code == 200
    staged = response.json()["staged"]
    assert all(set(c) == STAGED_KEYS for c in staged)
    by_file = {}
    for c in staged:
        by_file.setdefault(c["source_file_id"], []).append((c["source_entry_name"], c["status"], c["reject_reason"]))
    assert by_file == {
        str(archive): [("a.docx", "processed", None), ("b.pdf", "rejected", ".pdf is not supported")],
        str(direct): [(None, "processed", None)],
    }
    assert [c["source_file_id"] for c in staged] == sorted(c["source_file_id"] for c in staged)


@pytest.mark.parametrize("suffix", ["", "/staged"])
def test_unknown_batch_is_404(client: TestClient, suffix: str) -> None:
    """An unknown batch → 404 not_found on both routes."""
    response = client.get(f"/api/v1/uploads/batches/{uuid.uuid4()}{suffix}")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("suffix", ["", "/staged"])
def test_malformed_batch_id_is_400(client: TestClient, suffix: str) -> None:
    """A malformed id → 400 validation_error on both routes."""
    response = client.get(f"/api/v1/uploads/batches/not-a-uuid{suffix}")
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation_error"


def test_batch_without_candidates_returns_empty_list(client: TestClient, db_session: Session) -> None:
    """A batch whose files produced nothing yet → 200 with an empty staged list."""
    batch_id = batch_of(db_session, make_file(db_session, status="uploading"))
    response = client.get(f"/api/v1/uploads/batches/{batch_id}/staged")
    assert response.status_code == 200 and response.json() == {"batch_id": str(batch_id), "staged": []}


def test_files_created_together_are_ordered_by_name(client: TestClient, db_session: Session) -> None:
    """Files seeded in one transaction share created_at; they come back by file_name, deterministically."""
    response = client.post("/poc/seed", json={"files": [
        {"file_name": n, "s3_key": f"ClinSync/incoming/o/{n}"} for n in ("c.docx", "a.zip", "b.docx")]})
    batch_id = response.json()["batch_id"]
    names = [f["file_name"] for f in client.get(f"/api/v1/uploads/batches/{batch_id}").json()["files"]]
    assert names == ["a.zip", "b.docx", "c.docx"]
