"""Heartbeat thread (design.md §8.6; R10.1): proves liveness while a file is processed."""
import threading
from typing import Protocol

from app.errors import ClaimSuperseded
from app.logging import get_logger
from engine.errors import Transient

log = get_logger(__name__)


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
                log.warning("heartbeat_superseded", file_id=str(self._api.file_id), beats=self.beats)
                return
            except Transient as exc:                                # 5xx, refused, timeout: next beat may work
                log.warning("heartbeat_failed", file_id=str(self._api.file_id), error=repr(exc))
            except Exception as exc:                                # 401, other 4xx, bugs: stop the task too
                self.stop_reason = exc
                log.exception("heartbeat_stopped", file_id=str(self._api.file_id), error=repr(exc))
                return
