"""Commit: add a batch's ready candidates to the library (upload-ingest-merge U6.3, U8; design.md §5.4)."""
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import storage
from app.db import get_db_session
from app.main import app
from app.naming import file_name_norm, title_norm, title_of
from app.repositories import audit, documents
from app.services import uploads
from app.services.uploads import ALREADY_IN_LIBRARY, SAME_FILE_IN_BATCH, TITLE_IN_LIBRARY
from tests.db_helpers import POC_ORG, POC_USER, SIZE, make_file
from tests.upload_fakes import FakeStorage, Recorder, enqueued, fake_storage  # noqa: F401 — fixtures

HASH_A, HASH_B, HASH_C = "aa" * 32, "bb" * 32, "cc" * 32


class SimulatedCrash(BaseException):
    """A process death at an exact point: no `except Exception` catches it."""


# ── setup: finished files and their candidates, as the worker leaves them ──

def new_file(db: Session, batch_id: uuid.UUID | None = None, status: str = "processed") -> tuple[uuid.UUID, uuid.UUID]:
    """A file in `status`, in batch_id (or its own new batch); return (batch_id, file_id)."""
    file_id = make_file(db, status=status)
    # clock_timestamp(), not now(): one test transaction would give every file the same created_at, and the
    # commit takes files in created_at order.
    db.execute(text("UPDATE upload_file SET batch_id = coalesce(:b, batch_id), created_at = clock_timestamp() "
                    "WHERE file_id = :f"), {"b": batch_id, "f": file_id})
    db.commit()
    own = db.execute(text("SELECT batch_id FROM upload_file WHERE file_id = :f"), {"f": file_id}).scalar_one()
    return own, file_id


def add_candidate(db: Session, file_id: uuid.UUID, name: str, content_hash: str | None = HASH_A,
                  status: str = "processed", index: int | None = None) -> uuid.UUID:
    """A candidate row as the upsert writes it: titles from app.naming, a staging key when processed."""
    staged_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO staged_document (staged_id, batch_id, organization_id, source_file_id, source_entry_name,
                                     entry_index, file_name, file_ext, size_bytes, content_hash, s3_key, status,
                                     reject_reason, proposed_title, title_norm)
        SELECT :sid, batch_id, organization_id, file_id, :entry, :idx, :name, 'docx', :size, :hash, :key, :status,
               :reason, :title, :title_norm
        FROM upload_file WHERE file_id = :f"""),
        {"sid": staged_id, "f": file_id, "entry": name if index is not None else None, "idx": index, "name": name,
         "size": SIZE, "hash": content_hash if status == "processed" else None,
         "key": f"ClinSync/staging/x/{file_id}/{name}" if status == "processed" else None, "status": status,
         "reason": None if status == "processed" else ".pdf is not supported", "title": title_of(name),
         "title_norm": title_norm(title_of(name))})
    db.commit()
    return staged_id


def library_document(db: Session, title: str, content_hash: str) -> uuid.UUID:
    """A document already in the library (from an earlier batch)."""
    document_id = uuid.uuid4()
    documents.insert(db, document_id=document_id, organization_id=POC_ORG, title=title, file_name=f"{title}.docx",
                     file_ext="docx", size_bytes=1, content_hash=content_hash,
                     s3_key=documents.document_key(POC_ORG, document_id, f"{title}.docx"),
                     source_file_id=new_file(db)[1], uploaded_by=POC_USER)
    db.commit()
    return document_id


def library(db: Session) -> list[dict[str, Any]]:
    """Every document in the POC organization, oldest first."""
    return [dict(r) for r in db.execute(text("SELECT * FROM documents WHERE organization_id = :o "
                                             "ORDER BY added_at, title_norm"), {"o": POC_ORG}).mappings()]


def batch_candidates(db: Session, batch_id: uuid.UUID) -> list[str]:
    """File names of the batch's remaining candidates."""
    return [r[0] for r in db.execute(text("SELECT file_name FROM staged_document WHERE batch_id = :b ORDER BY 1"),
                                     {"b": batch_id})]


def batch_status(db: Session, batch_id: uuid.UUID) -> str:
    """The batch's status."""
    return db.execute(text("SELECT status FROM upload_batch WHERE batch_id = :b"), {"b": batch_id}).scalar_one()


