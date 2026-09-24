"""Tests for the fenced writes heartbeat, progress, finish, release (design.md §6.2; R4.4, R10.3)."""
import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import ClaimSuperseded
from app.repositories.files import claim, finish, heartbeat, progress, release
from tests.db_helpers import MAX_ATTEMPTS, STALE, backdate_timestamps, changed, claimed, full_row, set_heartbeat_age

Write = Callable[[Session, uuid.UUID, uuid.UUID], None]

# One call of each write with valid arguments.
WRITES: dict[str, Write] = {
    "heartbeat": lambda s, f, t: heartbeat(s, f, t),
    "progress": lambda s, f, t: progress(s, f, t, entries_done=1),
    "finish": lambda s, f, t: finish(s, f, t, "processed"),
    "release": lambda s, f, t: release(s, f, t, reason="test"),
}


def assert_superseded_and_unchanged(session: Session, write: Write, file_id: uuid.UUID, token: uuid.UUID) -> None:
    """The write raises ClaimSuperseded and leaves every column as it was."""
    before = full_row(session, file_id)
    with pytest.raises(ClaimSuperseded):
        write(session, file_id, token)
    assert full_row(session, file_id) == before


# --- what each write sets, exactly ---

def test_heartbeat_sets_only_heartbeat_and_updated_at(db_session: Session) -> None:
    """heartbeat changes heartbeat_at and updated_at and nothing else."""
    file_id, token = claimed(db_session)
    backdate_timestamps(db_session, file_id)
    before = full_row(db_session, file_id)
    heartbeat(db_session, file_id, token)
    assert changed(before, full_row(db_session, file_id)) == {"heartbeat_at", "updated_at"}


@pytest.mark.parametrize("fields", [
    {"entries_total": 30}, {"entries_done": 7}, {"detected_type": "docx"},
    {"entries_total": 30, "entries_done": 7, "detected_type": "zip"},
])
def test_progress_sets_only_passed_fields(db_session: Session, fields: dict[str, Any]) -> None:
    """progress changes only the fields passed, plus heartbeat_at and updated_at."""
    file_id, token = claimed(db_session)
    backdate_timestamps(db_session, file_id)
    before = full_row(db_session, file_id)
    progress(db_session, file_id, token, **fields)
    after = full_row(db_session, file_id)
    assert changed(before, after) == set(fields) | {"heartbeat_at", "updated_at"}
    assert all(after[name] == value for name, value in fields.items())


def test_progress_with_no_fields_raises_value_error(db_session: Session) -> None:
    """progress with nothing to set is a programming error and writes nothing."""
    file_id, token = claimed(db_session)
    before = full_row(db_session, file_id)
    with pytest.raises(ValueError):
        progress(db_session, file_id, token)
    assert full_row(db_session, file_id) == before


@pytest.mark.parametrize("status", ["processed", "partial", "rejected", "error"])
def test_finish_sets_terminal_status_and_clears_token(db_session: Session, status: str) -> None:
    """finish sets the status and message and clears claim_token."""
    file_id, token = claimed(db_session)
    backdate_timestamps(db_session, file_id)
    before = full_row(db_session, file_id)
    finish(db_session, file_id, token, status, "done")
    after = full_row(db_session, file_id)
    assert changed(before, after) == {"status", "status_message", "claim_token", "updated_at"}
    assert after["status"] == status and after["status_message"] == "done" and after["claim_token"] is None


@pytest.mark.parametrize("status", ["uploaded", "processing", "uploading", "bogus"])
def test_finish_refuses_non_terminal_status(db_session: Session, status: str) -> None:
    """finish with a non-terminal status raises ValueError and writes nothing."""
    file_id, token = claimed(db_session)
    before = full_row(db_session, file_id)
    with pytest.raises(ValueError):
        finish(db_session, file_id, token, status)
    assert full_row(db_session, file_id) == before


def test_finish_truncates_long_message_with_ellipsis(db_session: Session) -> None:
    """A message longer than 512 characters is cut to 512, ending with an ellipsis."""
    file_id, token = claimed(db_session)
    finish(db_session, file_id, token, "error", "x" * 600)
    message = full_row(db_session, file_id)["status_message"]
    assert len(message) == 512 and message.endswith("…") and message[:511] == "x" * 511


