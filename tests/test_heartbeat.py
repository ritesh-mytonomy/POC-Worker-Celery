"""workers/heartbeat.py (design.md §8.6; R10.1)."""
import threading
import time

import pytest

from app.errors import ClaimSuperseded
from engine.errors import Transient
from workers.heartbeat import Heartbeat
from workers.internal_client import WorkerContractError


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
    """A Transient error (API briefly down) is logged and the next beat is attempted."""
    api = FakeApi([Transient("503"), Transient("reset")])
    beat = Heartbeat(api, every=0.02).start()
    wait_for(lambda: beat.beats >= 2)
    beat.stop()
    assert api.calls >= 4 and not beat.superseded


def test_unexpected_error_stops_once_without_dying_silently(caplog: pytest.LogCaptureFixture) -> None:
    """A contract/auth error stops the thread after one loud log line (no spam, no silent death)."""
    api = FakeApi([WorkerContractError(401, "invalid_internal_key", "bad key")])
    beat = Heartbeat(api, every=0.02).start()
    wait_for(lambda: not beat._thread.is_alive())
    beat.stop()
    assert api.calls == 1 and not beat.superseded
    assert [r.getMessage() for r in caplog.records if r.name == "workers.heartbeat"] == ["heartbeat_stopped"]


def test_not_superseded_does_not_raise_and_stop_is_idempotent() -> None:
    """raise_if_superseded is silent while ownership holds; stop() twice is fine."""
    beat = Heartbeat(FakeApi(), every=1).start()
    beat.raise_if_superseded()
    beat.stop()
    beat.stop()
