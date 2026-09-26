"""Sweeper tasks (task 11.1): log counts on success; on any failure log and return — no Celery retry."""
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.config import get_settings
from workers.clients import Transient
from workers.clients import InternalAuthError, InternalClient


@pytest.fixture
def sweepers(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """workers.tasks (the sweeper tasks) with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.tasks as module
    yield module
    get_settings.cache_clear()


class FakeClient:
    """Answers sweeps with counts, or raises."""

    def __init__(self, error: BaseException | None = None) -> None:
        """Script the outcome."""
        self.error, self.calls = error, []

    def sweep(self, kind: str) -> dict[str, int]:
        """Record and answer."""
        self.calls.append(kind)
        if self.error:
            raise self.error
        return {"stale": {"reset": 1, "errored": 0, "enqueue_failed": 0},
                "reconcile": {"requeued": 2, "errored": 1, "enqueue_failed": 0},
                "abandoned": {"cancelled": 3, "abort_failed": 1}}[kind]


@pytest.mark.parametrize(("task", "kind", "counts"), [
    ("run_stale_sweep", "stale", {"reset": 1, "errored": 0, "enqueue_failed": 0}),
    ("run_reconcile_sweep", "reconcile", {"requeued": 2, "errored": 1, "enqueue_failed": 0}),
    ("run_abandoned_sweep", "abandoned", {"cancelled": 3, "abort_failed": 1}),
])
def test_sweep_logs_its_counts(sweepers: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                               task: str, kind: str, counts: dict[str, int]) -> None:
    """Success → one '<kind>_sweep' line carrying the API's counts."""
    fake = FakeClient()
    monkeypatch.setattr(sweepers, "internal", lambda: fake)
    with caplog.at_level(logging.INFO, logger="workers.sweepers"):
        getattr(sweepers, task).apply().get()
    record = next(r for r in caplog.records if r.getMessage() == f"{kind}_sweep")
    assert fake.calls == [kind] and {k: record.fields[k] for k in counts} == counts


@pytest.mark.parametrize("error", [Transient("API 503"), InternalAuthError(401, "invalid_internal_key", "x"),
                                   KeyError("bug")], ids=["Transient", "401", "bug"])
@pytest.mark.parametrize("task", ["run_stale_sweep", "run_reconcile_sweep", "run_abandoned_sweep"])
def test_failed_sweep_is_logged_and_not_retried(sweepers: Any, monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture, task: str,
                                                error: BaseException) -> None:
    """Any failure → sweep_failed, the task completes normally, self.retry is never called."""
    monkeypatch.setattr(sweepers, "internal", lambda: FakeClient(error))
    retried: list[Any] = []
    monkeypatch.setattr(getattr(sweepers, task), "retry", lambda *a, **k: retried.append(k))
    with caplog.at_level(logging.WARNING, logger="workers.sweepers"):
        result = getattr(sweepers, task).apply()
    assert result.successful() and retried == []
    assert any(r.getMessage() == "sweep_failed" for r in caplog.records)


def test_client_sweep_calls_the_internal_route() -> None:
    """InternalClient.sweep POSTs /internal/sweeps/{kind} with the key and returns the counts."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"requeued": 0, "errored": 0, "enqueue_failed": 0})

    client = InternalClient("http://api:8000", "k", transport=httpx.MockTransport(handler))
    assert client.sweep("reconcile") == {"requeued": 0, "errored": 0, "enqueue_failed": 0}
    assert seen[0].method == "POST" and seen[0].url.path == "/internal/sweeps/reconcile"
    assert seen[0].headers["X-Internal-Key"] == "k"
