"""workers/internal_client.py maps every Internal API response to the right worker outcome (task 6.2)."""
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.errors import ClaimSuperseded
from workers.clients import Transient
from workers.clients import (
    BoundClient, FileClaim, FinalStatus, InternalAuthError, InternalClient, WorkerContractError,
)

FILE = uuid.uuid4()
TOKEN = uuid.uuid4()
CLAIM_BODY = {"file_id": str(FILE), "organization_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
              "s3_key": "ClinSync/incoming/x/mixed.zip", "file_name": "mixed.zip", "file_ext": "zip",
              "is_archive": True, "entries_done": 10, "attempt_count": 2, "claim_token": str(TOKEN)}


def envelope(status: int, code: str) -> httpx.Response:
    """An error response in the API's envelope."""
    return httpx.Response(status, json={"error": {"code": code, "message": f"{code} happened"}})


def make(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[InternalClient, list[httpx.Request]]:
    """A client whose transport records requests and answers with handler."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return InternalClient("http://api:8000", "the-key", transport=httpx.MockTransport(record)), seen


def bound(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[BoundClient, list[httpx.Request]]:
    """A token-bound client for FILE."""
    client, seen = make(handler)
    return client.with_token(FILE, TOKEN), seen


# Every write, as a call on a bound client.
WRITES: dict[str, Callable[[BoundClient], Any]] = {
    "heartbeat": lambda b: b.heartbeat(),
    "progress": lambda b: b.progress(entries_done=3),
    "candidates": lambda b: b.upsert_candidate(entry_name="a.docx", entry_index=0, file_name="a.docx",
                                               file_ext="docx", status="processed", s3_key="k", size_bytes=1),
    "finish": lambda b: b.finish(FinalStatus("processed")),
    "release": lambda b: b.release("S3 unavailable"),
}
OK = {"heartbeat": httpx.Response(204), "progress": httpx.Response(204),
      "candidates": httpx.Response(200, json={"staged_id": str(uuid.uuid4())}),
      "finish": httpx.Response(204), "release": httpx.Response(204)}


# --- claim ---

def test_claim_200_returns_file_claim() -> None:
    """200 → FileClaim with typed fields; X-Internal-Key sent, no token."""
    client, seen = make(lambda r: httpx.Response(200, json=CLAIM_BODY))
    claim = client.claim(FILE)
    assert claim == FileClaim.from_json(CLAIM_BODY) and claim.claim_token == TOKEN and claim.entries_done == 10
    assert seen[0].method == "POST" and seen[0].url.path == f"/internal/files/{FILE}/claim"
    assert seen[0].headers["X-Internal-Key"] == "the-key" and "X-Claim-Token" not in seen[0].headers


def test_claim_409_not_claimable_returns_none() -> None:
    """409 not_claimable → None (the caller logs claim_lost and acks)."""
    client, _ = make(lambda r: envelope(409, "not_claimable"))
    assert client.claim(FILE) is None


# --- the mapping, for claim and every write ---

CASES: list[tuple[str, httpx.Response | Exception, type[BaseException]]] = [
    ("409 claim_superseded", envelope(409, "claim_superseded"), ClaimSuperseded),
    ("409 candidate_identity_mismatch", envelope(409, "candidate_identity_mismatch"), WorkerContractError),
    ("409 unknown code", envelope(409, "something_new"), WorkerContractError),
    ("400 validation_error", envelope(400, "validation_error"), WorkerContractError),
    ("400 invalid_claim_token", envelope(400, "invalid_claim_token"), WorkerContractError),
    ("404 not_found", envelope(404, "not_found"), WorkerContractError),
    ("401 invalid_internal_key", envelope(401, "invalid_internal_key"), InternalAuthError),
    ("500 internal_error", envelope(500, "internal_error"), Transient),
    ("502 non-JSON", httpx.Response(502, text="<html>Bad Gateway</html>"), Transient),
    ("503", envelope(503, "unavailable"), Transient),
    ("connect refused", httpx.ConnectError("refused"), Transient),
    ("read timeout", httpx.ReadTimeout("slow"), Transient),
    ("connection reset", httpx.RemoteProtocolError("reset"), Transient),
]


def _answer(outcome: httpx.Response | Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return handler


@pytest.mark.parametrize(("label", "outcome", "expected"), CASES, ids=[c[0] for c in CASES])
def test_write_outcome_mapping(label: str, outcome: httpx.Response | Exception, expected: type[BaseException]) -> None:
    """Every write maps each response class to the same worker outcome."""
    for name, call in WRITES.items():
        client, _ = bound(_answer(outcome))
        with pytest.raises(expected):
            call(client)


@pytest.mark.parametrize(("label", "outcome", "expected"), CASES, ids=[c[0] for c in CASES])
def test_claim_outcome_mapping(label: str, outcome: httpx.Response | Exception, expected: type[BaseException]) -> None:
    """claim maps the same way (apart from 409 not_claimable → None)."""
    client, _ = make(_answer(outcome))
    with pytest.raises(expected):
        client.claim(FILE)


def test_contract_errors_are_not_transient_and_superseded_is_not_either() -> None:
    """The worker retries only Transient: none of the 4xx outcomes may be (or subclass) Transient."""
    for cls in (WorkerContractError, InternalAuthError, ClaimSuperseded):
        assert not issubclass(cls, Transient)
    assert issubclass(InternalAuthError, WorkerContractError)


def test_401_is_logged_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """A wrong key is a CRITICAL log line saying what to fix."""
    client, _ = make(lambda r: envelope(401, "invalid_internal_key"))
    with caplog.at_level(logging.CRITICAL, logger="workers.internal_client"):
        with pytest.raises(InternalAuthError):
            client.claim(FILE)
    record = next(r for r in caplog.records if r.getMessage() == "internal_api_key_rejected")
    assert record.levelno == logging.CRITICAL and "INTERNAL_API_KEY" in record.fields["hint"]


# --- request shape of each write ---

@pytest.mark.parametrize("name", WRITES)
def test_write_success_and_request_shape(name: str) -> None:
    """Each write hits its §6.2 route with both headers; success returns normally."""
    client, seen = bound(lambda r: OK[name])
    WRITES[name](client)
    request = seen[0]
    assert request.headers["X-Internal-Key"] == "the-key" and request.headers["X-Claim-Token"] == str(TOKEN)
    expected = {"heartbeat": ("POST", "heartbeat"), "progress": ("PATCH", "progress"),
                "candidates": ("PUT", "candidates"), "finish": ("POST", "finish"), "release": ("POST", "release")}
    assert (request.method, request.url.path) == (expected[name][0], f"/internal/files/{FILE}/{expected[name][1]}")


def test_bodies() -> None:
    """progress sends only passed fields; candidates renames entry_name; finish and release carry their fields."""
    bodies: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.rsplit("/", 1)[-1]
        bodies[suffix] = json.loads(request.content) if request.content else None
        return OK[suffix]

    client, _ = bound(handler)
    client.progress(entries_total=30)
    client.upsert_candidate(entry_name="d/a.pdf", entry_index=4, file_name="a.pdf", file_ext="pdf",
                            status="rejected", reject_reason=".pdf is not supported")
    client.finish(FinalStatus("error", "Could not be processed"))
    client.release("S3 unavailable")
    assert bodies["progress"] == {"entries_total": 30}
    assert bodies["candidates"] == {"source_entry_name": "d/a.pdf", "entry_index": 4, "file_name": "a.pdf",
                                    "file_ext": "pdf", "status": "rejected", "size_bytes": None, "s3_key": None,
                                    "reject_reason": ".pdf is not supported"}
    assert bodies["finish"] == {"status": "error", "status_message": "Could not be processed"}
    assert bodies["release"] == {"reason": "S3 unavailable"}


def test_upsert_returns_staged_id() -> None:
    """The 200 body's staged_id comes back as a UUID."""
    staged = uuid.uuid4()
    client, _ = bound(lambda r: httpx.Response(200, json={"staged_id": str(staged)}))
    assert WRITES["candidates"](client) == staged
