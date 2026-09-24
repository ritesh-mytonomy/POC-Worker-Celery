"""process_upload skeleton (task 7.1): claim, then finish processed; claim_lost when not claimable (R3.1, R3.4)."""
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import get_settings
from app.errors import ClaimSuperseded
from workers.internal_client import FileClaim, FinalStatus


class FakeBound:
    """Records the writes of one claimed file."""

    file_id = "f"

    def __init__(self, calls: list[Any], fail_finish: bool) -> None:
        """Share the call log; optionally lose ownership at finish."""
        self.calls, self.fail_finish = calls, fail_finish

    def heartbeat(self) -> None:
        """Record a heartbeat."""
        self.calls.append(("heartbeat",))

    def finish(self, final: FinalStatus) -> None:
        """Record finish, or raise ClaimSuperseded."""
        self.calls.append(("finish", final))
        if self.fail_finish:
            raise ClaimSuperseded("f")


class FakeInternal:
    """A scripted Internal API: claim returns `claim`; writes are recorded."""

    def __init__(self, claim: FileClaim | None, fail_finish: bool = False) -> None:
        """Script the claim result."""
        self.claim_result, self.fail_finish, self.calls = claim, fail_finish, []

    def claim(self, file_id: str) -> FileClaim | None:
        """Record and answer the claim."""
        self.calls.append(("claim", file_id))
        return self.claim_result

    def with_token(self, file_id: str, token: uuid.UUID) -> FakeBound:
        """Record the token binding."""
        self.calls.append(("with_token", file_id, token))
        return FakeBound(self.calls, self.fail_finish)


def a_claim(file_id: str) -> FileClaim:
    """A FileClaim for file_id."""
    return FileClaim(file_id=uuid.UUID(file_id), organization_id=uuid.uuid4(), batch_id=uuid.uuid4(),
                     s3_key="ClinSync/incoming/x/valid.docx", file_name="valid.docx", file_ext="docx",
                     is_archive=False, entries_done=0, attempt_count=1, claim_token=uuid.uuid4())


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The workers.ingest module with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.ingest as module
    yield module
    get_settings.cache_clear()


def run(ingest: Any, fake: FakeInternal, monkeypatch: pytest.MonkeyPatch, file_id: str) -> None:
    """Run the task body eagerly against the fake API."""
    monkeypatch.setattr(ingest, "internal", lambda: fake)
    ingest.process_upload.apply(kwargs={"file_id": file_id, "organization_id": str(uuid.uuid4())}).get()


def test_claim_ok_then_finish_processed(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                        caplog: pytest.LogCaptureFixture) -> None:
    """A successful claim logs claim_ok with R15.1 fields and finishes processed with the claim token."""
    file_id = str(uuid.uuid4())
    claim = a_claim(file_id)
    fake = FakeInternal(claim)
    with caplog.at_level(logging.INFO, logger="workers.ingest"):
        run(ingest, fake, monkeypatch, file_id)
    assert fake.calls == [("claim", file_id), ("with_token", file_id, claim.claim_token),
                          ("finish", FinalStatus("processed"))]
    ok = next(r for r in caplog.records if r.getMessage() == "claim_ok")
    assert ok.fields["file_id"] == file_id and ok.fields["attempt"] == 1
    assert ok.fields["claim_token"] == str(claim.claim_token) and ok.fields["task_id"]


def test_not_claimable_logs_claim_lost_and_does_nothing_else(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    """claim → None: log claim_lost and return — no token, no writes, no download (R3.4)."""
    file_id = str(uuid.uuid4())
    fake = FakeInternal(None)
    with caplog.at_level(logging.INFO, logger="workers.ingest"):
        run(ingest, fake, monkeypatch, file_id)
    assert fake.calls == [("claim", file_id)]
    assert [r.getMessage() for r in caplog.records if r.name == "workers.ingest"] == ["claim_lost"]


def test_superseded_at_finish_is_logged_not_raised(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    """Losing ownership before finish → warning claim_superseded, task completes (design.md §8.1)."""
    file_id = str(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="workers.ingest"):
        run(ingest, FakeInternal(a_claim(file_id), fail_finish=True), monkeypatch, file_id)
    assert any(r.getMessage() == "claim_superseded" for r in caplog.records)


def test_task_is_registered_under_the_queue_contract_name(ingest: Any) -> None:
    """The worker app registers workers.ingest.process_upload (design.md §6.3), routed to clinsync.ingest."""
    from workers.celery_app import app
    assert "workers.ingest.process_upload" in app.tasks
    assert app.amqp.router.route({}, "workers.ingest.process_upload")["queue"].name == "clinsync.ingest"


def test_heartbeat_thread_is_stopped_when_the_task_ends(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The task starts a heartbeat after claim and stops it in finally — no thread outlives the task."""
    import threading
    file_id = str(uuid.uuid4())
    run(ingest, FakeInternal(a_claim(file_id)), monkeypatch, file_id)
    run(ingest, FakeInternal(a_claim(file_id), fail_finish=True), monkeypatch, file_id)
    assert not [t for t in threading.enumerate() if t.name.startswith("heartbeat-")]
