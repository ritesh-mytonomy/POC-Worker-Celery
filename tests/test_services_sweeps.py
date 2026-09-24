"""Tests for services.sweeps (design.md §8.7; R11.4, R11.5)."""
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.repositories.files import release
from app.services.sweeps import reconcile_sweep, stale_sweep
from tests.db_helpers import (
    MAX_ATTEMPTS, STALE, claimed, full_row, make_file, set_heartbeat_age, set_uploaded_age,
)

AGE = 30                                 # RECONCILE_AFTER_SECONDS, POC value
ERROR_MESSAGE = "Processing did not complete after the maximum number of attempts"


class FakeEnqueue:
    """Records enqueue calls; raises for file_ids in `fail`."""

    def __init__(self, fail: set[uuid.UUID] | None = None) -> None:
        """Start with no calls; fail for the given file_ids."""
        self.calls: list[tuple[str, str]] = []
        self.fail = {str(f) for f in (fail or set())}

    def __call__(self, file_id: str, organization_id: str) -> None:
        """Record the call, or raise as an unreachable Redis would."""
        if file_id in self.fail:
            raise ConnectionError("Redis unreachable")
        self.calls.append((file_id, organization_id))

    @property
    def file_ids(self) -> list[str]:
        """The file_ids enqueued, in order."""
        return [f for f, _ in self.calls]


def stale(db: Session, enqueue: FakeEnqueue) -> dict[str, int]:
    """Run the stale sweep with the test limits."""
    return stale_sweep(db, stale_after_seconds=STALE, max_attempts=MAX_ATTEMPTS, enqueue=enqueue)


def reconcile(db: Session, enqueue: FakeEnqueue) -> dict[str, int]:
    """Run the reconcile sweep with the test limits."""
    return reconcile_sweep(db, reconcile_after_seconds=AGE, max_attempts=MAX_ATTEMPTS, enqueue=enqueue)


def processing_file(db: Session, *, attempts: int, heartbeat_age: int) -> uuid.UUID:
    """A processing file with a claim token, the given attempt count and heartbeat age."""
    file_id = make_file(db, attempt_count=attempts)
    db.execute(text("UPDATE upload_file SET claim_token = gen_random_uuid() WHERE file_id = :id"), {"id": file_id})
    set_heartbeat_age(db, file_id, heartbeat_age)
    return file_id


def backdate_updated_at(db: Session, file_id: uuid.UUID) -> None:
    """Age updated_at, so a sweep stamping now() visibly changes it."""
    db.execute(text("UPDATE upload_file SET updated_at = now() - interval '60 seconds' WHERE file_id = :id"),
               {"id": file_id})
    db.commit()


def uploaded_file(db: Session, *, attempts: int, uploaded_age: int, status: str = "uploaded") -> uuid.UUID:
    """A file in `status` with the given attempt count and uploaded_at age."""
    file_id = make_file(db, status=status, attempt_count=attempts)
    set_uploaded_age(db, file_id, uploaded_age)
    return file_id


# --- stale sweep ---

def test_stale_sweep_resets_below_max_and_errors_at_max(db_session: Session) -> None:
    """Stale files below MAX_ATTEMPTS go back to uploaded and are enqueued; at MAX_ATTEMPTS they error."""
    to_reset = processing_file(db_session, attempts=1, heartbeat_age=STALE + 5)
    to_error = processing_file(db_session, attempts=MAX_ATTEMPTS, heartbeat_age=STALE + 5)
    fresh = [processing_file(db_session, attempts=a, heartbeat_age=0) for a in (1, MAX_ATTEMPTS)]
    backdate_updated_at(db_session, to_error)
    error_before = full_row(db_session, to_error)
    fresh_before = [full_row(db_session, f) for f in fresh]
    enqueue = FakeEnqueue()

    assert stale(db_session, enqueue) == {"reset": 1, "errored": 1, "enqueue_failed": 0}

    reset_row, error_row = full_row(db_session, to_reset), full_row(db_session, to_error)
    assert reset_row["status"] == "uploaded" and reset_row["claim_token"] is None
    assert reset_row["attempt_count"] == 1
    assert error_row["status"] == "error" and error_row["claim_token"] is None
    assert error_row["status_message"] == ERROR_MESSAGE
    assert error_row["updated_at"] > error_before["updated_at"]
    assert enqueue.calls == [(str(to_reset), str(reset_row["organization_id"]))]
    assert [full_row(db_session, f) for f in fresh] == fresh_before


