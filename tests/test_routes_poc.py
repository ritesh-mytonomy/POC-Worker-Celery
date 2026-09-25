"""Tests for the POC routes /poc/seed and /poc/enqueue (design.md §6.1)."""
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import tasks_client
from app.config import get_settings
from app.db import get_db_session
from poc.main import app


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """The real app, with its sessions replaced by the rolled-back test session."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def seed(client: TestClient, *names: str) -> Any:
    """POST /poc/seed for objects named `names` under incoming/."""
    return client.post("/poc/seed", json={"files": [{"file_name": n, "s3_key": f"ClinSync/incoming/x/{n}"}
                                                    for n in names]})


def test_seed_creates_batch_and_uploading_files(client: TestClient, db_session: Session) -> None:
    """201 with ids; rows exist as uploading, under POC_ORGANIZATION_ID, is_archive from the extension."""
    response = seed(client, "mixed.zip", "valid.docx", "Report.DOCX")
    assert response.status_code == 201, response.text
    body = response.json()
    assert [(f["file_name"], f["is_archive"], f["status"]) for f in body["files"]] == [
        ("mixed.zip", True, "uploading"), ("valid.docx", False, "uploading"), ("Report.DOCX", False, "uploading")]
    rows = db_session.execute(text("SELECT f.file_id, f.file_ext, f.organization_id, f.uploaded_at, b.batch_id "
                                   "FROM upload_file f JOIN upload_batch b USING (batch_id) "
                                   "WHERE b.batch_id = :b ORDER BY f.created_at"), {"b": body["batch_id"]}).all()
    assert [str(r.file_id) for r in rows] == [f["file_id"] for f in body["files"]]
    assert [r.file_ext for r in rows] == ["zip", "docx", "docx"]
    assert {r.organization_id for r in rows} == {get_settings().POC_ORGANIZATION_ID}
    assert all(r.uploaded_at is None for r in rows)


@pytest.mark.parametrize("files", [
    [{"file_name": "notes.pdf", "s3_key": "ClinSync/incoming/notes.pdf"}],          # A3: not allowed
    [{"file_name": "noext", "s3_key": "ClinSync/incoming/noext"}],
    [{"file_name": "a.docx", "s3_key": "ClinSync/staging/a.docx"}],                 # not in incoming/
    [],
    [{"file_name": "a.docx", "s3_key": "ClinSync/incoming/a.docx", "batch_id": "x"}],
])
def test_seed_refuses_bad_input_with_400_and_writes_nothing(
    client: TestClient, db_session: Session, files: list[dict[str, Any]]
) -> None:
    """Disallowed extension, key outside incoming/, empty list or unknown field → 400; no rows."""
    response = client.post("/poc/seed", json={"files": files})
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation_error"
    assert db_session.execute(text("SELECT count(*) FROM upload_batch")).scalar_one() == 0


def test_enqueue_publishes_n_times_without_changing_state(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """times=5 → five process_upload publishes with the file's organization; the row is unchanged."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(tasks_client, "enqueue_process_upload", lambda f, o: calls.append((f, o)))
    file_id = seed(client, "valid.docx").json()["files"][0]["file_id"]
    before = db_session.execute(text("SELECT * FROM upload_file WHERE file_id = :id"), {"id": file_id}).one()
    response = client.post(f"/poc/enqueue/{file_id}", params={"times": 5})
    assert response.status_code == 200 and response.json() == {"file_id": file_id, "published": 5}
    assert calls == [(file_id, str(get_settings().POC_ORGANIZATION_ID))] * 5
    assert db_session.execute(text("SELECT * FROM upload_file WHERE file_id = :id"), {"id": file_id}).one() == before


def test_enqueue_unknown_file_is_404(client: TestClient) -> None:
    """A file that does not exist → 404 not_found."""
    response = client.post(f"/poc/enqueue/{uuid.uuid4()}")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("times", [0, 101, "x"])
def test_enqueue_times_out_of_range_is_400(client: TestClient, times: Any) -> None:
    """times outside 1..100 → 400."""
    assert client.post(f"/poc/enqueue/{uuid.uuid4()}", params={"times": times}).status_code == 400


def test_enqueue_failure_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A publish failure (Redis down, or the pre-4.4 stub) → 503 enqueue_failed."""
    def down(*_: Any) -> None:
        raise ConnectionError("Redis unreachable")

    monkeypatch.setattr(tasks_client, "enqueue_process_upload", down)
    file_id = seed(client, "valid.docx").json()["files"][0]["file_id"]
    response = client.post(f"/poc/enqueue/{file_id}", params={"times": 3})
    assert response.status_code == 503 and response.json()["error"]["code"] == "enqueue_failed"
