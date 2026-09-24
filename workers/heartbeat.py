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
    """Daemon thread calling heartbeat() every `every` seconds until stopped or superseded."""

    def __init__(self, api: Beats, every: float) -> None:
        """Prepare, but do not start, the thread."""
        self._api, self._every = api, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{api.file_id}", daemon=True)
        self.superseded = False
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
        """Raise ClaimSuperseded if a heartbeat was refused; lets the main loop stop sooner (writes are fenced anyway)."""
        if self.superseded:
            raise ClaimSuperseded(self._api.file_id)

    def _run(self) -> None:
        """Beat until stopped; stop for good on 409 or an unexpected error."""
        while not self._stop.wait(self._every):
            try:
                self._api.heartbeat()
                self.beats += 1
            except ClaimSuperseded:
                self.superseded = True
                log.warning("heartbeat_superseded", file_id=str(self._api.file_id), beats=self.beats)
                return
            except Transient as exc:                                # the next beat may get through
                log.warning("heartbeat_failed", file_id=str(self._api.file_id), error=repr(exc))
            except Exception as exc:                                # contract/auth error: once, loudly
                log.exception("heartbeat_stopped", file_id=str(self._api.file_id), error=repr(exc))
                return
