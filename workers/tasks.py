"""The workers' Celery app and every task it runs (design.md §7, §8.1–§8.7).

Sections, in order: Celery app and config · Heartbeat · process_upload / process_document / process_archive ·
sweeper tasks. Task names are set explicitly and never change (the §6.3 message contract); each section keeps its
original logger name so log output is identical. Workers never open a database connection (R4.1).
"""
import hashlib
import os
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from celery import Celery, signals
from celery.exceptions import SoftTimeLimitExceeded

from app.config import get_settings
from app.constants import (
    QUEUE_INGEST, QUEUE_MAINTENANCE, TASK_PROCESS_UPLOAD, TASK_RECONCILE_SWEEP, TASK_STALE_SWEEP,
)
from app.errors import ClaimSuperseded
from app.logging import configure_logging, get_logger
from engine.file_checks import (
    UNREADABLE, Limits, Rejected, candidate_entries, detect_file_type, extract_streaming, folder_depth,
    inspect_archive, mismatch_reason,
)
from workers.clients import (
    BoundClient, FileClaim, FinalStatus, InternalClient, S3ConfigError, S3ObjectNotFound, S3Store, Transient,
    WorkerContractError,
)

# ============================================================================ Celery app and config

configure_logging()
_app_log = get_logger("workers.celery_app")


@signals.setup_logging.connect
def _keep_json_logging(**_: Any) -> None:
    """Stop Celery replacing our JSON logging with its own format."""
    configure_logging()


@signals.celeryd_init.connect
def _log_start_instead_of_banner(sender: str, instance: Any, options: dict[str, Any], **_: Any) -> None:
    """Suppress the plain-text startup banner and log its key facts as JSON."""
    instance.quiet = True
    _app_log.info("worker_starting", hostname=sender,
             queues=options.get("queues"), concurrency=options.get("concurrency"))


settings = get_settings()

app = Celery("clinsync", broker=settings.REDIS_URL)   # every task below is defined in this module

app.conf.update(
    task_acks_late=True,                 # R9.1 — ack after the body, not on receipt
    task_reject_on_worker_lost=True,     # R9.2 — killed child → message back on the queue
    worker_prefetch_multiplier=1,        # R9.3 — one message per process
    task_ignore_result=True,             # state is in PostgreSQL, not a result backend
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.TASK_TIME_LIMIT,
    broker_transport_options={"visibility_timeout": settings.VISIBILITY_TIMEOUT},  # R9.4
    broker_connection_retry_on_startup=True,
    task_default_queue=QUEUE_INGEST,
    task_routes={
        "workers.ingest.*":   {"queue": QUEUE_INGEST},
        "workers.sweepers.*": {"queue": QUEUE_MAINTENANCE},
    },
    # Each sweep message expires after one interval: if the consumer is stuck (or beat runs separately, as in
    # AWS), stale sweeps are discarded on receipt instead of all running at once afterwards (task 11.1).
    beat_schedule={
        "stale-sweep":     {"task": TASK_STALE_SWEEP, "schedule": settings.SWEEP_INTERVAL_SECONDS,
                            "options": {"expires": settings.SWEEP_INTERVAL_SECONDS}},
        "reconcile-sweep": {"task": TASK_RECONCILE_SWEEP, "schedule": settings.SWEEP_INTERVAL_SECONDS,
                            "options": {"expires": settings.SWEEP_INTERVAL_SECONDS}},
    },
    beat_schedule_filename="/tmp/celerybeat-schedule",   # /srv is not writable by the app user
)
try:
    settings.validate()                  # R9.5 — refuse to start on a bad combination
except ValueError as exc:
    _app_log.error("invalid_configuration", error=str(exc))
    raise SystemExit(1) from exc


# ============================================================================ Heartbeat
# design.md §8.6; R10.1: proves liveness while a file is processed.

_heartbeat_log = get_logger("workers.heartbeat")


class Beats(Protocol):
    """Anything with a fenced heartbeat() — the token-bound internal client in practice."""

    file_id: object

    def heartbeat(self) -> None:
        """Refresh heartbeat_at; raise ClaimSuperseded on 409."""


