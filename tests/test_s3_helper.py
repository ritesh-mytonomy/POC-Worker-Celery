"""workers/s3.py: every S3 failure maps to the right worker outcome (task 8.1), plus a LocalStack round trip."""
import io
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import (
    ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError,
)
from botocore.stub import Stubber

from engine.errors import Transient
from workers.s3 import CHUNK, S3ConfigError, S3ObjectNotFound, S3Store

BUCKET = "clinsync-poc"


def stubbed() -> tuple[S3Store, Stubber]:
    """A store over a stubbed client: no network, one attempt (so each stubbed error is seen once)."""
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x",
                          endpoint_url="http://s3.invalid", config=Config(retries={"total_max_attempts": 1}))
    return S3Store(client, BUCKET), Stubber(client)


OPS = {
    "download": ("get_object", lambda store, tmp: store.download_to_tmp("ClinSync/incoming/a.docx", tmp_dir=tmp)),
    "copy": ("copy_object", lambda store, tmp: store.copy("ClinSync/incoming/a.docx", "ClinSync/staging/a.docx")),
    "upload": ("put_object", lambda store, tmp: store.upload(_file(tmp.parent), "ClinSync/staging/a.docx")),
}


def _file(tmp: Path) -> Path:
    path = tmp / "src.bin"
    path.write_bytes(b"data")
    return path


CLIENT_ERRORS = [
    ("NoSuchKey", 404, S3ObjectNotFound),
    ("404", 404, S3ObjectNotFound),
    ("AccessDenied", 403, S3ConfigError),
    ("403", 403, S3ConfigError),
    ("InvalidAccessKeyId", 403, S3ConfigError),
    ("NoSuchBucket", 404, S3ConfigError),        # a missing bucket is configuration, not a missing upload
    ("InternalError", 500, Transient),
    ("ServiceUnavailable", 503, Transient),
    ("SlowDown", 503, Transient),
    ("ThrottlingException", 400, Transient),
    ("RequestTimeout", 400, Transient),
    ("SomethingNew", 502, Transient),            # any 5xx
]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(("code", "status", "expected"), CLIENT_ERRORS, ids=[f"{c}-{s}" for c, s, _ in CLIENT_ERRORS])
def test_client_error_mapping(tmp_path: Path, op: str, code: str, status: int, expected: type) -> None:
    """Each S3 error code maps the same way for download, copy and upload; no temp file survives."""
    store, stub = stubbed()
    method, call = OPS[op]
    stub.add_client_error(method, service_error_code=code, http_status_code=status)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    with stub, pytest.raises(expected):
        call(store, downloads)
    assert list(downloads.iterdir()) == []


CONNECTION_ERRORS = [EndpointConnectionError(endpoint_url="http://s3"), ConnectTimeoutError(endpoint_url="http://s3"),
                     ReadTimeoutError(endpoint_url="http://s3"), ConnectionClosedError(endpoint_url="http://s3")]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("error", CONNECTION_ERRORS, ids=lambda e: type(e).__name__)
def test_connection_errors_are_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str,
                                         error: Exception) -> None:
    """Refused, connect/read timeouts and dropped connections → Transient."""
    store, _ = stubbed()
    method, call = OPS[op]

    def fail(**_: Any) -> None:
        raise error

    monkeypatch.setattr(store.client, method, fail)
    with pytest.raises(Transient):
        call(store, tmp_path)


def test_no_credentials_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing credentials → S3ConfigError."""
    store, _ = stubbed()
    monkeypatch.setattr(store.client, "get_object", lambda **_: (_ for _ in ()).throw(NoCredentialsError()))
    with pytest.raises(S3ConfigError):
        store.download_to_tmp("k", tmp_dir=tmp_path)


def test_access_denied_is_logged_loudly(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """403 → a CRITICAL s3_configuration_error line saying retrying will not help."""
    store, stub = stubbed()
    stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    with stub, caplog.at_level(logging.CRITICAL, logger="workers.s3"), pytest.raises(S3ConfigError):
        store.download_to_tmp("ClinSync/incoming/a.docx", tmp_dir=tmp_path)
    record = next(r for r in caplog.records if r.getMessage() == "s3_configuration_error")
    assert record.levelno == logging.CRITICAL and "retrying will not help" in record.fields["hint"]


class Body:
    """A streaming body that records chunk sizes and can fail after some chunks."""

    def __init__(self, data: bytes, fail_after: int | None = None, error: BaseException | None = None) -> None:
        """Serve data; raise error after fail_after chunks."""
        self.data, self.fail_after, self.error, self.sizes = data, fail_after, error, []

    def iter_chunks(self, size: int) -> Any:
        """Yield chunks of `size`."""
        self.sizes.append(size)
        for n, start in enumerate(range(0, len(self.data), size)):
            if self.fail_after is not None and n >= self.fail_after:
                raise self.error  # type: ignore[misc]
            yield self.data[start:start + size]


def test_download_streams_in_64_kb_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The body is read in 64 KB chunks straight to disk."""
    store, _ = stubbed()
    body = Body(os.urandom(200 * 1024))
    monkeypatch.setattr(store.client, "get_object", lambda **_: {"Body": body})
    out = store.download_to_tmp("k", tmp_dir=tmp_path)
    assert out.read_bytes() == body.data and body.sizes == [CHUNK] and CHUNK == 64 * 1024


