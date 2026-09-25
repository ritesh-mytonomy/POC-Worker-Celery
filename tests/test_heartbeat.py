"""The Heartbeat thread in workers/ingest.py (design.md §8.6; R10.1)."""
import threading
import time

import pytest

from app.errors import ClaimSuperseded
from workers.clients import Transient
from workers.tasks import Heartbeat
from workers.clients import InternalAuthError, WorkerContractError


class FakeApi:
    """Counts heartbeats; answers with the scripted outcomes in order, then succeeds."""

    file_id = "file-1"

    def __init__(self, outcomes: list[BaseException | None] | None = None) -> None:
        """Script the outcomes of successive calls."""
        self.calls = 0
        self.outcomes = list(outcomes or [])
        self.lock = threading.Lock()

    def heartbeat(self) -> None:
        """Count, then raise the next scripted error if any."""
        with self.lock:
            self.calls += 1
            outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


def wait_for(condition, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    """Poll until condition() is true."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_beats_every_interval_until_stopped() -> None:
    """Beats repeatedly at the interval; stop() ends the thread and no beats follow."""
    api = FakeApi()
    beat = Heartbeat(api, every=0.05).start()
    wait_for(lambda: api.calls >= 4)
    beat.stop()
    after = api.calls
    time.sleep(0.2)
    assert after >= 4 and api.calls == after and not beat._thread.is_alive()


def test_first_beat_waits_one_interval() -> None:
    """The claim itself set heartbeat_at, so the first beat comes one interval later, not immediately."""
    api = FakeApi()
    beat = Heartbeat(api, every=0.5).start()
    time.sleep(0.1)
    beat.stop()
    assert api.calls == 0


def test_409_sets_superseded_and_stops() -> None:
    """ClaimSuperseded → superseded flag, thread ends, raise_if_superseded raises."""
    api = FakeApi([ClaimSuperseded("file-1")])
    beat = Heartbeat(api, every=0.02).start()
    wait_for(lambda: not beat._thread.is_alive())
    assert beat.superseded and api.calls == 1
    with pytest.raises(ClaimSuperseded):
        beat.raise_if_superseded()
    beat.stop()


def test_transient_failure_keeps_beating() -> None:
    """5xx or connection errors (Transient) are logged; beating continues and the task is not stopped."""
    api = FakeApi([Transient("503"), Transient("connection refused")])
    beat = Heartbeat(api, every=0.02).start()
    wait_for(lambda: beat.beats >= 2)
    beat.stop()
    assert api.calls >= 4 and not beat.superseded and beat.stop_reason is None
    beat.raise_if_superseded()


@pytest.mark.parametrize("error", [InternalAuthError(401, "invalid_internal_key", "bad key"),
                                   WorkerContractError(400, "validation_error", "bad"),
                                   WorkerContractError(404, "not_found", "gone")], ids=["401", "400", "404"])
def test_non_retryable_error_stops_beating_and_the_task(error: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    """401 or another non-retryable 4xx: one loud log line, the thread stops, and the task's next check raises it."""
    api = FakeApi([error])
    beat = Heartbeat(api, every=0.02).start()
    wait_for(lambda: not beat._thread.is_alive())
    beat.stop()
    assert api.calls == 1 and not beat.superseded
    assert [r.getMessage() for r in caplog.records if r.name == "workers.heartbeat"] == ["heartbeat_stopped"]
    with pytest.raises(type(error)):
        beat.raise_if_superseded()


def test_not_superseded_does_not_raise_and_stop_is_idempotent() -> None:
    """raise_if_superseded is silent while ownership holds; stop() twice is fine."""
    beat = Heartbeat(FakeApi(), every=1).start()
    beat.raise_if_superseded()
    beat.stop()
    beat.stop()
