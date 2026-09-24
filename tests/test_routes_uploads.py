"""Tests for confirm (R2) and the producer-only tasks_client (design.md §6.2a)."""
import logging
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import tasks_client
from app.config import get_settings
from app.constants import QUEUE_INGEST, TASK_PROCESS_UPLOAD
from app.db import get_db_session
from app.main import app
from app.services import uploads
from tests.db_helpers import full_row, make_file


class Recorder:
    """A thread-safe fake enqueue that records calls, or raises when `fail` is set."""

    def __init__(self, fail: bool = False) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, str]] = []
        self.fail = fail
        self._lock = threading.Lock()

    def __call__(self, file_id: str, organization_id: str) -> None:
        """Record the call, or raise as an unreachable Redis would."""
        if self.fail:
            raise ConnectionError("Redis unreachable")
        with self._lock:
            self.calls.append((file_id, organization_id))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Replace the real publish with a recorder."""
    fake = Recorder()
    monkeypatch.setattr(tasks_client, "enqueue_process_upload", fake)
    return fake


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """The real app, with its sessions replaced by the rolled-back test session."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def confirm(client: TestClient, file_id: Any) -> Any:
    """POST confirm for file_id."""
    return client.post(f"/api/v1/uploads/{file_id}/confirm")


# --- the three branches, plus unknown and malformed ---

def test_confirm_uploading_sets_uploaded_and_enqueues(client: TestClient, db_session: Session,
                                                      recorder: Recorder) -> None:
    """uploading → 200 uploaded, enqueued; uploaded_at set; one publish with file_id and organization_id."""
    file_id = make_file(db_session, status="uploading")
    response = confirm(client, file_id)
    assert response.status_code == 200
    assert response.json() == {"file_id": str(file_id), "status": "uploaded", "enqueued": True}
    row = full_row(db_session, file_id)
    assert row["status"] == "uploaded" and row["uploaded_at"] is not None
    assert recorder.calls == [(str(file_id), str(row["organization_id"]))]


@pytest.mark.parametrize("status", ["uploaded", "processing", "processed", "partial", "rejected", "error"])
def test_confirm_later_status_is_unchanged_and_not_enqueued(client: TestClient, db_session: Session,
                                                           recorder: Recorder, status: str) -> None:
    """Any later status → 200 with that status, enqueued false, row untouched (R2.5)."""
    file_id = make_file(db_session, status=status)
    before = full_row(db_session, file_id)
    response = confirm(client, file_id)
    assert response.status_code == 200
    assert response.json() == {"file_id": str(file_id), "status": status, "enqueued": False}
    assert full_row(db_session, file_id) == before and recorder.calls == []


def test_confirm_unknown_file_is_404(client: TestClient, recorder: Recorder) -> None:
    """Unknown file → 404 not_found."""
    response = confirm(client, uuid.uuid4())
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_confirm_malformed_id_is_400(client: TestClient) -> None:
    """Malformed id → 400 validation_error."""
    assert confirm(client, "not-a-uuid").status_code == 400


