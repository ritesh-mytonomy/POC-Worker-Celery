"""Typed httpx client for the Internal API (design.md §6.2). Workers reach state only through it (R4.1, R4.2)."""
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.errors import ClaimSuperseded
from app.logging import get_logger
from engine.errors import Transient

log = get_logger(__name__)
TIMEOUT = httpx.Timeout(10.0, connect=2.0)


class WorkerContractError(Exception):
    """The API refused the request as malformed (400, 409 candidate_identity_mismatch, other 4xx): a worker bug.

    Never retried — retrying the same request cannot succeed.
    """

    def __init__(self, status: int, code: str | None, message: str) -> None:
        """Record the HTTP status and error code."""
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


class InternalAuthError(WorkerContractError):
    """401: INTERNAL_API_KEY is wrong. A configuration error — never retried, logged loudly."""


@dataclass(frozen=True)
class FileClaim:
    """A successful claim (design.md §6.2 FileClaim)."""

    file_id: uuid.UUID
    organization_id: uuid.UUID
    batch_id: uuid.UUID
    s3_key: str
    file_name: str
    file_ext: str
    is_archive: bool
    entries_done: int
    attempt_count: int
    claim_token: uuid.UUID

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> "FileClaim":
        """Parse the 200 claim body."""
        return cls(file_id=uuid.UUID(body["file_id"]), organization_id=uuid.UUID(body["organization_id"]),
                   batch_id=uuid.UUID(body["batch_id"]), s3_key=body["s3_key"], file_name=body["file_name"],
                   file_ext=body["file_ext"], is_archive=body["is_archive"], entries_done=body["entries_done"],
                   attempt_count=body["attempt_count"], claim_token=uuid.UUID(body["claim_token"]))


@dataclass(frozen=True)
class FinalStatus:
    """The terminal status a worker reports through finish (R10.3)."""

    status: str
    message: str | None = None


def _error_code(response: httpx.Response) -> tuple[str | None, str]:
    """(code, message) from the error envelope; (None, body text) if the body is not the envelope."""
    try:
        error = response.json()["error"]
        return error.get("code"), error.get("message", "")
    except (ValueError, KeyError, TypeError):
        return None, response.text[:200]


def _raise_for(response: httpx.Response, file_id: uuid.UUID | str) -> None:
    """Map a non-success response to the worker outcome (task 6.2); return silently on 2xx."""
    if response.is_success:
        return
    status = response.status_code
    code, message = _error_code(response)
    if status >= 500:
        raise Transient(f"Internal API {status} {code or ''} for file {file_id}: {message}".strip())
    if status == 409 and code == "claim_superseded":
        raise ClaimSuperseded(file_id)
    if status == 401:
        log.critical("internal_api_key_rejected", file_id=str(file_id), path=response.request.url.path,
                     hint="INTERNAL_API_KEY does not match the API; this worker cannot do anything until fixed")
        raise InternalAuthError(status, code, message)
    log.error("internal_api_contract_error", file_id=str(file_id), status=status, code=code, message=message,
              path=response.request.url.path)
    raise WorkerContractError(status, code, message)


class InternalClient:
    """X-Internal-Key on every request; transport errors and 5xx become Transient."""

    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        """Open one pooled httpx client for the worker process."""
        self._http = httpx.Client(base_url=base_url, headers={"X-Internal-Key": api_key}, timeout=TIMEOUT,
                                  transport=transport)

    @classmethod
    def from_settings(cls, settings: Any) -> "InternalClient":
        """Build from INTERNAL_API_BASE_URL and INTERNAL_API_KEY."""
        return cls(settings.INTERNAL_API_BASE_URL, settings.INTERNAL_API_KEY)

    def request(self, method: str, path: str, file_id: uuid.UUID | str, **kwargs: Any) -> httpx.Response:
        """Send one request; map transport errors to Transient and error statuses via _raise_for."""
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:          # connect/read timeouts, refused, reset
            raise Transient(f"Internal API unreachable for file {file_id}: {exc!r}") from exc
        if not (response.status_code == 409 and _error_code(response)[0] == "not_claimable"):
            _raise_for(response, file_id)
        return response

    def claim(self, file_id: uuid.UUID | str) -> FileClaim | None:
        """Claim the file; None if it is not claimable (R3.4)."""
        response = self.request("POST", f"/internal/files/{file_id}/claim", file_id)
        if response.status_code == 409:
            return None
        return FileClaim.from_json(response.json())

    def sweep(self, kind: str) -> dict[str, int]:
        """Run the stale or reconcile sweep in the API (design.md §8.7); return its counts."""
        return self.request("POST", f"/internal/sweeps/{kind}", f"sweep:{kind}").json()

    def with_token(self, file_id: uuid.UUID | str, token: uuid.UUID | str) -> "BoundClient":
        """A client for one claimed file that adds X-Claim-Token to every write."""
        return BoundClient(self, file_id, token)

    def close(self) -> None:
        """Close the pooled connections."""
        self._http.close()


class BoundClient:
    """Writes after claim, each carrying the claim token (design.md §6.2)."""

    def __init__(self, client: InternalClient, file_id: uuid.UUID | str, token: uuid.UUID | str) -> None:
        """Bind to one file and its claim token."""
        self._client, self.file_id = client, file_id
        self._headers = {"X-Claim-Token": str(token)}

    def _write(self, method: str, suffix: str, body: dict[str, Any] | None = None) -> httpx.Response:
        """Send one fenced write."""
        return self._client.request(method, f"/internal/files/{self.file_id}/{suffix}", self.file_id,
                                    headers=self._headers, json=body)

    def heartbeat(self) -> None:
        """Refresh the heartbeat (R10.1)."""
        self._write("POST", "heartbeat")

    def progress(self, *, entries_total: int | None = None, entries_done: int | None = None,
                 detected_type: str | None = None) -> None:
        """Record progress; only the fields passed are sent."""
        body = {k: v for k, v in {"entries_total": entries_total, "entries_done": entries_done,
                                  "detected_type": detected_type}.items() if v is not None}
        self._write("PATCH", "progress", body)

    def upsert_candidate(self, *, entry_name: str | None, entry_index: int | None, file_name: str, file_ext: str,
                         status: str, size_bytes: int | None = None, s3_key: str | None = None,
                         reject_reason: str | None = None) -> uuid.UUID:
        """Insert or overwrite one candidate (R8.3); return its staged_id."""
        response = self._write("PUT", "candidates", {
            "source_entry_name": entry_name, "entry_index": entry_index, "file_name": file_name,
            "file_ext": file_ext, "status": status, "size_bytes": size_bytes, "s3_key": s3_key,
            "reject_reason": reject_reason})
        return uuid.UUID(response.json()["staged_id"])

    def finish(self, final: FinalStatus) -> None:
        """Set the terminal status (R10.3)."""
        self._write("POST", "finish", {"status": final.status, "status_message": final.message})

    def release(self, reason: str) -> None:
        """Hand the file back before a retry."""
        self._write("POST", "release", {"reason": reason})
