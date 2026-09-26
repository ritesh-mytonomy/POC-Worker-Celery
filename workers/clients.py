"""Clients for the outside world (design.md §8.1): the Internal API and S3, each with its error mapping.

Sections, in order: Transient · Internal API client · S3 store. This module is the only place that raises
Transient: workers reach state only through the Internal API (R4.1, R4.2) and bytes only through S3 (R1.4).
"""
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectionClosedError, EndpointConnectionError, HTTPClientError, IncompleteReadError,
    NoCredentialsError, PartialCredentialsError, ResponseStreamingError,
)
from botocore.exceptions import ConnectionError as BotoConnectionError

from app.errors import ClaimSuperseded
from app.logging import get_logger

# ============================================================================ Transient


class Transient(Exception):
    """A temporary failure (S3 unavailable, Internal API 5xx, connection reset); retry (R11.1)."""


# ============================================================================ Internal API client
# Error mapping: 409 not_claimable → None · 409 claim_superseded → ClaimSuperseded · other 409, 400, other 4xx →
# WorkerContractError · 401 → InternalAuthError (CRITICAL log) · 5xx and transport errors → Transient.

_api_log = get_logger("workers.internal_client")
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
        _api_log.critical("internal_api_key_rejected", file_id=str(file_id), path=response.request.url.path,
                     hint="INTERNAL_API_KEY does not match the API; this worker cannot do anything until fixed")
        raise InternalAuthError(status, code, message)
    _api_log.error("internal_api_contract_error", file_id=str(file_id), status=status, code=code, message=message,
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
                         reject_reason: str | None = None, content_hash: str | None = None) -> uuid.UUID:
        """Insert or overwrite one candidate (R8.3); return its staged_id. A processed one carries its SHA-256."""
        response = self._write("PUT", "candidates", {
            "source_entry_name": entry_name, "entry_index": entry_index, "file_name": file_name,
            "file_ext": file_ext, "status": status, "size_bytes": size_bytes, "s3_key": s3_key,
            "reject_reason": reject_reason, "content_hash": content_hash})
        return uuid.UUID(response.json()["staged_id"])

    def finish(self, final: FinalStatus) -> None:
        """Set the terminal status (R10.3)."""
        self._write("POST", "finish", {"status": final.status, "status_message": final.message})

    def release(self, reason: str) -> None:
        """Hand the file back before a retry."""
        self._write("POST", "release", {"reason": reason})


# ============================================================================ S3 store
# Error mapping: connection errors, timeouts, 5xx and throttling → Transient · 404 / NoSuchKey →
# S3ObjectNotFound (finish error, no retry) · 403 / AccessDenied and other 4xx → S3ConfigError (CRITICAL log).

_s3_log = get_logger("workers.s3")
CHUNK = 64 * 1024
# Short on purpose (task 10.2): a hung S3 took 30.6 s to fail with read_timeout=10 and 3 attempts, silently
# absorbing an outage. Now a failure surfaces in a few seconds and the task's own release + backoff takes over.
# read_timeout is per socket read (inactivity), not the whole transfer. One botocore retry covers a 5xx blip.
CLIENT_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 2})

NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
DENIED = {"AccessDenied", "403", "Forbidden", "InvalidAccessKeyId", "SignatureDoesNotMatch", "AllAccessDisabled",
          "AccountProblem", "InvalidToken", "ExpiredToken"}
THROTTLED = {"SlowDown", "Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequests",
             "RequestTimeout", "RequestTimeTooSkewed", "ServiceUnavailable", "InternalError"}
TRANSPORT = (BotoConnectionError, EndpointConnectionError, ConnectionClosedError, HTTPClientError,
             ResponseStreamingError, IncompleteReadError)


class S3ObjectNotFound(Exception):
    """The object is not there (404 / NoSuchKey). Deterministic: finish `error`, never retry."""


class S3ConfigError(Exception):
    """S3 refused us for a reason retrying cannot fix (403 / AccessDenied, missing bucket, no credentials)."""


def map_error(exc: Exception, key: str) -> Exception:
    """Translate a botocore error into Transient, S3ObjectNotFound or S3ConfigError (or return it unchanged)."""
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in NOT_FOUND:
            return S3ObjectNotFound(f"s3://{key} not found")
        if code in DENIED or status == 403:
            return _config_error(f"access denied ({code}) for {key}", exc)
        if status >= 500 or code in THROTTLED:
            return Transient(f"S3 {status} {code} for {key}")
        return _config_error(f"S3 refused {key}: {status} {code}", exc)
    if isinstance(exc, TRANSPORT):
        return Transient(f"S3 unreachable for {key}: {exc!r}")
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return _config_error(f"no usable AWS credentials for {key}", exc)
    return exc


def _config_error(message: str, exc: Exception) -> S3ConfigError:
    """Log a configuration problem loudly and build the error."""
    _s3_log.critical("s3_configuration_error", message=message, error=repr(exc),
                 hint="check the bucket, credentials and IAM policy; retrying will not help")
    return S3ConfigError(message)


class S3Store:
    """The POC bucket, through one boto3 client."""

    def __init__(self, client: Any, bucket: str) -> None:
        """Wrap a boto3 S3 client and a bucket name."""
        self.client, self.bucket = client, bucket

    @classmethod
    def from_settings(cls, settings: Any) -> "S3Store":
        """boto3 client with endpoint_url=AWS_ENDPOINT_URL when set (LocalStack), otherwise real AWS."""
        client = boto3.client("s3", endpoint_url=settings.AWS_ENDPOINT_URL or None, config=CLIENT_CONFIG)
        return cls(client, settings.S3_BUCKET)

    def download_to_tmp(self, key: str, tmp_dir: Path | None = None, digest: Any = None) -> Path:
        """Stream the object to a temp file in 64 KB chunks; the caller deletes it. No partial file on failure.

        `digest` (a hashlib object) is fed each block as it is written: the file's hash with no second read (U5.2).
        """
        fd, name = tempfile.mkstemp(prefix="clinsync-download-", dir=tmp_dir)
        os.close(fd)
        out = Path(name)
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            with out.open("wb") as dst:
                for block in body.iter_chunks(CHUNK):
                    dst.write(block)
                    if digest is not None:
                        digest.update(block)
        except (ClientError, BotoCoreError) as exc:
            out.unlink(missing_ok=True)
            raise map_error(exc, key) from exc
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        return out

    def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy within the bucket (no bytes pass through the worker)."""
        try:
            self.client.copy_object(Bucket=self.bucket, Key=dst_key, CopySource={"Bucket": self.bucket, "Key": src_key})
        except (ClientError, BotoCoreError) as exc:
            raise map_error(exc, src_key) from exc

    def upload(self, path: Path, key: str) -> None:
        """Upload a local file to key."""
        try:
            with path.open("rb") as src:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=src)
        except (ClientError, BotoCoreError) as exc:
            raise map_error(exc, key) from exc

    def delete_quietly(self, key: str) -> None:
        """Delete the object; never raise — the incoming/ lifecycle rule is the backstop (R10.2)."""
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            _s3_log.warning("s3_delete_failed", key=key, error=repr(exc))

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under prefix (design.md §8.5a step 1); return how many. Errors map as above."""
        deleted = 0
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                for start in range(0, len(keys), 1000):
                    batch = keys[start:start + 1000]
                    result = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
                    if result.get("Errors"):
                        raise Transient(f"S3 could not delete {len(result['Errors'])} objects under {prefix}")
                    deleted += len(batch)
        except (ClientError, BotoCoreError) as exc:
            raise map_error(exc, prefix) from exc
        return deleted
