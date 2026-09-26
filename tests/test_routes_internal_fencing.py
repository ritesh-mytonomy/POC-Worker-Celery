"""Route tests for /internal/* (design.md §6.2): status codes, error envelope, fencing (R3.1, R4.2, R4.4)."""
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db_session
from app.main import app
from app.repositories import files as files_repo
from tests.db_helpers import MAX_ATTEMPTS, STALE, claimed, make_file, set_heartbeat_age

HASH = "ab" * 32                         # a content_hash: 64 lowercase hex (U5.2)

KEY = {"X-Internal-Key": get_settings().INTERNAL_API_KEY}
CANDIDATE = {"source_entry_name": "docs/a.docx", "entry_index": 0, "file_name": "a.docx", "file_ext": "docx",
             "status": "processed", "s3_key": "ClinSync/staging/k", "size_bytes": 10,
             "content_hash": HASH}

# Every write route: method, path suffix, a valid body, the success status.
WRITES: dict[str, tuple[str, str, dict[str, Any] | None, int]] = {
    "heartbeat": ("POST", "heartbeat", None, 204),
    "progress": ("PATCH", "progress", {"entries_done": 1}, 204),
    "candidates": ("PUT", "candidates", CANDIDATE, 200),
    "finish": ("POST", "finish", {"status": "processed"}, 204),
    "release": ("POST", "release", {"reason": "S3 unavailable"}, 204),
}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """The real app, with its sessions replaced by the rolled-back test session."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def call(client: TestClient, name: str, file_id: uuid.UUID, token: str | None,
         body: dict[str, Any] | None = None) -> Any:
    """Send write route `name` with the key and, if given, the claim token."""
    method, suffix, default_body, _ = WRITES[name]
    headers = {**KEY, **({"X-Claim-Token": token} if token is not None else {})}
    return client.request(method, f"/internal/files/{file_id}/{suffix}", headers=headers,
                          json=body if body is not None else default_body)


def assert_error(response: Any, status: int, code: str) -> None:
    """The response is the error envelope with this status and code."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code and body["error"]["message"]


# --- claim ---

def test_claim_returns_file_claim_then_not_claimable(client: TestClient, db_session: Session) -> None:
    """First claim → 200 FileClaim; a second while the heartbeat is fresh → 409 not_claimable."""
    file_id = make_file(db_session)
    response = client.post(f"/internal/files/{file_id}/claim", headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"file_id", "organization_id", "batch_id", "s3_key", "file_name", "file_ext",
                         "is_archive", "entries_done", "attempt_count", "claim_token"}
    assert body["file_id"] == str(file_id) and body["attempt_count"] == 1
    assert_error(client.post(f"/internal/files/{file_id}/claim", headers=KEY), 409, "not_claimable")


# --- fencing: every write route ---

@pytest.mark.parametrize("name", WRITES)
def test_write_with_current_token_succeeds(client: TestClient, db_session: Session, name: str) -> None:
    """The current token → 204 (200 for candidates)."""
    file_id, token = claimed(db_session)
    response = call(client, name, file_id, str(token))
    assert response.status_code == WRITES[name][3], response.text
    if name == "candidates":
        assert uuid.UUID(response.json()["staged_id"])


@pytest.mark.parametrize("name", WRITES)
def test_write_with_old_token_after_takeover_is_409(client: TestClient, db_session: Session, name: str) -> None:
    """An old token after a stale takeover → 409 claim_superseded."""
    file_id, old = claimed(db_session)
    set_heartbeat_age(db_session, file_id, STALE + 5)
    assert files_repo.claim(db_session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)
    assert_error(call(client, name, file_id, str(old)), 409, "claim_superseded")


@pytest.mark.parametrize("name", WRITES)
@pytest.mark.parametrize("token", [None, "", "not-a-uuid", "1234"])
def test_write_with_missing_or_malformed_token_is_400(
    client: TestClient, db_session: Session, name: str, token: str | None
) -> None:
    """A missing or malformed X-Claim-Token → 400 invalid_claim_token, not FastAPI's 422."""
    file_id, _ = claimed(db_session)
    assert_error(call(client, name, file_id, token), 400, "invalid_claim_token")


# --- other mappings ---

def test_candidate_identity_mismatch_is_409(client: TestClient, db_session: Session) -> None:
    """A replay with a different file_name → 409 candidate_identity_mismatch."""
    file_id, token = claimed(db_session)
    assert call(client, "candidates", file_id, str(token)).status_code == 200
    response = call(client, "candidates", file_id, str(token), {**CANDIDATE, "file_name": "b.docx"})
    assert_error(response, 409, "candidate_identity_mismatch")


@pytest.mark.parametrize(("name", "body"), [
    ("progress", {}),                                                   # nothing to set
    ("progress", {"entries_done": -1}),
    ("progress", {"entries_done": "many"}),
    ("progress", {"entries_done": 1, "unknown": 2}),
    ("finish", {"status": "uploaded"}),                                 # not terminal
    ("finish", {}),
    ("candidates", {**CANDIDATE, "s3_key": None}),                      # processed without s3_key
    ("candidates", {**CANDIDATE, "status": "rejected"}),                # rejected with s3_key, no reason
    ("candidates", {**CANDIDATE, "entry_index": None}),                 # name without index
    ("candidates", {**CANDIDATE, "batch_id": str(uuid.uuid4())}),       # not accepted from workers
    ("candidates", {**CANDIDATE, "organization_id": str(uuid.uuid4())}),
    ("release", {"reason": ""}),
    ("release", {}),
])
def test_invalid_request_is_400(client: TestClient, db_session: Session, name: str, body: dict[str, Any]) -> None:
    """Deterministic caller mistakes → 400 validation_error (never retried)."""
    file_id, token = claimed(db_session)
    assert_error(call(client, name, file_id, str(token), body), 400, "validation_error")