class Heartbeat:
    """Daemon thread calling heartbeat() every `every` seconds until stopped, superseded or refused."""

    def __init__(self, api: Beats, every: float) -> None:
        """Prepare, but do not start, the thread."""
        self._api, self._every = api, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{api.file_id}", daemon=True)
        self.superseded = False
        self.stop_reason: BaseException | None = None
        self.beats = 0

    def start(self) -> "Heartbeat":
        """Start beating; return self so `beat = Heartbeat(...).start()` reads naturally."""
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop and wait for the thread (bounded by one request timeout)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)

    def raise_if_superseded(self) -> None:
        """Raise if beating stopped for good, so the task stops at its next check (writes are fenced anyway).

        409 → ClaimSuperseded; 401 or another non-retryable 4xx → that same error (a configuration or worker bug).
        """
        if self.superseded:
            raise ClaimSuperseded(self._api.file_id)
        if self.stop_reason is not None:
            raise self.stop_reason

    def _run(self) -> None:
        """Beat until stopped. 409 or a non-retryable error stops for good; Transient keeps beating."""
        while not self._stop.wait(self._every):
            try:
                self._api.heartbeat()
                self.beats += 1
            except ClaimSuperseded as exc:
                self.superseded, self.stop_reason = True, exc
                _heartbeat_log.warning("heartbeat_superseded", file_id=str(self._api.file_id), beats=self.beats)
                return
            except Transient as exc:                                # 5xx, refused, timeout: next beat may work
                _heartbeat_log.warning("heartbeat_failed", file_id=str(self._api.file_id), error=repr(exc))
            except Exception as exc:                                # 401, other 4xx, bugs: stop the task too
                self.stop_reason = exc
                _heartbeat_log.exception("heartbeat_stopped", file_id=str(self._api.file_id), error=repr(exc))
                return


# ============================================================================ process_upload / process_document / process_archive

log = get_logger("workers.ingest")
LIMITS = Limits.from_settings(settings)          # engine/ takes limits as arguments, not settings
# R11.1. OSError and MemoryError are environment problems (a full disk) and stay retryable (rev 1.3). The
# deliberately non-retryable errors are caught before this tuple is reached (see process_upload).
RETRYABLE = (Transient, SoftTimeLimitExceeded, OSError, MemoryError)
NON_RETRYABLE = (S3ConfigError, WorkerContractError)          # WorkerContractError includes InternalAuthError
_clients: tuple[int, InternalClient, S3Store] | None = None


def _per_process() -> tuple[InternalClient, S3Store]:
    """This process's Internal API client and S3 store, created after fork so prefork children share nothing."""
    global _clients
    if _clients is None or _clients[0] != os.getpid():
        _clients = (os.getpid(), InternalClient.from_settings(settings), S3Store.from_settings(settings))
    return _clients[1], _clients[2]


def internal() -> InternalClient:
    """This process's Internal API client."""
    return _per_process()[0]


def s3() -> S3Store:
    """This process's S3 store."""
    return _per_process()[1]


def staging_prefix(claim: FileClaim) -> str:
    """Every staged object of one file lives under this prefix (design.md §8.3)."""
    return f"ClinSync/staging/{claim.organization_id}/{claim.batch_id}/{claim.file_id}/"


def staging_key(claim: FileClaim, index: int, name: str) -> str:
    """Deterministic staging key: a replay writes the same key, so no orphans and no duplicates (design.md §8.3)."""
    return f"{staging_prefix(claim)}{index:04d}_{name}"


def process_document(api: BoundClient, claim: FileClaim, path: Path, limits: Limits, store: S3Store,
                     content_hash: str) -> FinalStatus:
    """Verify a single document by content and stage it (R5.1–R5.4); raise Rejected on a mismatch.

    content_hash is the SHA-256 taken while downloading (U5.2); the staged copy is the same bytes.
    """
    d = detect_file_type(path, limits)                           # R5.1
    api.progress(detected_type=d.type)
    if d.type != claim.file_ext:                                 # R5.3
        raise Rejected(mismatch_reason(claim.file_ext, d))
    key = staging_key(claim, index=0, name=claim.file_name)
    store.copy(claim.s3_key, key)                                # server-side copy
    api.upsert_candidate(entry_name=None, entry_index=None, file_name=claim.file_name,
                         file_ext=claim.file_ext, size_bytes=path.stat().st_size,
                         s3_key=key, status="processed", content_hash=content_hash)   # R5.4, U5.2
    log.info("staged", file_id=str(claim.file_id), attempt=claim.attempt_count, s3_key=key)
    return FinalStatus("processed")


def poc_delay() -> None:
    """POC only: pause ENTRY_DELAY_SECONDS after each entry so concurrency and kills are observable."""
    if settings.ENTRY_DELAY_SECONDS > 0:
        time.sleep(settings.ENTRY_DELAY_SECONDS)