@pytest.mark.parametrize(("error", "expected"), [(ReadTimeoutError(endpoint_url="http://s3"), Transient),
                                                 (OSError(28, "No space left on device"), OSError)],
                         ids=["read timeout mid-stream", "disk full mid-stream"])
def test_download_failure_mid_stream_leaves_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    error: BaseException, expected: type) -> None:
    """A dropped stream is Transient; a full disk propagates unchanged (retryable); either way no partial file."""
    store, _ = stubbed()
    monkeypatch.setattr(store.client, "get_object",
                        lambda **_: {"Body": Body(bytes(300 * 1024), fail_after=2, error=error)})
    with pytest.raises(expected):
        store.download_to_tmp("k", tmp_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["client_error", "connection"])
def test_delete_quietly_never_raises(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """delete_quietly swallows every error (the incoming/ lifecycle rule is the backstop)."""
    store, stub = stubbed()
    if failure == "client_error":
        stub.add_client_error("delete_object", service_error_code="AccessDenied", http_status_code=403)
        with stub:
            store.delete_quietly("k")
    else:
        monkeypatch.setattr(store.client, "delete_object",
                            lambda **_: (_ for _ in ()).throw(EndpointConnectionError(endpoint_url="x")))
        store.delete_quietly("k")


@pytest.mark.parametrize(("endpoint", "expected"), [("http://localstack:4566", "http://localstack:4566"),
                                                    (None, "amazonaws.com")])
def test_endpoint_is_the_only_difference(endpoint: str | None, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1.4: the standard boto3 client; AWS_ENDPOINT_URL set → LocalStack, unset → real S3.

    boto3 (>= 1.28) also reads the AWS_ENDPOINT_URL environment variable itself, so "unset" must clear it too.
    """
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    settings = SimpleNamespace(AWS_ENDPOINT_URL=endpoint, S3_BUCKET=BUCKET)
    store = S3Store.from_settings(settings)
    assert expected in store.client.meta.endpoint_url and store.bucket == BUCKET


# --- smoke test through LocalStack (compose network) ---

@pytest.fixture
def localstack() -> S3Store:
    """The real store from settings; skip if LocalStack is unreachable (fail when REQUIRE_DB=1)."""
    from app.config import get_settings
    store = S3Store.from_settings(get_settings())
    try:
        store.client.head_bucket(Bucket=store.bucket)
    except Exception as exc:
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"LocalStack not reachable: {exc!r} (REQUIRE_DB=1)")
        pytest.skip(f"LocalStack not reachable: {exc!r}")
    return store


def test_round_trip_through_localstack(localstack: S3Store, tmp_path: Path) -> None:
    """upload → download → server-side copy → download → delete; a missing key is S3ObjectNotFound."""
    run = uuid.uuid4().hex
    src_key, dst_key = f"ClinSync/incoming/smoke-{run}/a.bin", f"ClinSync/staging/smoke-{run}/0000_a.bin"
    data = os.urandom(300 * 1024)
    local = tmp_path / "a.bin"
    local.write_bytes(data)
    try:
        localstack.upload(local, src_key)
        assert localstack.download_to_tmp(src_key, tmp_dir=tmp_path).read_bytes() == data
        localstack.copy(src_key, dst_key)
        assert localstack.download_to_tmp(dst_key, tmp_dir=tmp_path).read_bytes() == data
    finally:
        localstack.delete_quietly(src_key)
        localstack.delete_quietly(dst_key)
    with pytest.raises(S3ObjectNotFound):
        localstack.download_to_tmp(src_key, tmp_dir=tmp_path)
    with pytest.raises(S3ObjectNotFound):
        localstack.copy(src_key, dst_key)