def audits(db: Session, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    """The batch_committed audit rows of the batch."""
    rows = db.execute(text("SELECT * FROM audit_log WHERE action = 'batch_committed' AND entity_id = :b "
                           "ORDER BY created_at"), {"b": str(batch_id)}).mappings()
    return [dict(r) for r in rows]


# ── one commit ──────────────────────────────────────────────────────────────

def test_a_commit_adds_the_document_copies_it_and_cleans_up(db_session: Session,
                                                            fake_storage: FakeStorage) -> None:  # noqa: F811
    """processed → a library document (uuid5 of the staged_id, processed/ key), copied from staging; the rejected
    candidate is deleted; candidates gone; staging deleted; one audit row; the batch committed."""
    batch_id, file_id = new_file(db_session, status="partial")
    staged_id = add_candidate(db_session, file_id, "Pre-op Guide.docx", HASH_A, index=0)
    add_candidate(db_session, file_id, "notes.pdf", status="rejected", index=1)
    result = uploads.commit(db_session, batch_id)

    document_id = uuid.uuid5(documents.DOCUMENT_NAMESPACE, str(staged_id))
    key = f"ClinSync/processed/{POC_ORG}/{document_id}/v1_Pre-op Guide.docx"
    assert (result.added, result.skipped, result.still_in_progress) == ([document_id], [], 0)
    (doc,) = library(db_session)
    assert (doc["document_id"], doc["title"], doc["title_norm"], doc["file_name_norm"], doc["s3_key"]) == \
        (document_id, "Pre-op Guide", "pre op guide", file_name_norm("Pre-op Guide.docx"), key)
    assert (doc["content_hash"], doc["size_bytes"], doc["current_version"], doc["source_file_id"],
            doc["version_uploaded_by"]) == (HASH_A, SIZE, 1, file_id, POC_USER)
    staging = f"ClinSync/staging/x/{file_id}/Pre-op Guide.docx"
    assert fake_storage.copies == [(staging, key)] and staging in fake_storage.deleted
    assert batch_candidates(db_session, batch_id) == [] and batch_status(db_session, batch_id) == "committed"
    (row,) = audits(db_session, batch_id)
    assert (row["organization_id"], row["user_id"], row["entity_type"]) == (POC_ORG, POC_USER, "upload_batch")
    assert row["details"] == {"added": [str(document_id)], "skipped": [], "still_in_progress": 0}


def test_a_second_commit_adds_nothing(db_session: Session, fake_storage: FakeStorage) -> None:  # noqa: F811
    """Idempotent: the candidates are gone, so a second commit adds 0, copies nothing, and still answers."""
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "a.docx")
    uploads.commit(db_session, batch_id)
    copies = list(fake_storage.copies)
    again = uploads.commit(db_session, batch_id)
    assert (again.added, again.skipped, again.still_in_progress) == ([], [], 0)
    assert fake_storage.copies == copies and len(library(db_session)) == 1
    assert len(audits(db_session, batch_id)) == 2                              # one audit row per commit


def test_two_identical_files_in_one_batch_become_one_document(db_session: Session,
                                                              fake_storage: FakeStorage) -> None:  # noqa: F811
    """The re-check sees the document added earlier in the same commit: the second is skipped, its staging
    object deleted with the rest."""
    batch_id, first = new_file(db_session)
    _, second = new_file(db_session, batch_id)
    add_candidate(db_session, first, "a.docx", HASH_A)
    later = add_candidate(db_session, second, "copy of a.docx", HASH_A)
    result = uploads.commit(db_session, batch_id)
    assert len(result.added) == 1 and len(library(db_session)) == 1
    assert result.skipped == [{"staged_id": later, "file_name": "copy of a.docx", "reason": SAME_FILE_IN_BATCH}]
    assert f"ClinSync/staging/x/{second}/copy of a.docx" in fake_storage.deleted


def test_two_files_with_the_same_title_become_one_document(db_session: Session,
                                                           fake_storage: FakeStorage) -> None:  # noqa: F811
    """Different bytes, same title ("Guide.docx" and "guide.DOCX"): one document, the other skipped (uq_org_title)."""
    batch_id, first = new_file(db_session)
    _, second = new_file(db_session, batch_id)
    add_candidate(db_session, first, "Guide.docx", HASH_A)
    add_candidate(db_session, second, "guide.DOCX", HASH_B)
    result = uploads.commit(db_session, batch_id)
    assert len(result.added) == 1 and [s["reason"] for s in result.skipped] == [TITLE_IN_LIBRARY]
    assert [d["title"] for d in library(db_session)] == ["Guide"]


