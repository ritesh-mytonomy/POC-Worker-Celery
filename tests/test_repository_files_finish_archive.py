"""finish() decides processed vs partial from the candidate rows, and refuses an archive with missing entries
(rev 1.3; R7.6)."""
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db_session
from app.errors import InvalidInput
from app.main import app
from app.repositories.candidates import upsert
from app.repositories.files import claim, finish, progress
from tests.db_helpers import MAX_ATTEMPTS, STALE, full_row, make_file, triggers_off


def claimed_archive(db: Session, entries_total: int | None) -> tuple[uuid.UUID, uuid.UUID]:
    """A claimed .zip file with entries_total recorded (or not)."""
    file_id = make_file(db)
    db.execute(text("UPDATE upload_file SET is_archive = true, file_name = 'm.zip', file_ext = 'zip' "
                    "WHERE file_id = :id"), {"id": file_id})
    db.commit()
    token = claim(db, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE).claim_token
    if entries_total is not None:
        progress(db, file_id, token, entries_total=entries_total)
    return file_id, token


def add(db: Session, file_id: uuid.UUID, token: uuid.UUID, i: int, rejected: bool = False) -> None:
    """Upsert candidate i (processed, or rejected)."""
    if rejected:
        upsert(db, file_id, token, source_entry_name=f"e{i}.pdf", entry_index=i, file_name=f"e{i}.pdf",
               file_ext="pdf", status="rejected", reject_reason=".pdf is not supported")
    else:
        upsert(db, file_id, token, source_entry_name=f"e{i}.docx", entry_index=i, file_name=f"e{i}.docx",
               file_ext="docx", status="processed", s3_key=f"k{i}", size_bytes=1)


def test_processed_becomes_partial_when_any_candidate_is_rejected(db_session: Session) -> None:
    """The worker says processed (it may not know, after a resume); a rejected candidate makes it partial."""
    file_id, token = claimed_archive(db_session, 3)
    add(db_session, file_id, token, 0)
    add(db_session, file_id, token, 1, rejected=True)
    add(db_session, file_id, token, 2)
    assert finish(db_session, file_id, token, "processed") == "partial"
    assert full_row(db_session, file_id)["status"] == "partial"


def test_processed_stays_processed_when_every_candidate_passed(db_session: Session) -> None:
    """No rejected candidate → processed."""
    file_id, token = claimed_archive(db_session, 2)
    add(db_session, file_id, token, 0)
    add(db_session, file_id, token, 1)
    assert finish(db_session, file_id, token, "processed") == "processed"


def test_partial_is_never_turned_into_processed(db_session: Session) -> None:
    """The API only upgrades processed → partial; a partial sent by the worker is kept."""
    file_id, token = claimed_archive(db_session, 1)
    add(db_session, file_id, token, 0)
    assert finish(db_session, file_id, token, "partial") == "partial"


@pytest.mark.parametrize("status", ["processed", "partial"])
def test_archive_with_a_missing_candidate_is_refused(db_session: Session, status: str) -> None:
    """entries_total 3 but only 2 candidates (a worker skipped an entry) → InvalidInput; nothing changes."""
    file_id, token = claimed_archive(db_session, 3)
    add(db_session, file_id, token, 0)
    add(db_session, file_id, token, 2, rejected=True)
    before = full_row(db_session, file_id)
    with pytest.raises(InvalidInput, match="2 candidates but entries_total is 3"):
        finish(db_session, file_id, token, status)
    assert full_row(db_session, file_id) == before                     # still processing, token intact


def test_archive_without_entries_total_is_refused(db_session: Session) -> None:
    """An archive that never recorded entries_total cannot finish processed."""
    file_id, token = claimed_archive(db_session, None)
    with pytest.raises(InvalidInput, match="entries_total is None"):
        finish(db_session, file_id, token, "processed")


def test_empty_archive_with_zero_entries_finishes_processed(db_session: Session) -> None:
    """entries_total 0 and no candidates is consistent."""
    file_id, token = claimed_archive(db_session, 0)
    assert finish(db_session, file_id, token, "processed") == "processed"


@pytest.mark.parametrize("status", ["rejected", "error"])
def test_rejected_and_error_skip_the_count_check(db_session: Session, status: str) -> None:
    """An archive rejected or errored mid-way has fewer candidates than entries — that is expected."""
    file_id, token = claimed_archive(db_session, 5)
    add(db_session, file_id, token, 0)
    assert finish(db_session, file_id, token, status, "stopped") == status


def test_single_document_needs_no_entries_total(db_session: Session) -> None:
    """The count check applies to archives only."""
    file_id = make_file(db_session)
    token = claim(db_session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE).claim_token
    upsert(db_session, file_id, token, source_entry_name=None, entry_index=None, file_name="a.docx",
           file_ext="docx", status="processed", s3_key="k", size_bytes=1)
    assert finish(db_session, file_id, token, "processed") == "processed"


# --- through the route: a worker that skipped an entry is refused with 400 (never retried) ---

