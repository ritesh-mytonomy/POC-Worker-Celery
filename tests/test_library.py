"""Batch review counts and duplicate markers (U7), and the Library routes (U9.1, U9.2)."""
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import storage
from app.config import get_settings
from app.db import get_db_session
from app.main import app
from app.repositories import documents
from tests.db_helpers import POC_ORG
from tests.test_commit import HASH_A, HASH_B, add_candidate, library_document, new_file
from tests.upload_fakes import FakeStorage, fake_storage  # noqa: F401 — fixture


@pytest.fixture
def client(db_session: Session, fake_storage: FakeStorage) -> Iterator[TestClient]:  # noqa: F811
    """The real app on the rolled-back session, fake S3 for commits, the compose endpoints for presigning."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ── batch review (U7) ───────────────────────────────────────────────────────

def test_batch_reports_ready_to_add_and_in_progress(client: TestClient, db_session: Session) -> None:
    """ready_to_add: processed candidates of finished files, not duplicates; in_progress: unfinished files."""
    library_document(db_session, "Known", HASH_B)
    batch_id, done = new_file(db_session)
    _, busy = new_file(db_session, batch_id, status="processing")
    new_file(db_session, batch_id, status="uploading")
    add_candidate(db_session, done, "ready.docx", HASH_A, index=0)
    add_candidate(db_session, done, "notes.pdf", status="rejected", index=1)
    add_candidate(db_session, busy, "later.docx", "cc" * 32, index=0)                 # file not finished yet
    dup = add_candidate(db_session, done, "again.docx", HASH_B, index=2)
    db_session.execute(text("UPDATE staged_document SET duplicate_of_document_id = "
                            "(SELECT document_id FROM documents WHERE content_hash = :h), duplicate_kind = "
                            "'same_content' WHERE staged_id = :id"), {"h": HASH_B, "id": dup})
    db_session.commit()
    body = client.get(f"/api/v1/uploads/batches/{batch_id}").json()
    assert (body["ready_to_add"], body["in_progress"]) == (1, 2)
    assert body["pending_review"] == 3            # ready + rejected + duplicate of the finished file; not busy's


def test_pending_review_empties_after_a_commit(client: TestClient, db_session: Session) -> None:
    """A batch holding only a duplicate and a rejection: ready_to_add 0 but pending_review 2; a commit clears them."""
    library_document(db_session, "Known", HASH_B)
    batch_id, file_id = new_file(db_session, status="partial")
    add_candidate(db_session, file_id, "known again.docx", HASH_B, index=0)
    add_candidate(db_session, file_id, "notes.pdf", status="rejected", index=1)
    db_session.execute(text("UPDATE staged_document SET duplicate_of_document_id = "
                            "(SELECT document_id FROM documents WHERE content_hash = :h), duplicate_kind = "
                            "'same_content' WHERE source_file_id = :f AND status = 'processed'"),
                       {"h": HASH_B, "f": file_id})
    db_session.commit()
    before = client.get(f"/api/v1/uploads/batches/{batch_id}").json()
    assert (before["ready_to_add"], before["pending_review"]) == (0, 2)
    client.post(f"/api/v1/uploads/batches/{batch_id}/commit")
    after = client.get(f"/api/v1/uploads/batches/{batch_id}").json()
    assert (after["ready_to_add"], after["pending_review"], after["in_progress"]) == (0, 0, 0)


def test_candidates_carry_their_duplicate_marker_with_the_documents_title(client: TestClient,
                                                                          db_session: Session) -> None:
    """…/staged: duplicate_of {document_id, title, kind} for a marked candidate, null otherwise; content_hash."""
    document_id = library_document(db_session, "Pre-op Guide", HASH_B)
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "fresh.docx", HASH_A, index=0)
    dup = add_candidate(db_session, file_id, "pre op guide.docx", HASH_A.replace("a", "d"), index=1)
    db_session.execute(text("UPDATE staged_document SET duplicate_of_document_id = :d, duplicate_kind = 'same_title' "
                            "WHERE staged_id = :id"), {"d": document_id, "id": dup})
    db_session.commit()
    staged = {c["file_name"]: c for c in client.get(f"/api/v1/uploads/batches/{batch_id}/staged").json()["staged"]}
    assert staged["fresh.docx"]["duplicate_of"] is None and staged["fresh.docx"]["content_hash"] == HASH_A
    assert staged["pre op guide.docx"]["duplicate_of"] == {"document_id": str(document_id), "title": "Pre-op Guide",
                                                           "kind": "same_title"}


def test_added_candidates_leave_the_review_list(client: TestClient, db_session: Session) -> None:
    """After a commit its candidates are deleted (D12): the review list is empty, ready_to_add 0."""
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "a.docx")
    client.post(f"/api/v1/uploads/batches/{batch_id}/commit")
    assert client.get(f"/api/v1/uploads/batches/{batch_id}/staged").json()["staged"] == []
    assert client.get(f"/api/v1/uploads/batches/{batch_id}").json()["ready_to_add"] == 0


# ── the Library (U9) ────────────────────────────────────────────────────────

def test_library_lists_documents_newest_first_with_a_stable_tie_break(client: TestClient,
                                                                      db_session: Session) -> None:
    """Newest first; documents added together (one commit, one added_at) come back by title, every time."""
    old = library_document(db_session, "Oldest", "01" * 32)
    db_session.execute(text("UPDATE documents SET added_at = now() - interval '1 day' WHERE document_id = :d"),
                       {"d": old})
    batch_id, file_id = new_file(db_session)
    for i, name in enumerate(("zeta.docx", "Alpha.docx", "mid.docx")):
        add_candidate(db_session, file_id, name, f"{i + 2:02d}" * 32, index=i)
    client.post(f"/api/v1/uploads/batches/{batch_id}/commit")                         # one added_at for all three
    for _ in range(3):
        body = client.get("/api/v1/library/documents").json()
        assert [d["title"] for d in body["documents"]] == ["Alpha", "mid", "zeta", "Oldest"]
    assert body["total"] == 4
    assert set(body["documents"][0]) == {"document_id", "title", "file_name", "file_ext", "size_bytes", "created_at"}


def test_download_is_a_presigned_get_on_the_public_host(client: TestClient, db_session: Session,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """URL on localhost:4566 (not the internal host), saving as the file name, expiring in 300 s."""
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    for cached in (get_settings, storage.public_client, storage.internal_client):
        cached.cache_clear()
    try:
        document_id = library_document(db_session, "Pré op", HASH_A)
        before = datetime.now(timezone.utc)
        body = client.get(f"/api/v1/library/documents/{document_id}/download").json()
    finally:
        for cached in (get_settings, storage.public_client, storage.internal_client):
            cached.cache_clear()
    url = urlsplit(body["url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}" == "http://localhost:4566"
    assert unquote(url.path) == \
        f"/{get_settings().S3_BUCKET}/{documents.document_key(POC_ORG, document_id, 'Pré op.docx')}"
    assert query["X-Amz-Expires"] == "300"
    assert unquote(query["response-content-disposition"].split("filename*=UTF-8''")[1]) == "Pré op.docx"
    expires = datetime.fromisoformat(body["expires_at"])
    assert 295 <= (expires - before).total_seconds() <= 305


def test_unknown_document_download_is_404(client: TestClient) -> None:
    """An id not in the organization's library → 404 in the envelope."""
    response = client.get(f"/api/v1/library/documents/{uuid.uuid4()}/download")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
