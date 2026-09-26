"""Sweepers (design.md §8.7): recover stale and never-claimed files. Runs inside the API (R11.4, R11.5)."""
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import storage
from app.logging import get_logger
from app.tasks_client import enqueue_process_upload

log = get_logger(__name__)
Enqueue = Callable[[str, str], None]

_STALE_RESET_SQL = text("""
    UPDATE upload_file SET status='uploaded', claim_token=NULL, uploaded_at=now(), updated_at=now()
    WHERE status='processing' AND heartbeat_at < now() - make_interval(secs => :stale)
      AND attempt_count < :max RETURNING file_id, organization_id""")

_STALE_ERROR_SQL = text("""
    UPDATE upload_file SET status='error', claim_token=NULL, updated_at=now(),
           status_message='Processing did not complete after the maximum number of attempts'
    WHERE status='processing' AND heartbeat_at < now() - make_interval(secs => :stale)
      AND attempt_count >= :max RETURNING file_id""")

_RECONCILE_REQUEUE_SQL = text("""
    UPDATE upload_file SET uploaded_at = now()
    WHERE status='uploaded' AND uploaded_at < now() - make_interval(secs => :age)
      AND attempt_count < :max
    RETURNING file_id, organization_id""")

_RECONCILE_ERROR_SQL = text("""
    UPDATE upload_file SET status='error', claim_token=NULL, updated_at=now(),
           status_message='Processing did not complete after the maximum number of attempts'
    WHERE status='uploaded' AND uploaded_at < now() - make_interval(secs => :age)
      AND attempt_count >= :max RETURNING file_id""")


def _enqueue_committed(rows: Sequence[Any], enqueue: Enqueue, sweep: str) -> int:
    """Enqueue each committed row; log and count failures, which a later reconcile sweep recovers."""
    failed = 0
    for row in rows:
        try:
            enqueue(str(row.file_id), str(row.organization_id))
        except Exception as exc:
            # The row is committed as `uploaded` with uploaded_at = now(), so the reconcile sweep
            # re-enqueues it once RECONCILE_AFTER_SECONDS have passed.
            failed += 1
            log.warning("enqueue_failed", file_id=str(row.file_id), sweep=sweep, error=repr(exc))
    return failed


def stale_sweep(db: Session, *, stale_after_seconds: int, max_attempts: int,
                enqueue: Enqueue = enqueue_process_upload) -> dict[str, int]:
    """Recover files whose worker stopped heartbeating: reset (uploaded_at = now()) and re-enqueue, or error."""
    params = {"stale": stale_after_seconds, "max": max_attempts}
    reset = db.execute(_STALE_RESET_SQL, params).all()
    errored = db.execute(_STALE_ERROR_SQL, params).all()
    db.commit()                          # enqueue only after the state change is durable
    failed = _enqueue_committed(reset, enqueue, "stale")
    log.info("stale_sweep", reset=len(reset), errored=len(errored), enqueue_failed=failed,
             file_ids=[str(r.file_id) for r in [*reset, *errored]])
    return {"reset": len(reset), "errored": len(errored), "enqueue_failed": failed}


def reconcile_sweep(db: Session, *, reconcile_after_seconds: int, max_attempts: int,
                    enqueue: Enqueue = enqueue_process_upload) -> dict[str, int]:
    """Re-enqueue files confirmed but never claimed (lost Redis messages); error them when attempts are spent."""
    params = {"age": reconcile_after_seconds, "max": max_attempts}
    rows = db.execute(_RECONCILE_REQUEUE_SQL, params).all()
    errored = db.execute(_RECONCILE_ERROR_SQL, params).all()   # backstop for the fix in §8.1 (rev 1.1)
    db.commit()                          # enqueue only after the state change is durable
    failed = _enqueue_committed(rows, enqueue, "reconcile")
    log.info("reconcile_sweep", requeued=len(rows), errored=len(errored), enqueue_failed=failed,
             file_ids=[str(r.file_id) for r in [*rows, *errored]])
    return {"requeued": len(rows), "errored": len(errored), "enqueue_failed": failed}


# ── abandoned uploads (upload-ingest-merge U4.3; LLD M2.5's stale-upload sweeper) ──

_ABANDONED_SQL = text("""
    SELECT file_id, s3_key, upload_id FROM upload_file
    WHERE status IN ('staged', 'uploading') AND created_at < now() - make_interval(secs => :age)
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED""")

_CANCEL_ABANDONED_SQL = text("""
    UPDATE upload_file SET status = 'error', status_message = 'Upload was not completed', claim_token = NULL,
                           updated_at = now()
    WHERE file_id = :id AND status IN ('staged', 'uploading')""")

Abort = Callable[[str, str], None]


def abandoned_sweep(db: Session, *, abandon_after_seconds: int, abort: Abort | None = None) -> dict[str, int]:
    """Cancel uploads left staged or uploading too long: abort the multipart upload, then error the file.

    An abort that finds the upload already gone is not a failure (storage.abort_multipart treats it as done).
    An abort that fails for another reason leaves that file as it is for the next sweep; the bucket's 1-day
    AbortIncompleteMultipartUpload rule is the backstop. The rows stay locked until the one commit, so a racing
    complete waits and then sees the file cancelled; a racing sweep skips them.
    """
    abort = abort or storage.abort_multipart
    rows = db.execute(_ABANDONED_SQL, {"age": abandon_after_seconds}).all()
    cancelled, failed = [], []
    for row in rows:
        if row.upload_id:                # a /poc/seed row has no multipart upload to abort
            try:
                abort(row.s3_key, row.upload_id)
            except Exception as exc:     # S3 down, denied: try again next sweep
                failed.append(row.file_id)
                log.warning("abandoned_abort_failed", file_id=str(row.file_id), error=repr(exc))
                continue
        db.execute(_CANCEL_ABANDONED_SQL, {"id": row.file_id})
        cancelled.append(row.file_id)
    db.commit()
    log.info("abandoned_sweep", cancelled=len(cancelled), abort_failed=len(failed),
             file_ids=[str(f) for f in cancelled])
    return {"cancelled": len(cancelled), "abort_failed": len(failed)}