def _process_entry(api: BoundClient, claim: FileClaim, zf: zipfile.ZipFile, e: zipfile.ZipInfo, i: int,
                   limits: Limits, store: S3Store) -> bool:
    """Handle one archive entry (R7.2–R7.4); return True if it was rejected. Archive-level problems raise Rejected."""
    name = PurePosixPath(e.filename).name
    ext = PurePosixPath(name).suffix.lower().lstrip(".")

    def reject(reason: str) -> bool:
        api.upsert_candidate(entry_name=e.filename, entry_index=i, file_name=name, file_ext=ext[:10],
                             status="rejected", reject_reason=reason)
        log.info("entry_rejected", file_id=str(claim.file_id), attempt=claim.attempt_count, entry_index=i,
                 entry=e.filename, reason=reason)
        return True

    if folder_depth(e.filename) > limits.max_folder_depth:                # U5.4 — entry-level; the archive continues
        return reject("Folders nested too deeply — at most one subfolder is supported")
    if not ext:
        return reject("Files without an extension are not supported")
    if ext not in limits.allowed_entry_ext:                               # R7.2 — not stored
        return reject(f".{ext} is not supported")
    digest = hashlib.sha256()                                             # U5.2 — hashed while extracting
    tmp = extract_streaming(zf, e, digest=digest)                         # R6.7, R6.8 — outer CRC → archive-level
    try:
        d = detect_file_type(tmp, limits)                                 # inner problems → entry-level
        if d.type != ext:                                                 # R7.3 — not stored
            return reject(mismatch_reason(ext, d))
        key = staging_key(claim, index=i, name=name)
        store.upload(tmp, key)
        api.upsert_candidate(entry_name=e.filename, entry_index=i, file_name=name, file_ext=ext,
                             size_bytes=e.file_size, s3_key=key, status="processed",
                             content_hash=digest.hexdigest())                          # R7.4, U5.2
        log.info("entry_staged", file_id=str(claim.file_id), attempt=claim.attempt_count, entry_index=i,
                 entry=e.filename, s3_key=key)
        return False
    finally:
        tmp.unlink(missing_ok=True)                                       # every path, not only success (rev 1.3)


def _open_archive(path: Path) -> zipfile.ZipFile:
    """Open the outer zip; any format error is Rejected("Archive is damaged") (design.md §11)."""
    try:
        return zipfile.ZipFile(path)
    except UNREADABLE as exc:
        raise Rejected("Archive is damaged") from exc


def process_archive(api: BoundClient, claim: FileClaim, path: Path, beat: Heartbeat, limits: Limits,
                    store: S3Store) -> FinalStatus:
    """Verify an archive and stage its entries, resuming after entries_done (R7, R8.1, R8.2)."""
    outer = detect_file_type(path, limits)                       # is it really an archive?
    api.progress(detected_type=outer.type)
    if outer.type == "corrupt":
        raise Rejected("Archive is damaged")                     # design.md §11 — BadZipFile on open
    if outer.type != "zip":
        raise Rejected(mismatch_reason("zip", outer))            # e.g. a .docx renamed .zip
    try:
        with _open_archive(path) as zf:
            guarded = inspect_archive(zf, limits)                # R6 — archive-level, EVERY entry, hidden ones too
            entries = candidate_entries(guarded)                 # U5.3 — only then drop __MACOSX/ and dot-files
            api.progress(entries_total=len(entries))             # R7.1 — counted, and numbered, after the filter
            rejected_any = False
            for i, e in enumerate(entries):                      # R7.5 — directory order
                if i < claim.entries_done:                       # R8.2 — resume
                    continue
                beat.raise_if_superseded()                       # stop fast if ownership lost
                rejected_any |= _process_entry(api, claim, zf, e, i, limits, store)
                api.progress(entries_done=i + 1)                 # R8.1 — after, never before
                poc_delay()                                      # ENTRY_DELAY_SECONDS — POC only
    except Rejected:                                             # §8.5a — archive-level only
        store.delete_prefix(staging_prefix(claim))               # delete FIRST, then finish (in process_upload)
        raise
    return FinalStatus("partial" if rejected_any else "processed")   # R7.6