@pytest.mark.parametrize("status", ["uploaded", "processed", "partial", "rejected", "error"])
def test_stale_sweep_ignores_files_not_processing(db_session: Session, status: str) -> None:
    """An old heartbeat on a file that is not processing is not the stale sweeper's business."""
    file_id = processing_file(db_session, attempts=1, heartbeat_age=STALE + 5)
    db_session.execute(text("UPDATE upload_file SET status = :s WHERE file_id = :id"), {"s": status, "id": file_id})
    db_session.commit()
    before = full_row(db_session, file_id)
    assert stale(db_session, FakeEnqueue()) == {"reset": 0, "errored": 0, "enqueue_failed": 0}
    assert full_row(db_session, file_id) == before


def test_stale_reset_sets_uploaded_at_so_reconcile_does_not_double_enqueue(db_session: Session) -> None:
    """The reset stamps uploaded_at = now() (rev 1.3): an immediate reconcile sweep leaves the file alone."""
    file_id = processing_file(db_session, attempts=1, heartbeat_age=STALE + 5)
    before = full_row(db_session, file_id)
    first = FakeEnqueue()
    stale(db_session, first)
    assert first.file_ids == [str(file_id)]
    assert full_row(db_session, file_id)["uploaded_at"] > before["uploaded_at"]
    assert reconcile(db_session, FakeEnqueue()) == {"requeued": 0, "errored": 0, "enqueue_failed": 0}


def test_stale_reset_whose_enqueue_failed_is_picked_up_by_reconcile(db_session: Session) -> None:
    """A failed enqueue is counted, does not stop the others, and a reconcile sweep enqueues the file once it ages."""
    failed = processing_file(db_session, attempts=1, heartbeat_age=STALE + 5)
    other = processing_file(db_session, attempts=1, heartbeat_age=STALE + 5)
    first = FakeEnqueue(fail={failed})
    assert stale(db_session, first) == {"reset": 2, "errored": 0, "enqueue_failed": 1}
    assert first.file_ids == [str(other)]                     # one failure does not stop the others
    assert reconcile(db_session, FakeEnqueue()) == {"requeued": 0, "errored": 0, "enqueue_failed": 0}
    set_uploaded_age(db_session, failed, AGE + 5)              # RECONCILE_AFTER_SECONDS pass
    later = FakeEnqueue()
    reconcile(db_session, later)
    assert later.file_ids == [str(failed)]


# --- reconcile sweep ---

def test_reconcile_requeues_below_max_and_errors_at_max(db_session: Session) -> None:
    """Old uploaded files below MAX_ATTEMPTS are re-enqueued (uploaded_at reset); at MAX_ATTEMPTS they error."""
    requeue = [uploaded_file(db_session, attempts=a, uploaded_age=AGE + 5) for a in (0, MAX_ATTEMPTS - 1)]
    to_error = uploaded_file(db_session, attempts=MAX_ATTEMPTS, uploaded_age=AGE + 5)
    backdate_updated_at(db_session, to_error)
    error_before = full_row(db_session, to_error)
    untouched = [
        uploaded_file(db_session, attempts=0, uploaded_age=0),                        # too recent
        uploaded_file(db_session, attempts=0, uploaded_age=AGE + 5, status="uploading"),
        uploaded_file(db_session, attempts=1, uploaded_age=AGE + 5, status="processing"),
        uploaded_file(db_session, attempts=MAX_ATTEMPTS, uploaded_age=AGE + 5, status="processed"),
    ]
    untouched_before = [full_row(db_session, f) for f in untouched]
    requeue_before = [full_row(db_session, f) for f in requeue]
    enqueue = FakeEnqueue()

    assert reconcile(db_session, enqueue) == {"requeued": 2, "errored": 1, "enqueue_failed": 0}

    assert sorted(enqueue.file_ids) == sorted(str(f) for f in requeue)
    for file_id, before in zip(requeue, requeue_before):
        after = full_row(db_session, file_id)
        assert after["status"] == "uploaded" and after["uploaded_at"] > before["uploaded_at"]
        assert after["attempt_count"] == before["attempt_count"]
    error_row = full_row(db_session, to_error)
    assert error_row["status"] == "error" and error_row["status_message"] == ERROR_MESSAGE
    assert error_row["updated_at"] > error_before["updated_at"]
    assert str(to_error) not in enqueue.file_ids
    assert [full_row(db_session, f) for f in untouched] == untouched_before