def test_confirm_with_redis_down_still_returns_uploaded(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Publish fails → 200 uploaded, enqueued false, logged enqueue_failed; the file stays uploaded (R2.6)."""
    monkeypatch.setattr(tasks_client, "enqueue_process_upload", Recorder(fail=True))
    file_id = make_file(db_session, status="uploading")
    with caplog.at_level(logging.WARNING, logger="app.services.uploads"):
        response = confirm(client, file_id)
    assert response.status_code == 200
    assert response.json() == {"file_id": str(file_id), "status": "uploaded", "enqueued": False}
    assert full_row(db_session, file_id)["status"] == "uploaded"
    record = next(r for r in caplog.records if r.getMessage() == "enqueue_failed")
    assert record.fields["file_id"] == str(file_id)


# --- the real producer against an address that never answers: bounded by the connect timeout ---

@pytest.mark.parametrize("broker_url", [
    "redis://10.255.255.1:6379/0",       # unreachable IP: ended by the connect timeout
    "redis://no-such-host:6379/0",       # name does not resolve: a stopped compose container
    "redis://postgres:6379/0",           # reachable host, port closed: connection refused
], ids=["timeout", "unresolvable", "refused"])
def test_confirm_with_broker_down_returns_within_2s(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, broker_url: str
) -> None:
    """With the real tasks_client, every way Redis can be down still gives 200 uploaded in under 2 s (R2.4)."""
    monkeypatch.setenv("REDIS_URL", broker_url)
    get_settings.cache_clear()
    tasks_client.get_producer.cache_clear()
    try:
        file_id = make_file(db_session, status="uploading")
        started = time.monotonic()
        response = confirm(client, file_id)
        elapsed = time.monotonic() - started
    finally:
        get_settings.cache_clear()
        tasks_client.get_producer.cache_clear()
    assert response.status_code == 200
    assert response.json() == {"file_id": str(file_id), "status": "uploaded", "enqueued": False}
    assert elapsed < 2.0, f"confirm took {elapsed:.2f}s"


# --- the producer itself ---

def test_publish_sends_only_file_id_and_organization_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """R2.2: the message names the task and queue from §6.3 and carries only file_id and organization_id."""
    producer = MagicMock()
    monkeypatch.setattr(tasks_client, "get_producer", lambda: producer)
    tasks_client.enqueue_process_upload("f", "o")
    producer.send_task.assert_called_once_with(
        TASK_PROCESS_UPLOAD, kwargs={"file_id": "f", "organization_id": "o"}, queue=QUEUE_INGEST)


def test_publish_works_again_after_a_failed_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (4.4 live check): after one failed publish the next real publish succeeds, not 'closed pool'."""
    import redis

    tasks_client.reset_producer()
    producer = tasks_client.get_producer()
    real_send, failed_once = producer.send_task, []

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        """Fail the first publish as a Redis outage would; later publishes are real."""
        if not failed_once:
            failed_once.append(True)
            raise ConnectionError("Redis unreachable")
        return real_send(*args, **kwargs)

    monkeypatch.setattr(producer, "send_task", fail_once)
    with pytest.raises(ConnectionError):
        tasks_client.enqueue_process_upload("f", "o")
    queue = f"test.probe.{uuid.uuid4()}"
    tasks_client.get_producer().send_task("probe.noop", kwargs={}, queue=queue)   # real Redis
    client = redis.Redis.from_url(get_settings().REDIS_URL)
    try:
        assert client.llen(queue) == 1
    finally:
        client.delete(queue)


def test_producer_settings_fail_fast() -> None:
    """No publish retry, no connection retry or reconnect loop, 1 s timeouts (design.md §6.2a, rev 1.3)."""
    conf = tasks_client.get_producer().conf
    assert conf.task_publish_retry is False and conf.broker_connection_retry is False
    assert conf.broker_connection_timeout == 1
    assert conf.broker_transport_options == {"socket_connect_timeout": 1, "socket_timeout": 1, "max_retries": 0}


def test_tasks_client_does_not_import_the_worker_app() -> None:
    """The API never imports workers.celery_app."""
    code = "import sys, app.tasks_client, app.main; print('workers.celery_app' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "False"


# --- concurrency and ordering: committed rows, one connection per thread ---

def test_ten_concurrent_confirms_enqueue_exactly_once(race_engine: Engine, committed_uploading_file: uuid.UUID) -> None:
    """10 simultaneous confirms of one uploading file → one transition, one enqueue."""
    fake = Recorder()
    start = threading.Barrier(10)

    def one() -> uploads.Confirmed | None:
        with race_engine.connect() as conn, Session(bind=conn) as session:
            start.wait(timeout=10)
            return uploads.confirm(session, committed_uploading_file, enqueue=fake)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: one(), range(10)))
    assert sum(r.enqueued for r in results) == 1
    assert all(r.status == "uploaded" for r in results)
    assert len(fake.calls) == 1
    with race_engine.connect() as conn:
        row = conn.execute(text("SELECT status, uploaded_at FROM upload_file WHERE file_id = :id"),
                           {"id": committed_uploading_file}).one()
    assert row.status == "uploaded" and row.uploaded_at is not None


def test_confirm_enqueues_only_after_commit(race_engine: Engine, committed_uploading_file: uuid.UUID) -> None:
    """At enqueue time a second connection already sees the file committed as uploaded."""
    seen: list[str] = []

    def check(file_id: str, _: str) -> None:
        with race_engine.connect() as conn:
            seen.append(conn.execute(text("SELECT status FROM upload_file WHERE file_id = :id"),
                                     {"id": file_id}).scalar_one())

    with Session(race_engine) as session:
        assert uploads.confirm(session, committed_uploading_file, enqueue=check) == uploads.Confirmed("uploaded", True)
    assert seen == ["uploaded"]


def test_worker_app_keeps_kombu_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_retries=0 is producer-only: the workers' Celery app must keep retrying so it recovers from a Redis restart."""
    code = ("import workers.celery_app as w; "
            "print(w.app.conf.broker_transport_options.get('max_retries', 'unset'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "unset"
