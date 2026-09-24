"""S3 helper for workers (design.md §8.1): boto3 with only the endpoint changed for LocalStack (R1.4).

Error mapping (task 8.1): connection errors, timeouts, 5xx and throttling → Transient; a missing object →
S3ObjectNotFound (the worker finishes `error`, no retry); 403 / AccessDenied and other 4xx → S3ConfigError
(non-retryable, logged loudly).
"""
import os
import tempfile
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectionClosedError, EndpointConnectionError, HTTPClientError, IncompleteReadError,
    NoCredentialsError, PartialCredentialsError, ResponseStreamingError,
)
from botocore.exceptions import ConnectionError as BotoConnectionError

from app.logging import get_logger
from engine.errors import Transient

log = get_logger(__name__)
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
    log.critical("s3_configuration_error", message=message, error=repr(exc),
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

    def download_to_tmp(self, key: str, tmp_dir: Path | None = None) -> Path:
        """Stream the object to a temp file in 64 KB chunks; the caller deletes it. No partial file on failure."""
        fd, name = tempfile.mkstemp(prefix="clinsync-download-", dir=tmp_dir)
        os.close(fd)
        out = Path(name)
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            with out.open("wb") as dst:
                for block in body.iter_chunks(CHUNK):
                    dst.write(block)
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
            log.warning("s3_delete_failed", key=key, error=repr(exc))

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
