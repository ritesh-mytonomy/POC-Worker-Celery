"""process_upload (design.md §8.1), process_document (§8.2) and process_archive (§8.3)."""
import os
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from celery.exceptions import SoftTimeLimitExceeded

from app.constants import TASK_PROCESS_UPLOAD
from app.errors import ClaimSuperseded
from app.logging import get_logger
from engine.archive import UNREADABLE, extract_streaming, inspect_archive
from engine.errors import Rejected, Transient
from engine.file_signature import detect_file_type, mismatch_reason
from engine.limits import Limits
from workers.celery_app import app, settings
from workers.internal_client import BoundClient, FileClaim, FinalStatus, InternalClient, WorkerContractError
from workers.s3 import S3ConfigError, S3ObjectNotFound, S3Store

log = get_logger(__name__)
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


def process_document(api: BoundClient, claim: FileClaim, path: Path, limits: Limits, store: S3Store) -> FinalStatus:
    """Verify a single document by content and stage it (R5.1–R5.4); raise Rejected on a mismatch."""
    d = detect_file_type(path, limits)                           # R5.1
    api.progress(detected_type=d.type)
    if d.type != claim.file_ext:                                 # R5.3
        raise Rejected(mismatch_reason(claim.file_ext, d))
    key = staging_key(claim, index=0, name=claim.file_name)
    store.copy(claim.s3_key, key)                                # server-side copy
    api.upsert_candidate(entry_name=None, entry_index=None, file_name=claim.file_name,
                         file_ext=claim.file_ext, size_bytes=path.stat().st_size,
                         s3_key=key, status="processed")        # R5.4
    log.info("staged", file_id=str(claim.file_id), attempt=claim.attempt_count, s3_key=key)
    return FinalStatus("processed")


# --- Heartbeat thread (design.md §8.6; R10.1): proves liveness while a file is processed. ---
# Its own logger name is kept, so its log lines are unchanged since it moved here from workers/heartbeat.py.
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

    if not ext:
        return reject("Files without an extension are not supported")
    if ext not in limits.allowed_entry_ext:                               # R7.2 — not stored
        return reject(f".{ext} is not supported")
    tmp = extract_streaming(zf, e)                                        # R6.7, R6.8 — outer CRC → archive-level
    try:
        d = detect_file_type(tmp, limits)                                 # inner problems → entry-level
        if d.type != ext:                                                 # R7.3 — not stored
            return reject(mismatch_reason(ext, d))
        key = staging_key(claim, index=i, name=name)
        store.upload(tmp, key)
        api.upsert_candidate(entry_name=e.filename, entry_index=i, file_name=name, file_ext=ext,
                             size_bytes=e.file_size, s3_key=key, status="processed")   # R7.4
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
            entries = inspect_archive(zf, limits)                # R6 — archive-level
            api.progress(entries_total=len(entries))             # R7.1
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
        local = store.download_to_tmp(claim.s3_key)                           # heartbeat keeps running
        beat.raise_if_superseded()
        if claim.is_archive:
            final = process_archive(api, claim, local, beat, LIMITS, store)
        else:
            final = process_document(api, claim, local, LIMITS, store)
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
