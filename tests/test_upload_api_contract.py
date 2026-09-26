"""Contract tests: Anugrah's Upload API, driven by the 0.2 fixtures in tests/fixtures/upload_api/ (golden rule).

Each case sends the fixture's request body as captured — only server-generated values (key, uploadId, id, fileId)
are swapped for the ones our own initiate returned — and checks the response:
  * the status: the fixture's, or the merge's where the spec changes it (INTENDED, with the requirement);
  * a success body has every field of the fixture response;
  * an error body is exactly {"detail": …} — word for word where the spec keeps the error.
The fixtures upload report.pdf, so these tests allow pdf: D2 makes the type lists configuration.
"""
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db_session
from app.main import app
from app.models import Document
from app.repositories.documents import normalize_file_name
from tests.db_helpers import POC_ORG, POC_USER
from tests.upload_fakes import FakeStorage, Recorder, enqueued, fake_storage  # noqa: F401 — fixtures

FIXTURES = Path(__file__).parent / "fixtures" / "upload_api"

# Where the merge answers differently from Anugrah's server, on purpose. Fields stay; status or outcome changes.
INTENDED = {
    "check_duplicate__true": "U6.1 — the match is against library documents",
    "initiate__409_duplicate": "U1.4 — the match is against library documents",
}


def fixture(name: str) -> dict[str, Any]:
    """One captured exchange."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorage,  # noqa: F811
           enqueued: Recorder) -> Iterator[TestClient]:  # noqa: F811
    """The real app on the rolled-back session, fake S3, recorded enqueue, and pdf allowed (the fixtures' type)."""
    monkeypatch.setenv("ALLOWED_TOP_LEVEL_EXT", "docx,zip,pdf")
    get_settings.cache_clear()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def send(client: TestClient, name: str, **replace: Any) -> tuple[Any, dict[str, Any]]:
    """Send the fixture's request, with server-generated body values replaced; return (response, fixture)."""
    fx = fixture(name)
    request = fx["request"]
    path = replace.pop("path", request["path"])        # a path carrying a server-generated uploadId
    body = request["body"]
    if isinstance(body, dict):
        body = {k: replace.get(k, v) for k, v in body.items()}
    response = client.request(request["method"], path, json=body)
    return response, fx


def assert_success_fields(response: Any, fx: dict[str, Any]) -> Any:
    """200, and the body has every field of the fixture response."""
    assert response.status_code == 200, response.text
    body, expected = response.json(), fx["response"]["body"]
    if isinstance(expected, list):
        assert isinstance(body, list)
        for item in body:
            assert set(expected[0]) <= set(item), set(expected[0]) - set(item)
    else:
        assert set(expected) <= set(body), set(expected) - set(body)
    return body


def assert_same_error(response: Any, fx: dict[str, Any]) -> None:
    """The fixture's status and exactly its {"detail": …} body."""
    assert response.status_code == fx["response"]["status"], response.text
    assert response.json() == fx["response"]["body"]


def add_library_document(db: Session, file_name: str, size_bytes: int) -> None:
    """A document already in the library, for the duplicate cases."""
    document_id = uuid.uuid4()
    db.add(Document(document_id=document_id, organization_id=POC_ORG, title=file_name.rsplit(".", 1)[0],
                    title_norm=normalize_file_name(file_name.rsplit(".", 1)[0]), file_name=file_name,
                    file_name_norm=normalize_file_name(file_name), s3_key=f"ClinSync/processed/x/{document_id}",
                    file_ext=file_name.rsplit(".", 1)[1], size_bytes=size_bytes, content_hash="0" * 64,
                    version_uploaded_by=POC_USER))
    db.flush()


# ── 3.1 check-duplicate and initiate ───────────────────────────────────────

def test_check_duplicate(client: TestClient) -> None:
    """No such library document → exactly the fixture body: duplicate false, message null."""
    response, fx = send(client, "check_duplicate")
    assert_success_fields(response, fx)
    assert response.json() == fx["response"]["body"]


def test_check_duplicate__true(client: TestClient, db_session: Session) -> None:
    """INTENDED (U6.1): a library document of that name (any case) and size → the fixture body, word for word."""
    add_library_document(db_session, "report.pdf", fixture("check_duplicate")["request"]["body"]["fileSize"])
    response, fx = send(client, "check_duplicate__true")
    assert response.status_code == 200 and response.json() == fx["response"]["body"]
    assert "duplicate" in response.json()["message"].lower()


def test_initiate(client: TestClient) -> None:
    """Every fixture field, the same part size and count, plus batchId; the key is under incoming/."""
    response, fx = send(client, "initiate")
    body = assert_success_fields(response, fx)
    expected = fx["response"]["body"]
    assert (body["partSize"], body["totalParts"]) == (expected["partSize"], expected["totalParts"])
    assert body["id"] == body["fileId"] and uuid.UUID(body["batchId"])
    assert body["key"].startswith(f"ClinSync/incoming/{POC_ORG}/{body['batchId']}/{body['id']}_")
    assert body["key"].endswith("_report.pdf")


def test_initiate__409_duplicate(client: TestClient, db_session: Session) -> None:
    """INTENDED (U1.4): a library duplicate → 409 with exactly the fixture's detail, which says "Duplicate"."""
    add_library_document(db_session, "report.pdf", fixture("initiate")["request"]["body"]["fileSize"])
    response, fx = send(client, "initiate__409_duplicate")
    assert_same_error(response, fx)
    assert "duplicate" in response.json()["detail"].lower()


@pytest.mark.parametrize("name", ["initiate__413_too_large", "initiate__422_zero_size"])
def test_initiate_errors_are_word_for_word(client: TestClient, name: str) -> None:
    """413 over 5 GiB and FastAPI's 422 for fileSize 0: the fixture's status and body exactly."""
    response, fx = send(client, name)
    assert_same_error(response, fx)
