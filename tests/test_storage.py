"""app/storage.py (upload-ingest-merge design.md §4, task 2.1): two clients, multipart, objects, error mapping."""
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from app import storage
from app.config import get_settings

KEY = "ClinSync/incoming/org/batch/file_valid.docx"
UPLOAD_ID = "upload-123"
INTERNAL, PUBLIC = "http://localstack:4566", "http://localhost:4566"


@pytest.fixture
def endpoints(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The compose endpoints: the API reaches LocalStack by service name, the browser by localhost."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", INTERNAL)
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", PUBLIC)
    for cached in (get_settings, storage.internal_client, storage.public_client):
        cached.cache_clear()
    yield
    for cached in (get_settings, storage.internal_client, storage.public_client):
        cached.cache_clear()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    """storage.internal_client() replaced by a stubbed client: no network, one attempt."""
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x",
                          endpoint_url="http://s3.invalid", config=Config(retries={"total_max_attempts": 1}))
    monkeypatch.setattr(storage, "internal_client", lambda: client)
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def bucket() -> str:
    """The configured bucket."""
    return get_settings().S3_BUCKET


def query(url: str) -> dict[str, str]:
    """A URL's query parameters, one value each."""
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ── the two clients ─────────────────────────────────────────────────────────

def test_presigned_part_url_uses_the_public_host_and_internal_client_the_service_name(endpoints: None) -> None:
    """The task 2.1 Done-when: a presigned part URL names localhost:4566; the internal client is localstack:4566."""
    url = storage.presign_part(KEY, UPLOAD_ID, 3)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == PUBLIC
    assert parts.path == f"/{bucket()}/{KEY}"                                 # path-style
    q = query(url)
    assert (q["uploadId"], q["partNumber"]) == (UPLOAD_ID, "3")
    assert q["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256" and q["X-Amz-SignedHeaders"] == "host"
    assert q["X-Amz-Expires"] == str(get_settings().PRESIGN_EXPIRES_SECONDS)
    assert storage.internal_client().meta.endpoint_url == INTERNAL


def test_clients_are_sigv4_path_style_with_short_timeouts(endpoints: None) -> None:
    """Both clients: SigV4, path-style for a custom endpoint, 2 s connect / 3 s read, one per process."""
    for make in (storage.internal_client, storage.public_client):
        config = make().meta.config
        assert config.signature_version == "s3v4" and config.s3["addressing_style"] == "path"
        assert (config.connect_timeout, config.read_timeout) == (2, 3)
        assert make() is make()


def test_real_aws_uses_virtual_hosted_addressing() -> None:
    """With no endpoint (real AWS) presigned URLs are virtual-hosted, which every region accepts."""
    assert storage._config(None).s3["addressing_style"] == "virtual"


def test_presign_get_is_public_and_downloads_under_the_file_name(endpoints: None) -> None:
    """A download URL on the public host, short-lived, with Content-Disposition naming the file (UTF-8 too)."""
    url = storage.presign_get("ClinSync/processed/org/doc/v1_Pré op.docx", "Pré op.docx")
    assert url.startswith(f"{PUBLIC}/{bucket()}/ClinSync/processed/")
    q = query(url)
    assert q["X-Amz-Expires"] == str(get_settings().DOWNLOAD_URL_EXPIRES_SECONDS)
    disposition = q["response-content-disposition"]
    assert disposition.startswith('attachment; filename="Pr_ op.docx"')        # ASCII fallback for old browsers
    assert unquote(disposition.split("filename*=UTF-8''")[1]) == "Pré op.docx"
    assert query(storage.presign_get("k", "a.docx", expires_in=60))["X-Amz-Expires"] == "60"


# ── multipart ───────────────────────────────────────────────────────────────

def test_create_multipart_returns_the_upload_id(stub: Stubber) -> None:
    """UploadId comes back; a missing content type defaults to application/octet-stream."""
    stub.add_response("create_multipart_upload", {"UploadId": UPLOAD_ID},
                      {"Bucket": bucket(), "Key": KEY, "ContentType": "application/octet-stream"})
    assert storage.create_multipart(KEY, None) == UPLOAD_ID


def test_complete_multipart_sorts_parts_by_number(stub: Stubber) -> None:
    """The task 2.1 Done-when: parts reach S3 in part-number order, whatever order the client sent."""
    stub.add_response("complete_multipart_upload", {"Location": "loc"}, {
        "Bucket": bucket(), "Key": KEY, "UploadId": UPLOAD_ID,
        "MultipartUpload": {"Parts": [{"PartNumber": n, "ETag": f'"e{n}"'} for n in (1, 2, 10)]}})
    parts = [{"partNumber": n, "etag": f'"e{n}"'} for n in (10, 1, 2)]
    assert storage.complete_multipart(KEY, UPLOAD_ID, parts) == "loc"


@pytest.mark.parametrize(("code", "error"), [
    ("NoSuchUpload", storage.NoSuchUpload),
    ("InvalidPart", storage.InvalidParts),
    ("InvalidPartOrder", storage.InvalidParts),
    ("EntityTooSmall", storage.InvalidParts),
    ("MalformedXML", storage.InvalidParts),
])
def test_complete_multipart_maps_errors(stub: Stubber, code: str, error: type[Exception]) -> None:
    """§5.3 needs two distinct outcomes: the upload is gone, or S3 refused the part list. Each keeps S3's code
    and message, which the Upload API shows as "S3 error (<code>): <message>"."""
    stub.add_client_error("complete_multipart_upload", service_error_code=code, service_message="S3 says no",
                          http_status_code=400)
    with pytest.raises(error) as raised:
        storage.complete_multipart(KEY, UPLOAD_ID, [{"partNumber": 1, "etag": '"e"'}])
    assert (raised.value.code, str(raised.value)) == (code, "S3 says no")


def test_complete_multipart_other_errors_propagate(stub: Stubber) -> None:
    """Anything else (denied, 5xx) is neither: it propagates as the ClientError."""
    stub.add_client_error("complete_multipart_upload", service_error_code="AccessDenied", http_status_code=403)
    with pytest.raises(ClientError):
        storage.complete_multipart(KEY, UPLOAD_ID, [{"partNumber": 1, "etag": '"e"'}])


def test_complete_without_location_falls_back_to_an_s3_uri(stub: Stubber) -> None:
    """A response with no Location still yields a location string (Anugrah's contract has one)."""
    stub.add_response("complete_multipart_upload", {})
    assert storage.complete_multipart(KEY, UPLOAD_ID, [{"partNumber": 1, "etag": '"e"'}]) == \
        f"s3://{bucket()}/{KEY}"


def test_list_parts_follows_pages_in_anugrahs_shape(stub: Stubber) -> None:
    """Every page is read; each part is {partNumber, etag, size}."""
    base = {"Bucket": bucket(), "Key": KEY, "UploadId": UPLOAD_ID}
    stub.add_response("list_parts", {"Parts": [{"PartNumber": 1, "ETag": '"a"', "Size": 8}],
                                     "IsTruncated": True, "NextPartNumberMarker": 1}, {**base, "PartNumberMarker": 0})
    stub.add_response("list_parts", {"Parts": [{"PartNumber": 2, "ETag": '"b"', "Size": 3}], "IsTruncated": False},
                      {**base, "PartNumberMarker": 1})
    assert storage.list_parts(KEY, UPLOAD_ID) == [{"partNumber": 1, "etag": '"a"', "size": 8},
                                                  {"partNumber": 2, "etag": '"b"', "size": 3}]


def test_list_parts_of_a_gone_upload_is_no_such_upload(stub: Stubber) -> None:
    """An aborted or completed upload → NoSuchUpload."""
    stub.add_client_error("list_parts", service_error_code="NoSuchUpload", http_status_code=404)
    with pytest.raises(storage.NoSuchUpload):
        storage.list_parts(KEY, UPLOAD_ID)


def test_abort_multipart_tolerates_a_gone_upload_but_not_other_errors(stub: Stubber) -> None:
    """Aborting twice is fine; a denied abort is not swallowed."""
    params = {"Bucket": bucket(), "Key": KEY, "UploadId": UPLOAD_ID}
    stub.add_response("abort_multipart_upload", {}, params)
    stub.add_client_error("abort_multipart_upload", service_error_code="NoSuchUpload", http_status_code=404)
    stub.add_client_error("abort_multipart_upload", service_error_code="AccessDenied", http_status_code=403)
    storage.abort_multipart(KEY, UPLOAD_ID)
    storage.abort_multipart(KEY, UPLOAD_ID)
    with pytest.raises(ClientError):
        storage.abort_multipart(KEY, UPLOAD_ID)


# ── objects ─────────────────────────────────────────────────────────────────

def expect_head(stub: Stubber, response: dict[str, Any] | None = None, error: tuple[str, int] | None = None) -> None:
    """Queue one head_object reply for KEY in the configured bucket."""
    params = {"Bucket": bucket(), "Key": KEY}
    if error:
        stub.add_client_error("head_object", service_error_code=error[0], http_status_code=error[1],
                              expected_params=params)
    else:
        stub.add_response("head_object", response or {}, expected_params=params)


def test_object_size_is_content_length(stub: Stubber) -> None:
    """200 → ContentLength, from the POC bucket."""
    expect_head(stub, {"ContentLength": 12_345})
    assert storage.object_size(KEY) == 12_345


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_missing_object_is_none_and_does_not_exist(stub: Stubber, code: str) -> None:
    """Every way S3 says "no such object" → None; exists() is False."""
    expect_head(stub, error=(code, 404))
    expect_head(stub, error=(code, 404))
    assert storage.object_size(KEY) is None
    assert storage.exists(KEY) is False


def test_exists_is_true_for_a_stored_object(stub: Stubber) -> None:
    """A HEAD that answers → True."""
    expect_head(stub, {"ContentLength": 1})
    assert storage.exists(KEY) is True


@pytest.mark.parametrize(("code", "status"), [("403", 403), ("AccessDenied", 403), ("InternalError", 500)])
def test_other_head_errors_propagate(stub: Stubber, code: str, status: int) -> None:
    """Denied or broken S3 is not a missing object: the error propagates."""
    expect_head(stub, error=(code, status))
    with pytest.raises(ClientError):
        storage.object_size(KEY)


def test_delete_removes_one_object(stub: Stubber) -> None:
    """One DeleteObject in the POC bucket."""
    stub.add_response("delete_object", {}, {"Bucket": bucket(), "Key": KEY})
    storage.delete(KEY)


def test_delete_many_batches_by_1000_and_skips_empty_keys(stub: Stubber) -> None:
    """2 500 keys → three DeleteObjects calls of 1000, 1000 and 500; None and "" are skipped."""
    keys = [f"k{i}" for i in range(2500)] + [None, ""]
    for size in (1000, 1000, 500):
        stub.add_response("delete_objects", {}, {"Bucket": bucket(), "Delete": {"Objects": ANY, "Quiet": True}})
    storage.delete_many(keys)


def test_delete_many_of_nothing_calls_nothing(stub: Stubber) -> None:
    """No keys → no request (the stub would fail on an unexpected call)."""
    storage.delete_many([None, ""])


def test_delete_many_raises_when_s3_reports_errors(stub: Stubber) -> None:
    """A per-key failure in the response is not ignored."""
    stub.add_response("delete_objects", {"Errors": [{"Key": "k", "Code": "AccessDenied", "Message": "no"}]})
    with pytest.raises(RuntimeError, match="could not delete 1"):
        storage.delete_many(["k"])


def test_copy_is_a_managed_server_side_copy_in_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """copy() uses boto3's managed copy (multipart above 5 GB), source and destination in the POC bucket."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(storage, "internal_client", lambda: SimpleNamespace(copy=lambda *a: calls.append(a)))
    storage.copy("ClinSync/staging/a", "ClinSync/processed/b")
    assert calls == [({"Bucket": bucket(), "Key": "ClinSync/staging/a"}, bucket(), "ClinSync/processed/b")]