def test_finish_keeps_message_of_exactly_512(db_session: Session) -> None:
    """A 512-character message is stored as-is."""
    file_id, token = claimed(db_session)
    finish(db_session, file_id, token, "error", "y" * 512)
    assert full_row(db_session, file_id)["status_message"] == "y" * 512


def test_release_returns_to_uploaded_and_resets_uploaded_at(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """release sets uploaded, clears the token, resets uploaded_at, keeps attempt_count; logs attempt and reason."""
    file_id, token = claimed(db_session)
    backdate_timestamps(db_session, file_id)
    before = full_row(db_session, file_id)
    with caplog.at_level(logging.INFO, logger="app.repositories.files"):
        release(db_session, file_id, token, reason="S3 unavailable")
    after = full_row(db_session, file_id)
    assert changed(before, after) == {"status", "claim_token", "uploaded_at", "updated_at"}
    assert after["status"] == "uploaded" and after["claim_token"] is None
    assert after["uploaded_at"] > before["uploaded_at"] and after["attempt_count"] == 1
    record = next(r for r in caplog.records if r.getMessage() == "released")
    assert record.fields == {"file_id": str(file_id), "attempt": 1, "reason": "S3 unavailable"}


def test_released_file_can_be_claimed_again(db_session: Session) -> None:
    """After release the retry's claim succeeds with a new token and attempt_count 2."""
    file_id, token = claimed(db_session)
    release(db_session, file_id, token, reason="retry")
    again = claim(db_session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)
    assert again is not None and again.attempt_count == 2 and again.claim_token != token


# --- fencing: every write, every way of losing ownership ---

@pytest.mark.parametrize("name", WRITES)
def test_write_succeeds_with_current_token(db_session: Session, name: str) -> None:
    """Each write succeeds with the current token."""
    file_id, token = claimed(db_session)
    WRITES[name](db_session, file_id, token)


@pytest.mark.parametrize("name", WRITES)
def test_write_with_old_token_after_stale_takeover_raises(db_session: Session, name: str) -> None:
    """After a stale takeover the old token is rejected (token fence); the new token still works."""
    file_id, old_token = claimed(db_session)
    set_heartbeat_age(db_session, file_id, STALE + 5)
    takeover = claim(db_session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)
    assert takeover is not None
    assert_superseded_and_unchanged(db_session, WRITES[name], file_id, old_token)
    WRITES[name](db_session, file_id, takeover.claim_token)


@pytest.mark.parametrize("name", WRITES)
def test_write_after_finish_raises_even_with_correct_token(db_session: Session, name: str) -> None:
    """Once finished, the file accepts no further writes, even with the token that finished it."""
    file_id, token = claimed(db_session)
    finish(db_session, file_id, token, "processed")
    assert_superseded_and_unchanged(db_session, WRITES[name], file_id, token)


@pytest.mark.parametrize("name", WRITES)
def test_write_after_release_raises_even_with_correct_token(db_session: Session, name: str) -> None:
    """Once released, the old token cannot write; the retry must claim again."""
    file_id, token = claimed(db_session)
    release(db_session, file_id, token, reason="retry")
    assert_superseded_and_unchanged(db_session, WRITES[name], file_id, token)


@pytest.mark.parametrize("name", WRITES)
def test_status_fence_alone_rejects_matching_token(db_session: Session, name: str) -> None:
    """A matching token on a row that is not processing is still rejected (status fence on its own)."""
    file_id, token = claimed(db_session)
    db_session.execute(text("UPDATE upload_file SET status = 'uploaded' WHERE file_id = :id"), {"id": file_id})
    db_session.commit()
    assert_superseded_and_unchanged(db_session, WRITES[name], file_id, token)


@pytest.mark.parametrize("name", WRITES)
def test_write_with_random_token_or_unknown_file_raises(db_session: Session, name: str) -> None:
    """A token that was never issued, or a file that does not exist, is rejected."""
    file_id, _ = claimed(db_session)
    assert_superseded_and_unchanged(db_session, WRITES[name], file_id, uuid.uuid4())
    with pytest.raises(ClaimSuperseded):
        WRITES[name](db_session, uuid.uuid4(), uuid.uuid4())