@app.task(bind=True, max_retries=None, name=TASK_PROCESS_UPLOAD)  # attempt_count, not Celery's counter, bounds attempts
def process_upload(self: Any, file_id: str, organization_id: str) -> None:
    """Verify an uploaded file and stage whatever it contains."""
    task_id = self.request.id
    claim = internal().claim(file_id)
    if claim is None:
        log.info("claim_lost", file_id=file_id, task_id=task_id)             # R3.4 — ack and exit, no download
        return
    ctx = {"file_id": file_id, "batch_id": str(claim.batch_id), "task_id": task_id, "attempt": claim.attempt_count}
    log.info("claim_ok", **ctx, claim_token=str(claim.claim_token))
    api = internal().with_token(file_id, claim.claim_token)
    store = s3()
    beat = Heartbeat(api, every=settings.HEARTBEAT_SECONDS).start()           # R10.1
    local: Path | None = None
    try:
        digest = hashlib.sha256()                                             # U5.2 — hashed while downloading
        local = store.download_to_tmp(claim.s3_key, digest=digest)            # heartbeat keeps running
        beat.raise_if_superseded()
        if claim.is_archive:
            final = process_archive(api, claim, local, beat, LIMITS, store)
        else:
            final = process_document(api, claim, local, LIMITS, store, digest.hexdigest())
        api.finish(final)                                                     # R10.3
        store.delete_quietly(claim.s3_key)                                    # R10.2 — lifecycle rule is the backstop
        log.info("finished", **ctx, status=final.status)
    except Rejected as r:                                                     # R11.2 — never retried
        api.finish(FinalStatus("rejected", str(r)))
        store.delete_quietly(claim.s3_key)
        log.info("finished", **ctx, status="rejected", reason=str(r))
    except S3ObjectNotFound:                                                  # deterministic: no retry
        api.finish(FinalStatus("error", "Uploaded object not found"))
        log.warning("finished", **ctx, status="error", reason="Uploaded object not found")
    except ClaimSuperseded:
        log.warning("claim_superseded", **ctx)                                # another worker owns it now
    except NON_RETRYABLE as exc:                                              # configuration or worker bug
        log.exception("task_failed", **ctx, retryable=False, error=repr(exc))
        raise                                                                 # the stale sweeper recovers the file
    except RETRYABLE as e:                                                    # R11.1
        beat.stop()                                                           # no heartbeat after handing it back
        if claim.attempt_count >= settings.MAX_ATTEMPTS:                      # R11.3 — last attempt: fail, don't release
            message = f"Could not be processed after {claim.attempt_count} attempts: {e!r}"
            api.finish(FinalStatus("error", message))
            store.delete_quietly(claim.s3_key)
            log.warning("finished", **ctx, status="error", reason=message)
            return
        try:
            api.release(reason=repr(e))                                       # resets uploaded_at (rev 1.2)
        except ClaimSuperseded:
            log.warning("claim_superseded", **ctx, during="release")
            return
        except Transient as exc:                                              # best effort (design.md §11)
            log.warning("release_failed", **ctx, error=repr(exc))
        countdown = 2 ** claim.attempt_count * 5
        log.warning("retrying", **ctx, countdown=countdown, error=repr(e))
        raise self.retry(exc=e, countdown=countdown)
    except Exception as exc:                                                  # a bug: log loudly, never swallow
        log.exception("task_failed", **ctx, retryable=False, error=repr(exc))
        raise
    finally:
        beat.stop()
        if local is not None:
            local.unlink(missing_ok=True)


# ============================================================================ sweeper tasks
# design.md §8.7; R10.4, R11.4, R11.5: run by beat in worker-maint, executed by the API.

_sweep_log = get_logger("workers.sweepers")


def _sweep(kind: str, task_id: str | None) -> dict[str, int] | None:
    """Run one sweep; log its counts, or log the failure and let the next beat tick try again (no Celery retry)."""
    try:
        counts = internal().sweep(kind)
    except Exception as exc:                          # API down, wrong key, bug: never retried, never piled up
        _sweep_log.warning("sweep_failed", sweep=kind, task_id=task_id, error=repr(exc))
        return None
    _sweep_log.info(f"{kind}_sweep", task_id=task_id, **counts)
    return counts


@app.task(bind=True, name=TASK_STALE_SWEEP, ignore_result=True)
def run_stale_sweep(self: Any) -> dict[str, int] | None:
    """Reset or error files whose worker stopped heartbeating (R11.4)."""
    return _sweep("stale", self.request.id)


@app.task(bind=True, name=TASK_RECONCILE_SWEEP, ignore_result=True)
def run_reconcile_sweep(self: Any) -> dict[str, int] | None:
    """Re-enqueue confirmed files nobody claimed, or error them when attempts are spent (R11.5)."""
    return _sweep("reconcile", self.request.id)
