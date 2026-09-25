"""Release and retry (task 10.2; design.md §8.1; R11.1, R11.3)."""
import uuid
from pathlib import Path
from typing import Any

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded

from app.errors import ClaimSuperseded
from engine.file_checks import Rejected
from workers.clients import Transient
from tests.test_worker_ingest import FakeBound, FakeInternal, FakeStore, Recorder, a_claim, finals
from tests.test_worker_ingest import ingest  # noqa: F401  (fixture)
from workers.clients import FileClaim, FinalStatus, InternalAuthError, WorkerContractError
from workers.clients import S3ConfigError, S3ObjectNotFound


def attempt(n: int) -> FileClaim:
    """A claim on attempt n."""
    base = a_claim()
    return FileClaim(**{**base.__dict__, "attempt_count": n})


def run_task(ingest: Any, monkeypatch: pytest.MonkeyPatch, claim: FileClaim, store: FakeStore, rec: Recorder,  # noqa: F811
             bound: type[FakeBound] = FakeBound) -> list[dict[str, Any]]:
    """Run the task eagerly; return the self.retry() calls it made."""
    retries: list[dict[str, Any]] = []

    def fake_retry(exc: BaseException | None = None, countdown: int | None = None, **_: Any) -> Retry:
        retries.append({"exc": exc, "countdown": countdown})
        return Retry(exc=exc, when=countdown)

    class Internal(FakeInternal):
        def with_token(self, file_id: str, token: uuid.UUID) -> FakeBound:
            self.rec.calls.append(("with_token", file_id, token))
            return bound(self.rec, False)

    monkeypatch.setattr(ingest, "internal", lambda: Internal(rec, claim))
    monkeypatch.setattr(ingest, "s3", lambda: store)
    monkeypatch.setattr(ingest.process_upload, "retry", fake_retry)
    ingest.process_upload.apply(kwargs={"file_id": str(claim.file_id), "organization_id": "o"})
    return retries


RETRYABLE_ERRORS = [Transient("S3 unreachable"), SoftTimeLimitExceeded(), OSError(28, "No space left on device"),
                    MemoryError()]
IDS = ["Transient", "SoftTimeLimitExceeded", "OSError", "MemoryError"]


@pytest.mark.parametrize("error", RETRYABLE_ERRORS, ids=IDS)
@pytest.mark.parametrize(("n", "countdown"), [(1, 10), (2, 20)])
def test_retryable_error_before_the_last_attempt_releases_then_retries(
    ingest: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException, n: int, countdown: int  # noqa: F811
) -> None:
    """Attempts 1 and 2: release (with the reason), then self.retry(countdown = 2 ** attempt * 5); no finish."""
    rec = Recorder()
    retries = run_task(ingest, monkeypatch, attempt(n), FakeStore(rec, None, error), rec)
    assert [c for c in rec.calls if c[0] == "release"] == [("release", repr(error))]
    assert retries == [{"exc": error, "countdown": countdown}] and finals(rec) == []


@pytest.mark.parametrize("error", RETRYABLE_ERRORS, ids=IDS)
def test_retryable_error_on_the_last_attempt_finishes_error_without_releasing(
    ingest: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException  # noqa: F811
) -> None:
    """Attempt MAX_ATTEMPTS: finish(error, 'after 3 attempts'), delete incoming; never release, never retry (R11.3)."""
    rec = Recorder()
    claim = attempt(3)
    retries = run_task(ingest, monkeypatch, claim, FakeStore(rec, None, error), rec)
    (final,) = finals(rec)
    assert final.status == "error" and final.message.startswith("Could not be processed after 3 attempts: ")
    assert "release" not in rec.names() and retries == []
    assert rec.names()[-1] == "delete" and ("delete", claim.s3_key) in rec.calls


def test_heartbeat_is_stopped_before_release(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """No heartbeat can land after the file has been handed back."""
    import threading
    alive_at_release: list[bool] = []

    class Bound(FakeBound):
        def release(self, reason: str) -> None:
            alive_at_release.append(any(t.name.startswith("heartbeat-") for t in threading.enumerate()))
            super().release(reason)

    rec = Recorder()
    run_task(ingest, monkeypatch, attempt(1), FakeStore(rec, None, Transient("x")), rec, bound=Bound)
    assert alive_at_release == [False]


def test_release_failing_transiently_still_retries(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Release is best effort: if the API is down too, log and retry anyway (the sweeper covers the rest)."""
    class Bound(FakeBound):
        def release(self, reason: str) -> None:
            super().release(reason)
            raise Transient("API 503")

    rec = Recorder()
    assert run_task(ingest, monkeypatch, attempt(1), FakeStore(rec, None, Transient("S3")), rec, bound=Bound) != []


def test_release_superseded_does_not_retry(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """If someone else owns the file by the time we release, stop: no retry."""
    class Bound(FakeBound):
        def release(self, reason: str) -> None:
            raise ClaimSuperseded("f")

    rec = Recorder()
    assert run_task(ingest, monkeypatch, attempt(1), FakeStore(rec, None, Transient("S3")), rec, bound=Bound) == []


def test_finish_error_failing_propagates(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """If even finish(error) fails on the last attempt, the task fails; the stale sweeper errors the file."""
    class Bound(FakeBound):
        def finish(self, final: FinalStatus) -> None:
            raise Transient("API down")

    rec = Recorder()
    retries = run_task(ingest, monkeypatch, attempt(3), FakeStore(rec, None, Transient("S3")), rec, bound=Bound)
    assert retries == [] and "release" not in rec.names()


@pytest.mark.parametrize("error", [S3ConfigError("access denied"), InternalAuthError(401, "invalid_internal_key", "x"),
                                   WorkerContractError(400, "validation_error", "x"), KeyError("bug")],
                         ids=["S3ConfigError", "InternalAuthError", "WorkerContractError", "a bug"])
def test_non_retryable_errors_are_logged_and_raised_without_release(
    ingest: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    """Configuration errors, worker contract errors and bugs: task_failed logged, no release, no retry, no finish."""
    import logging
    rec = Recorder()
    with caplog.at_level(logging.ERROR, logger="workers.ingest"):
        retries = run_task(ingest, monkeypatch, attempt(1), FakeStore(rec, None, error), rec)
    assert retries == [] and "release" not in rec.names() and finals(rec) == []
    assert any(r.getMessage() == "task_failed" for r in caplog.records)


@pytest.mark.parametrize("cls", [S3ConfigError, InternalAuthError, WorkerContractError, S3ObjectNotFound, Rejected,
                                 ClaimSuperseded])
def test_non_retryable_classes_do_not_subclass_retryable_ones(cls: type) -> None:
    """None of the deliberately non-retryable errors may be caught by the RETRYABLE clause by inheritance."""
    for retryable in (OSError, MemoryError, Transient, SoftTimeLimitExceeded):
        assert not issubclass(cls, retryable), f"{cls.__name__} subclasses {retryable.__name__}"


def test_retryable_tuple(ingest: Any) -> None:  # noqa: F811
    """RETRYABLE is exactly the §8.1 pair plus the rev 1.3 environment errors."""
    assert set(ingest.RETRYABLE) == {Transient, SoftTimeLimitExceeded, OSError, MemoryError}