def test_content_already_in_the_library_is_skipped(db_session: Session,
                                                   fake_storage: FakeStorage) -> None:  # noqa: F811
    """The same bytes under a new name, already added by an earlier batch → "Already in the library"."""
    library_document(db_session, "Original", HASH_A)
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "renamed.docx", HASH_A)
    result = uploads.commit(db_session, batch_id)
    assert result.added == [] and [s["reason"] for s in result.skipped] == [ALREADY_IN_LIBRARY]
    assert fake_storage.copies == [] and batch_status(db_session, batch_id) == "committed"


def test_a_title_clash_the_recheck_missed_becomes_a_skip(db_session: Session, fake_storage: FakeStorage,  # noqa: F811
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """If uq_org_title still fires (a writer outside commit), only that insert's savepoint rolls back: the
    candidate is skipped with the title reason and the commit carries on with the next."""
    library_document(db_session, "Guide", HASH_C)
    batch_id, first = new_file(db_session)
    _, second = new_file(db_session, batch_id)
    add_candidate(db_session, first, "Guide.docx", HASH_A)
    add_candidate(db_session, second, "Other.docx", HASH_B)
    monkeypatch.setattr(documents, "find_duplicate", lambda *_: None)          # the re-check misses the clash
    result = uploads.commit(db_session, batch_id)
    assert [s["reason"] for s in result.skipped] == [TITLE_IN_LIBRARY] and len(result.added) == 1
    assert sorted(d["title"] for d in library(db_session)) == ["Guide", "Other"]


def test_copies_happen_before_the_commit_and_deletes_after(db_session: Session, fake_storage: FakeStorage,  # noqa: F811
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """U8.4: staging → processed copies come before db.commit; staging objects are deleted only after it."""
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "a.docx")
    real_commit = db_session.commit
    monkeypatch.setattr(db_session, "commit", lambda: (fake_storage.calls.append("COMMIT"), real_commit())[1])
    uploads.commit(db_session, batch_id)
    calls = fake_storage.calls
    assert calls.index("copy") < calls.index("COMMIT") < calls.index("delete_many")


def test_a_failed_cleanup_after_the_commit_still_answers(db_session: Session, fake_storage: FakeStorage,  # noqa: F811
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The documents are durable once committed; a staging delete that fails is logged, not raised."""
    def down(_: Any) -> None:
        raise ClientError({"Error": {"Code": "InternalError", "Message": "S3 down"}}, "DeleteObjects")

    monkeypatch.setattr(storage, "delete_many", down)
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "a.docx")
    assert len(uploads.commit(db_session, batch_id).added) == 1 and len(library(db_session)) == 1


# ── incremental ─────────────────────────────────────────────────────────────

def test_incremental_commit_then_more_finishes_then_commit_again(db_session: Session,
                                                                 fake_storage: FakeStorage) -> None:  # noqa: F811
    """U8.2: a commit adds what is ready and leaves the file still processing; after it finishes, the next commit
    adds it; a third adds nothing. The batch is committed only once nothing is left."""
    batch_id, done = new_file(db_session)
    _, busy = new_file(db_session, batch_id, status="processing")
    add_candidate(db_session, done, "ready.docx", HASH_A)
    add_candidate(db_session, busy, "entry0.docx", HASH_B, index=0)       # an archive part-way through

    first = uploads.commit(db_session, batch_id)
    assert (len(first.added), first.still_in_progress) == (1, 1)
    assert batch_candidates(db_session, batch_id) == ["entry0.docx"] and batch_status(db_session, batch_id) == \
        "in_progress"

    db_session.execute(text("UPDATE upload_file SET status = 'processed' WHERE file_id = :f"), {"f": busy})
    db_session.commit()
    second = uploads.commit(db_session, batch_id)
    assert (len(second.added), second.still_in_progress) == (1, 0) and batch_status(db_session, batch_id) == \
        "committed"
    third = uploads.commit(db_session, batch_id)
    assert (third.added, third.skipped) == ([], []) and len(library(db_session)) == 2


def test_a_file_still_uploading_keeps_the_batch_open(db_session: Session,
                                                     fake_storage: FakeStorage) -> None:  # noqa: F811
    """A file of the batch not yet completed counts as in progress, so the batch is not committed yet."""
    batch_id, done = new_file(db_session)
    new_file(db_session, batch_id, status="uploading")
    add_candidate(db_session, done, "a.docx")
    assert uploads.commit(db_session, batch_id).still_in_progress == 1
    assert batch_status(db_session, batch_id) == "in_progress"


# ── crash safety ────────────────────────────────────────────────────────────

def test_a_crash_after_the_copies_then_a_rerun_leaves_no_orphans(
    db_session: Session, fake_storage: FakeStorage, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Die after both copies, before db.commit: the rows roll back, the copies stay. The re-run derives the same
    document ids → the same keys, and overwrites them: processed/ holds exactly the documents' objects."""
    batch_id, first = new_file(db_session)
    _, second = new_file(db_session, batch_id)
    add_candidate(db_session, first, "a.docx", HASH_A)
    add_candidate(db_session, second, "b.docx", HASH_B)
    real_write = audit.write

    def crash_once(*a: Any, **k: Any) -> None:
        monkeypatch.setattr(audit, "write", real_write)
        raise SimulatedCrash("after the copies, before db.commit")

    monkeypatch.setattr(audit, "write", crash_once)
    with pytest.raises(SimulatedCrash):
        uploads.commit(db_session, batch_id)
    assert library(db_session) == [] and len(batch_candidates(db_session, batch_id)) == 2       # rolled back
    assert len([k for k in fake_storage.objects if "/processed/" in k]) == 2                      # copies remain

    assert len(uploads.commit(db_session, batch_id).added) == 2
    processed = {k for k in fake_storage.objects if "/processed/" in k}
    assert processed == {d["s3_key"] for d in library(db_session)}, "no orphans in processed/"


def test_a_rerun_that_now_discards_a_candidate_deletes_its_earlier_copy(
    db_session: Session, fake_storage: FakeStorage, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Crash after the copies; meanwhile another batch adds the same content; the re-run discards that candidate —
    and deletes the copy the crashed run made for it, at its deterministic key."""
    batch_id, first = new_file(db_session)
    _, second = new_file(db_session, batch_id)
    add_candidate(db_session, first, "a.docx", HASH_A)
    add_candidate(db_session, second, "b.docx", HASH_B)
    real_write = audit.write

    def crash_once(*a: Any, **k: Any) -> None:
        monkeypatch.setattr(audit, "write", real_write)
        raise SimulatedCrash("after the copies")

    monkeypatch.setattr(audit, "write", crash_once)
    with pytest.raises(SimulatedCrash):
        uploads.commit(db_session, batch_id)
    library_document(db_session, "Elsewhere", HASH_B)                         # another batch, same bytes as b
    result = uploads.commit(db_session, batch_id)
    assert len(result.added) == 1 and [s["reason"] for s in result.skipped] == [ALREADY_IN_LIBRARY]
    processed = {k for k in fake_storage.objects if "/processed/" in k and "Elsewhere" not in k}
    assert processed == {d["s3_key"] for d in library(db_session) if d["title"] != "Elsewhere"}


# ── concurrency: committed rows, separate connections ──────────────────────

@pytest.fixture
def committed_batches(race_engine: Engine) -> Iterator[list[uuid.UUID]]:
    """Two batches, committed, each with one finished file whose candidate has the SAME bytes, different names."""
    batches: list[uuid.UUID] = []
    with race_engine.begin() as conn:
        for name in ("one.docx", "two.docx"):
            batch_id = conn.execute(text("INSERT INTO upload_batch (organization_id, created_by) VALUES (:o, 0) "
                                         "RETURNING batch_id"), {"o": POC_ORG}).scalar_one()
            file_id = conn.execute(text("""
                INSERT INTO upload_file (batch_id, organization_id, file_name, file_ext, size_bytes, is_archive, s3_key,
                                         status)
                VALUES (:b, :o, :n, 'docx', 1, false, 'ClinSync/incoming/x', 'processed') RETURNING file_id"""),
                {"b": batch_id, "o": POC_ORG, "n": name}).scalar_one()
            conn.execute(text("""
                INSERT INTO staged_document (batch_id, organization_id, source_file_id, file_name, file_ext,
                                             size_bytes, content_hash, s3_key, status, proposed_title, title_norm)
                VALUES (:b, :o, :f, :n, 'docx', 1, :h, :k, 'processed', :t, :tn)"""),
                {"b": batch_id, "o": POC_ORG, "f": file_id, "n": name, "h": HASH_C, "k": f"ClinSync/staging/{name}",
                 "t": title_of(name), "tn": title_norm(title_of(name))})
            batches.append(batch_id)
    yield batches
    with race_engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE entity_id = ANY(:ids)"), {"ids": [str(b) for b in batches]})
        conn.execute(text("DELETE FROM documents WHERE content_hash = :h"), {"h": HASH_C})
        conn.execute(text("DELETE FROM upload_batch WHERE batch_id = ANY(:ids)"), {"ids": batches})


def test_two_batches_with_the_same_content_committing_at_once_add_one_document(
    race_engine: Engine, committed_batches: list[uuid.UUID], fake_storage: FakeStorage  # noqa: F811
) -> None:
    """The organization lock serializes the two commits: whichever goes second sees the first's document."""
    barrier, results, errors = threading.Barrier(2), {}, []

    def go(batch_id: uuid.UUID) -> None:
        barrier.wait()
        try:
            with Session(race_engine) as session:
                results[batch_id] = uploads.commit(session, batch_id)
        except Exception as exc:                                       # surfaced below, not lost in the thread
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(b,)) for b in committed_batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with race_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM documents WHERE content_hash = :h"), {"h": HASH_C}).scalar() == 1
    assert sorted(len(r.added) for r in results.values()) == [0, 1]
    assert [s["reason"] for r in results.values() for s in r.skipped] == [ALREADY_IN_LIBRARY]


def test_two_commits_of_one_batch_at_once_add_each_document_once(
    race_engine: Engine, committed_batches: list[uuid.UUID], fake_storage: FakeStorage  # noqa: F811
) -> None:
    """Same batch, two commits together: one adds the document, the other finds nothing left."""
    batch_id = committed_batches[0]
    barrier, results = threading.Barrier(2), []

    def go() -> None:
        barrier.wait()
        with Session(race_engine) as session:
            results.append(uploads.commit(session, batch_id))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(len(r.added) for r in results) == [0, 1]


# ── the route ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_session: Session, fake_storage: FakeStorage) -> Iterator[TestClient]:  # noqa: F811
    """The real app on the rolled-back session, with fake S3."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_the_route_answers_what_the_commit_did(client: TestClient, db_session: Session) -> None:
    """POST …/commit → {added, skipped: [{staged_id, file_name, reason}], still_in_progress, documents}."""
    batch_id, first = new_file(db_session)
    _, second = new_file(db_session, batch_id)
    staged = add_candidate(db_session, first, "a.docx", HASH_A)
    later = add_candidate(db_session, second, "a copy.docx", HASH_A)
    body = client.post(f"/api/v1/uploads/batches/{batch_id}/commit").json()
    assert body == {"added": 1, "still_in_progress": 0,
                    "documents": [str(uuid.uuid5(documents.DOCUMENT_NAMESPACE, str(staged)))],
                    "skipped": [{"staged_id": str(later), "file_name": "a copy.docx", "reason": SAME_FILE_IN_BATCH}]}


def test_an_unknown_batch_is_404_in_the_envelope(client: TestClient) -> None:
    """/api/v1 keeps the {"error": …} envelope."""
    response = client.post(f"/api/v1/uploads/batches/{uuid.uuid4()}/commit")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_s3_failing_mid_commit_adds_nothing(client: TestClient, db_session: Session,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy that fails → 502 storage_error; the transaction rolls back, so the candidate is still there."""
    def down(*_: Any) -> None:
        raise ClientError({"Error": {"Code": "InternalError", "Message": "S3 down"}}, "CopyObject")

    monkeypatch.setattr(storage, "copy", down)
    batch_id, file_id = new_file(db_session)
    add_candidate(db_session, file_id, "a.docx")
    response = client.post(f"/api/v1/uploads/batches/{batch_id}/commit")
    assert response.status_code == 502 and response.json()["error"]["code"] == "storage_error"
    assert library(db_session) == [] and batch_candidates(db_session, batch_id) == ["a.docx"]