def test_malformed_path_id_with_key_is_400(client: TestClient) -> None:
    """With a valid key, a path id that is not a UUID → 400 validation_error."""
    assert_error(client.post("/internal/files/not-a-uuid/claim", headers=KEY), 400, "validation_error")


def test_unexpected_error_is_500(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything unexpected → 500 internal_error, which the worker retries."""
    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("database fell over")

    monkeypatch.setattr(files_repo, "heartbeat", boom)
    app.dependency_overrides[get_db_session] = lambda: db_session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            file_id, token = claimed(db_session)
            assert_error(call(client, "heartbeat", file_id, str(token)), 500, "internal_error")
    finally:
        app.dependency_overrides.clear()


def test_plain_value_error_is_500_not_400(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only InvalidInput is a caller mistake; a plain ValueError from a bug stays a retryable 500."""
    def bug(*_: Any, **__: Any) -> None:
        raise ValueError("bug, not validation")

    monkeypatch.setattr(files_repo, "heartbeat", bug)
    app.dependency_overrides[get_db_session] = lambda: db_session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            file_id, token = claimed(db_session)
            assert_error(call(client, "heartbeat", file_id, str(token)), 500, "internal_error")
    finally:
        app.dependency_overrides.clear()


def test_release_logs_reason(client: TestClient, db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    """release → 204 and a `released` log line with file_id, attempt and reason."""
    file_id, token = claimed(db_session)
    with caplog.at_level(logging.INFO, logger="app.repositories.files"):
        assert call(client, "release", file_id, str(token)).status_code == 204
    record = next(r for r in caplog.records if r.getMessage() == "released")
    assert record.fields == {"file_id": str(file_id), "attempt": 1, "reason": "S3 unavailable"}


@pytest.mark.parametrize(("path", "keys"), [("stale", {"reset", "errored", "enqueue_failed"}),
                                            ("reconcile", {"requeued", "errored", "enqueue_failed"})])
def test_sweeps_return_documented_counts(client: TestClient, path: str, keys: set[str]) -> None:
    """Sweeps → 200 with exactly the §6.2 keys."""
    response = client.post(f"/internal/sweeps/{path}", headers=KEY)
    assert response.status_code == 200 and set(response.json()) == keys


# --- claim is the first statement in a fresh session (real sessions, committed rows) ---

def test_claim_route_calls_claim_first_in_fresh_session_with_settings(
    committed_file: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's session has run nothing before claim(), and the limits come from settings."""
    seen: dict[str, Any] = {}
    real_claim = files_repo.claim

    def spy(db: Session, file_id: uuid.UUID, **limits: Any) -> Any:
        seen["in_transaction"] = db.in_transaction()
        seen["limits"] = limits
        return real_claim(db, file_id, **limits)

    monkeypatch.setattr(files_repo, "claim", spy)
    with TestClient(app) as client:
        assert client.post(f"/internal/files/{committed_file}/claim", headers=KEY).status_code == 200
    settings = get_settings()
    assert seen["in_transaction"] is False
    assert seen["limits"] == {"max_attempts": settings.MAX_ATTEMPTS,
                              "stale_after_seconds": settings.STALE_AFTER_SECONDS}



# --- content_hash on candidates (upload-ingest-merge U5.2) ---

@pytest.mark.parametrize("bad", ["AB" * 32, "ab" * 31 + "a", "zz" * 32, "ab" * 33, ""])
def test_candidate_content_hash_must_be_64_lowercase_hex(client: TestClient, db_session: Session, bad: str) -> None:
    """Anything but 64 lowercase hex characters → 400 validation_error; nothing stored."""
    file_id, token = claimed(db_session)
    response = call(client, "candidates", file_id, str(token), {**CANDIDATE, "content_hash": bad})
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation_error"
    assert db_session.execute(text("SELECT count(*) FROM staged_document WHERE source_file_id = :id"),
                              {"id": file_id}).scalar() == 0


def test_candidate_content_hash_is_stored_and_overwritten(client: TestClient, db_session: Session) -> None:
    """A valid hash is stored; a replay of the same entry overwrites it, like size_bytes."""
    file_id, token = claimed(db_session)
    for digest in ("ab" * 32, "cd" * 32):
        assert call(client, "candidates", file_id, str(token), {**CANDIDATE, "content_hash": digest}).status_code == 200
    assert db_session.execute(text("SELECT content_hash FROM staged_document WHERE source_file_id = :id"),
                              {"id": file_id}).scalar_one() == "cd" * 32


def test_processed_candidate_needs_a_hash_rejected_does_not(client: TestClient, db_session: Session) -> None:
    """processed without content_hash → 400 (documents.content_hash is NOT NULL); a rejected entry needs none."""
    file_id, token = claimed(db_session)
    missing = {k: v for k, v in CANDIDATE.items() if k != "content_hash"}
    assert call(client, "candidates", file_id, str(token), missing).status_code == 400
    rejected = {"source_entry_name": "x.pdf", "entry_index": 1, "file_name": "x.pdf", "file_ext": "pdf",
                "status": "rejected", "reject_reason": ".pdf is not supported"}
    assert call(client, "candidates", file_id, str(token), rejected).status_code == 200