@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """The real app on the rolled-back test session."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_route_refuses_a_worker_that_skipped_an_entry(client: TestClient, db_session: Session) -> None:
    """POST finish processed with a missing candidate → 400 validation_error; the file stays processing."""
    file_id, token = claimed_archive(db_session, 3)
    add(db_session, file_id, token, 0)
    add(db_session, file_id, token, 1)
    headers: dict[str, Any] = {"X-Internal-Key": get_settings().INTERNAL_API_KEY, "X-Claim-Token": str(token)}
    response = client.post(f"/internal/files/{file_id}/finish", headers=headers, json={"status": "processed"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation_error"
    assert full_row(db_session, file_id)["status"] == "processing"
    add(db_session, file_id, token, 2, rejected=True)
    ok = client.post(f"/internal/files/{file_id}/finish", headers=headers, json={"status": "processed"})
    assert ok.status_code == 204 and full_row(db_session, file_id)["status"] == "partial"


# --- task 9.3: finish(rejected) on an archive rejects its processed candidates (design.md §8.5a) ---

def candidate_rows(db: Session, file_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Candidates of the file by entry name, with status, reason, s3_key and updated_at."""
    rows = db.execute(text("SELECT source_entry_name, status, reject_reason, s3_key, updated_at "
                           "FROM staged_document WHERE source_file_id = :id"), {"id": file_id}).mappings()
    return {r["source_entry_name"]: dict(r) for r in rows}


def test_rejected_archive_rejects_processed_candidates_and_keeps_specific_reasons(db_session: Session) -> None:
    """Processed → rejected 'Archive rejected: …' with s3_key cleared; already-rejected keep their own reason.
    A mid-way rejection has fewer candidates than entries_total — the count check does not apply."""
    file_id, token = claimed_archive(db_session, 5)
    add(db_session, file_id, token, 0)
    add(db_session, file_id, token, 1, rejected=True)
    add(db_session, file_id, token, 2)
    with triggers_off(db_session):
        db_session.execute(text("UPDATE staged_document SET updated_at = now() - interval '60 seconds' "
                                "WHERE source_file_id = :id"), {"id": file_id})
    db_session.commit()
    before = candidate_rows(db_session, file_id)
    message = "'e3.docx' does not match its index (Bad CRC-32 for file 'e3.docx')"
    assert finish(db_session, file_id, token, "rejected", message) == "rejected"
    after = candidate_rows(db_session, file_id)
    for name in ("e0.docx", "e2.docx"):
        assert after[name]["status"] == "rejected" and after[name]["s3_key"] is None
        assert after[name]["reject_reason"] == f"Archive rejected: {message}"
        assert after[name]["updated_at"] > before[name]["updated_at"]
    assert after["e1.pdf"] == before["e1.pdf"]                          # its own reason, untouched
    row = full_row(db_session, file_id)
    assert row["status"] == "rejected" and row["status_message"] == message and row["claim_token"] is None


def test_archive_rejected_reason_is_truncated(db_session: Session) -> None:
    """'Archive rejected: ' + a long message is cut to 512 characters, ending with an ellipsis."""
    file_id, token = claimed_archive(db_session, 2)
    add(db_session, file_id, token, 0)
    finish(db_session, file_id, token, "rejected", "x" * 600)
    reason = candidate_rows(db_session, file_id)["e0.docx"]["reject_reason"]
    assert len(reason) == 512 and reason.startswith("Archive rejected: xxx") and reason.endswith("…")


def test_error_finish_leaves_processed_candidates_unchanged(db_session: Session) -> None:
    """finish(error) part-way (attempts exhausted) is not a verdict on the archive: candidates stay processed."""
    file_id, token = claimed_archive(db_session, 4)
    add(db_session, file_id, token, 0)
    before = candidate_rows(db_session, file_id)
    finish(db_session, file_id, token, "error", "Could not be processed after 3 attempts")
    assert candidate_rows(db_session, file_id) == before


def test_rejected_single_document_touches_no_candidates(db_session: Session) -> None:
    """The candidate rejection is scoped to archives (a rejected document never has candidates anyway)."""
    file_id = make_file(db_session)
    token = claim(db_session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE).claim_token
    upsert(db_session, file_id, token, source_entry_name=None, entry_index=None, file_name="a.docx",
           file_ext="docx", status="processed", s3_key="k", size_bytes=1)
    before = candidate_rows(db_session, file_id)
    finish(db_session, file_id, token, "rejected", "odd, but possible in principle")
    assert candidate_rows(db_session, file_id) == before


def test_file_and_candidate_updates_commit_together(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the candidate UPDATE fails, the fenced file UPDATE is not committed either: one transaction."""
    file_id, token = claimed_archive(db_session, 3)
    add(db_session, file_id, token, 0)
    file_before, cands_before = full_row(db_session, file_id), candidate_rows(db_session, file_id)
    real_execute = db_session.execute

    def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "UPDATE staged_document" in str(statement):
            raise RuntimeError("database fell over mid-finish")
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", failing_execute)
    with pytest.raises(RuntimeError):
        finish(db_session, file_id, token, "rejected", "boom")
    monkeypatch.undo()
    db_session.rollback()                                   # what get_db_session's close does after an error
    assert full_row(db_session, file_id) == file_before     # still processing, token intact
    assert candidate_rows(db_session, file_id) == cands_before


def test_route_rejected_finish_has_the_same_effect(client: TestClient, db_session: Session) -> None:
    """POST finish {status: rejected} → 204; processed candidates rejected."""
    file_id, token = claimed_archive(db_session, 3)
    add(db_session, file_id, token, 0)
    headers: dict[str, Any] = {"X-Internal-Key": get_settings().INTERNAL_API_KEY, "X-Claim-Token": str(token)}
    response = client.post(f"/internal/files/{file_id}/finish", headers=headers,
                           json={"status": "rejected", "status_message": "'e1.docx' is encrypted"})
    assert response.status_code == 204
    row = candidate_rows(db_session, file_id)["e0.docx"]
    assert (row["status"], row["reject_reason"], row["s3_key"]) == (
        "rejected", "Archive rejected: 'e1.docx' is encrypted", None)