def test_released_file_is_not_requeued_before_the_backoff(db_session: Session) -> None:
    """release resets uploaded_at (rev 1.2): reconcile leaves it alone until RECONCILE_AFTER_SECONDS pass."""
    file_id, token = claimed(db_session)
    release(db_session, file_id, token, reason="retry")
    assert reconcile(db_session, FakeEnqueue()) == {"requeued": 0, "errored": 0, "enqueue_failed": 0}
    set_uploaded_age(db_session, file_id, AGE + 5)
    later = FakeEnqueue()
    assert reconcile(db_session, later) == {"requeued": 1, "errored": 0, "enqueue_failed": 0}
    assert later.file_ids == [str(file_id)]


def test_reconcile_enqueue_failure_is_retried_by_a_later_sweep(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed enqueue is logged, does not stop the others, and the file is re-enqueued once it ages again."""
    failed = uploaded_file(db_session, attempts=0, uploaded_age=AGE + 5)
    other = uploaded_file(db_session, attempts=0, uploaded_age=AGE + 5)
    first = FakeEnqueue(fail={failed})
    with caplog.at_level(logging.WARNING, logger="app.services.sweeps"):
        assert reconcile(db_session, first) == {"requeued": 2, "errored": 0, "enqueue_failed": 1}
    assert first.file_ids == [str(other)]
    record = next(r for r in caplog.records if r.getMessage() == "enqueue_failed")
    assert record.fields["file_id"] == str(failed) and record.fields["sweep"] == "reconcile"

    assert reconcile(db_session, FakeEnqueue()) == {"requeued": 0, "errored": 0, "enqueue_failed": 0}   # uploaded_at just reset
    set_uploaded_age(db_session, failed, AGE + 5)
    later = FakeEnqueue()
    reconcile(db_session, later)
    assert later.file_ids == [str(failed)]


# --- enqueue happens only after the commit: committed rows, a second connection ---

def _read_committed(engine: Engine, file_id: uuid.UUID) -> dict[str, Any]:
    """Read the file from a separate connection, which sees only committed data."""
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT status, claim_token, uploaded_at FROM upload_file WHERE file_id = :id"),
                                 {"id": file_id}).mappings().one())


def test_stale_sweep_enqueues_only_after_commit(race_engine: Engine, committed_file: uuid.UUID) -> None:
    """At enqueue time another connection already sees the file committed as uploaded, token cleared."""
    with Session(race_engine) as db:
        db.execute(text("UPDATE upload_file SET status = 'processing', attempt_count = 1, "
                        "claim_token = gen_random_uuid(), heartbeat_at = now() - make_interval(secs => :age) "
                        "WHERE file_id = :id"), {"age": STALE + 5, "id": committed_file})
        db.commit()
    seen: list[dict[str, Any]] = []
    with Session(race_engine) as db:
        stale_sweep(db, stale_after_seconds=STALE, max_attempts=MAX_ATTEMPTS,
                    enqueue=lambda f, o: seen.append(_read_committed(race_engine, uuid.UUID(f))))
    assert len(seen) == 1
    assert seen[0]["status"] == "uploaded" and seen[0]["claim_token"] is None


def test_reconcile_sweep_enqueues_only_after_commit(race_engine: Engine, committed_file: uuid.UUID) -> None:
    """At enqueue time another connection already sees the new uploaded_at."""
    with Session(race_engine) as db:
        set_uploaded_age(db, committed_file, AGE + 5)
    before = _read_committed(race_engine, committed_file)["uploaded_at"]
    seen: list[dict[str, Any]] = []
    with Session(race_engine) as db:
        reconcile_sweep(db, reconcile_after_seconds=AGE, max_attempts=MAX_ATTEMPTS,
                        enqueue=lambda f, o: seen.append(_read_committed(race_engine, uuid.UUID(f))))
    assert len(seen) == 1 and seen[0]["uploaded_at"] > before
