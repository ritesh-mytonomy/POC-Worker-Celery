"""app/storage.py: the API's HEAD for /poc/seed (task 1.1) — size, missing object, other errors, endpoint."""
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app import storage
from app.config import get_settings

KEY = "ClinSync/incoming/x/valid.docx"


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    """storage.internal_client() replaced by a stubbed client: no network, one attempt."""
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x",
                          endpoint_url="http://s3.invalid", config=Config(retries={"total_max_attempts": 1}))
    monkeypatch.setattr(storage, "internal_client", lambda: client)
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def expect_head(stub: Stubber, response: dict[str, Any] | None = None, error: tuple[str, int] | None = None) -> None:
    """Queue one head_object reply for KEY in the configured bucket."""
    params = {"Bucket": get_settings().S3_BUCKET, "Key": KEY}
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
def test_missing_object_is_none(stub: Stubber, code: str) -> None:
    """Every way S3 says "no such object" → None."""
    expect_head(stub, error=(code, 404))
    assert storage.object_size(KEY) is None


@pytest.mark.parametrize(("code", "status"), [("403", 403), ("AccessDenied", 403), ("InternalError", 500)])
def test_other_errors_propagate(stub: Stubber, code: str, status: int) -> None:
    """Denied or broken S3 is not a missing object: the error propagates (the route answers 500)."""
    expect_head(stub, error=(code, status))
    with pytest.raises(ClientError):
        storage.object_size(KEY)


def test_internal_client_uses_aws_endpoint_url_and_short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The internal client talks to AWS_ENDPOINT_URL (LocalStack in compose) with the worker's timeouts."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    get_settings.cache_clear()
    storage.internal_client.cache_clear()
    try:
        client = storage.internal_client()
        assert client.meta.endpoint_url == "http://localstack:4566"
        assert (client.meta.config.connect_timeout, client.meta.config.read_timeout) == (2, 3)
        assert storage.internal_client() is client                      # one client per process
    finally:
        get_settings.cache_clear()
        storage.internal_client.cache_clear()
